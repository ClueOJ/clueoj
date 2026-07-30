import logging
import uuid

from celery import shared_task
from django.db import models, transaction
from django.utils import timezone

from judge.models import Problem
from judge.models.storage import StorageProblemUsage, StorageOrganizationUsage, StorageSystemStatus, \
    StorageSyncDeadLetter, StorageSyncLease
from judge.utils import storage_client

logger = logging.getLogger('judge.tasks.storage')

SYNC_LEASE_NAME = 'catalog'
SYNC_LOCK_TTL = 300  # 5 minutes
SYNC_DEADLETTER_RETRIES = 3
SYNC_MAX_PAGES = 100


class StorageSyncLeaseLost(RuntimeError):
    pass


class StorageSyncMalformedChange(RuntimeError):
    pass


class StorageSyncUnavailable(RuntimeError):
    pass


@shared_task(
    bind=True,
    name='storage_sync_catalog',
    autoretry_for=(StorageSyncLeaseLost, StorageSyncUnavailable),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=3,
)
def storage_sync_catalog(self):
    """Pull incremental catalog changes from storage app and update local projections.

    Uses a durable DB lease so only one sync runs at a time. Cursor is committed
    inside the same DB transaction as the projection batch, after all changes are applied.
    """
    owner = str(uuid.uuid4())
    if not _acquire_sync_lease(owner):
        logger.info('storage_sync_catalog already running, skipping')
        return

    try:
        status = _system_status()
        cursor = status.sync_cursor or None
        total_processed = 0
        pages = 0
        while pages < SYNC_MAX_PAGES:
            if not _renew_sync_lease(owner):
                raise StorageSyncLeaseLost('storage sync lease lost before fetch')
            changes, next_cursor, has_more = storage_client.get_sync_changes(cursor=cursor, limit=500)
            if changes is None:
                logger.warning('storage_sync_catalog: failed to fetch changes')
                storage_mark_stale()
                raise StorageSyncUnavailable('storage changes endpoint unavailable')
            if not changes:
                if next_cursor and next_cursor != cursor:
                    with transaction.atomic():
                        _assert_sync_lease_owner(owner)
                        status = StorageSystemStatus.objects.select_for_update().get(id=1)
                        status.sync_cursor = next_cursor
                        status.service_health = 'healthy'
                        status.synced_at = timezone.now()
                        status.save(update_fields=['sync_cursor', 'service_health', 'synced_at', 'updated_at'])
                break
            if _has_retryable_missing_problem(changes):
                logger.info('storage_sync_catalog: retrying later for missing local problem')
                return
            if not next_cursor and has_more:
                raise StorageSyncMalformedChange('storage changes page has has_more without next_cursor')
            if next_cursor == cursor and has_more:
                raise StorageSyncMalformedChange('storage changes cursor did not advance')
            with transaction.atomic():
                _assert_sync_lease_owner(owner)
                status = StorageSystemStatus.objects.select_for_update().get(id=1)
                for change in changes:
                    _apply_sync_change(change)
                if next_cursor:
                    status.sync_cursor = next_cursor
                    status.service_health = 'healthy'
                    status.synced_at = timezone.now()
                    status.save(update_fields=['sync_cursor', 'service_health', 'synced_at', 'updated_at'])
                    cursor = next_cursor
            total_processed += len(changes)
            pages += 1
            if not has_more or not next_cursor:
                break
        else:
            raise StorageSyncMalformedChange('storage sync page limit exceeded')
        logger.info('storage_sync_catalog: processed %d changes', total_processed)
        _rebuild_organization_usage()
        _update_system_status()
    finally:
        _release_sync_lease(owner)


def _system_status():
    status, _ = StorageSystemStatus.objects.get_or_create(id=1)
    return status


def _acquire_sync_lease(owner):
    now = timezone.now()
    expires_at = now + timezone.timedelta(seconds=SYNC_LOCK_TTL)
    with transaction.atomic():
        lease, created = StorageSyncLease.objects.select_for_update().get_or_create(
            name=SYNC_LEASE_NAME,
            defaults={'owner': owner, 'expires_at': expires_at, 'renewed_at': now},
        )
        if created:
            return True
        if lease.expires_at > now and lease.owner != owner:
            return False
        lease.owner = owner
        lease.expires_at = expires_at
        lease.renewed_at = now
        lease.save(update_fields=['owner', 'expires_at', 'renewed_at'])
        return True


def _renew_sync_lease(owner):
    now = timezone.now()
    return StorageSyncLease.objects.filter(name=SYNC_LEASE_NAME, owner=owner).update(
        expires_at=now + timezone.timedelta(seconds=SYNC_LOCK_TTL),
        renewed_at=now,
    ) == 1


def _assert_sync_lease_owner(owner):
    lease = StorageSyncLease.objects.select_for_update().filter(name=SYNC_LEASE_NAME).first()
    if lease is None or lease.owner != owner or lease.expires_at <= timezone.now():
        raise StorageSyncLeaseLost('storage sync lease lost')


def _release_sync_lease(owner):
    StorageSyncLease.objects.filter(name=SYNC_LEASE_NAME, owner=owner).delete()


def _apply_sync_change(change):
    """Upsert a single sync change into StorageProblemUsage."""
    _validate_change(change)
    external_id = str(change.get('external_id') or change.get('problem_pk') or '')
    if not external_id or not _is_local_problem_id(external_id):
        return
    try:
        problem = Problem.objects.get(pk=external_id)
    except Problem.DoesNotExist:
        return
    _resolve_deadletter_for_problem(external_id)

    event_type = (change.get('event_kind') or change.get('event_type') or change.get('type') or '').lower()
    usage, created = StorageProblemUsage.objects.get_or_create(problem=problem)
    if event_type in ('delete', 'deleted', 'tombstone'):
        usage.catalog_state = 'deleted'
        usage.code = change.get('code', usage.code)
        usage.owner_organization_id = _first_present(
            change, ('owner_organization_id', 'owner_organization'), usage.owner_organization_id,
        )
        usage.is_manually_managed = change.get('is_manually_managed', usage.is_manually_managed)
        usage.mirror_of_external_id = _first_present(
            change, ('mirror_of_external_id', 'mirror_of'), usage.mirror_of_external_id,
        )
        usage.mirror_root_external_id = _first_present(
            change, ('mirror_root_external_id', 'mirror_root'), usage.mirror_root_external_id,
        )
        usage.deleted_at = _parse_dt(change.get('deleted_at')) or timezone.now()
        usage.downloadable = False
        usage.stale = False
        usage.save()
        return
    usage.code = change.get('code', usage.code)
    usage.owner_organization_id = _first_present(
        change, ('owner_organization_id', 'owner_organization'), usage.owner_organization_id,
    )
    usage.is_manually_managed = change.get('is_manually_managed', usage.is_manually_managed if not created else False)
    usage.mirror_of_external_id = _first_present(change, ('mirror_of_external_id', 'mirror_of'), usage.mirror_of_external_id)
    usage.mirror_root_external_id = _first_present(
        change, ('mirror_root_external_id', 'mirror_root'), usage.mirror_root_external_id,
    )
    usage.catalog_state = change.get('catalog_state', 'present')
    usage.logical_bytes = change.get('logical_bytes', 0)
    usage.allocated_bytes = change.get('allocated_bytes', 0)
    usage.archive_bytes = change.get('archive_bytes', 0)
    usage.auxiliary_bytes = change.get('auxiliary_bytes', 0)
    usage.file_count = change.get('file_count', 0)
    usage.quota_bytes = change.get('quota_bytes') or change.get('organization_quota_bytes') or usage.quota_bytes or 0
    usage.local_status = change.get('local_status', 'present')
    usage.r2_status = _normalize_r2_status(change.get('r2_status', 'none'))
    usage.snapshot_generation = change.get('snapshot_generation')
    usage.schema_version = change.get('schema_version') or usage.schema_version
    usage.downloadable = _as_bool(change.get('downloadable', usage.r2_status == 'READY'))
    usage.deleted_at = _parse_dt(change.get('deleted_at')) if change.get('deleted_at') else None
    usage.orphan_bytes = change.get('orphan_bytes', 0)
    usage.referenced_bytes = change.get('referenced_bytes', 0)
    usage.observed_at = _parse_dt(change.get('observed_at')) or usage.observed_at
    usage.stale = change.get('stale', False)
    usage.save()


def _first_present(mapping, keys, default=None):
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _validate_change(change):
    if not isinstance(change, dict):
        raise StorageSyncMalformedChange('storage change must be an object')
    if not (change.get('external_id') or change.get('problem_pk')):
        raise StorageSyncMalformedChange('storage change missing external_id/problem_pk')
    if 'schema_version' in change and int(change['schema_version']) != storage_client.expected_schema_version():
        raise StorageSyncMalformedChange('storage change schema version mismatch')


def _normalize_r2_status(value):
    value = str(value or 'none')
    if value.lower() == 'none':
        return 'none'
    return value.upper()


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _parse_dt(value):
    if not value:
        return None
    try:
        from django.utils.dateparse import parse_datetime
        return parse_datetime(value)
    except (ValueError, TypeError):
        return None


def _change_key(change, external_id):
    return str(
        change.get('change_id') or
        change.get('id') or
        change.get('cursor') or
        '%s:%s:%s' % (
            external_id,
            change.get('event_kind') or change.get('event_type') or change.get('type') or 'change',
            change.get('observed_at') or '',
        )
    )


def _record_missing_problem_change(change, external_id):
    key = _change_key(change, external_id)
    now = timezone.now()
    dead, _ = StorageSyncDeadLetter.objects.select_for_update().get_or_create(
        change_key=key,
        defaults={
            'external_id': external_id,
            'reason': 'problem_not_found',
            'payload': change,
            'retry_count': 0,
            'first_seen_at': now,
            'last_seen_at': now,
        },
    )
    dead.retry_count += 1
    dead.last_seen_at = now
    dead.payload = change
    dead.save(update_fields=['retry_count', 'last_seen_at', 'payload'])
    return dead.retry_count


def _has_retryable_missing_problem(changes):
    retry_later = False
    for change in changes:
        external_id = str(change.get('external_id') or change.get('problem_pk') or '')
        if not external_id or not _is_local_problem_id(external_id):
            continue
        if Problem.objects.filter(pk=external_id).exists():
            continue
        with transaction.atomic():
            retry_count = _record_missing_problem_change(change, external_id)
        if retry_count < SYNC_DEADLETTER_RETRIES:
            retry_later = True
    return retry_later


def _is_local_problem_id(value):
    try:
        normalized = str(value).strip()
        parsed = int(normalized)
    except (TypeError, ValueError):
        return False
    return normalized == str(parsed) and 0 < parsed <= 2147483647


def _resolve_deadletter_for_problem(external_id):
    StorageSyncDeadLetter.objects.filter(external_id=str(external_id), resolved_at__isnull=True).update(
        resolved_at=timezone.now(),
    )


def _rebuild_organization_usage():
    """Aggregate StorageProblemUsage per organization."""
    from judge.models import Organization

    now = timezone.now()
    rows = {
        row['owner_organization_id']: row
        for row in StorageProblemUsage.objects.exclude(owner_organization_id__isnull=True).values(
            'owner_organization_id',
        ).annotate(
            total_logical=models.Sum('logical_bytes', filter=models.Q(catalog_state='present')),
            total_allocated=models.Sum('allocated_bytes', filter=models.Q(catalog_state='present')),
            total_archive=models.Sum('archive_bytes', filter=models.Q(catalog_state='present')),
            total_auxiliary=models.Sum('auxiliary_bytes', filter=models.Q(catalog_state='present')),
            total_files=models.Sum('file_count', filter=models.Q(catalog_state='present')),
            count=models.Count('pk', filter=models.Q(catalog_state='present')),
            orphan=models.Sum('orphan_bytes'),
            referenced=models.Sum('referenced_bytes'),
            quota=models.Max('quota_bytes'),
        )
    }
    for org in Organization.objects.filter(pk__in=rows.keys()).iterator():
        agg = rows.get(org.pk, {})
        org_usage, _ = StorageOrganizationUsage.objects.get_or_create(organization_id=org.pk)
        org_usage.total_logical_bytes = agg.get('total_logical') or 0
        org_usage.total_allocated_bytes = agg.get('total_allocated') or 0
        org_usage.total_archive_bytes = agg.get('total_archive') or 0
        org_usage.total_auxiliary_bytes = agg.get('total_auxiliary') or 0
        org_usage.total_file_count = agg.get('total_files') or 0
        org_usage.problem_count = agg.get('count') or 0
        org_usage.quota_bytes = agg.get('quota') or org_usage.quota_bytes or 0
        remote_usage = storage_client.get_organization_usage(str(org.pk))
        remote_fields = _apply_remote_organization_usage(org_usage, remote_usage)
        if 'orphan_bytes' not in remote_fields:
            org_usage.orphan_bytes = agg.get('orphan') or 0
        if 'referenced_bytes' not in remote_fields:
            org_usage.referenced_bytes = agg.get('referenced') or 0
        org_usage.observed_at = now
        org_usage.stale = False
        org_usage.save()
    empty_usage_qs = StorageOrganizationUsage.objects.exclude(organization_id__in=rows.keys())
    empty_usage_qs.update(
        total_logical_bytes=0,
        total_allocated_bytes=0,
        total_archive_bytes=0,
        total_auxiliary_bytes=0,
        total_file_count=0,
        problem_count=0,
        orphan_bytes=0,
        referenced_bytes=0,
        observed_at=now,
        stale=False,
    )
    for org_usage in empty_usage_qs.iterator():
        remote_usage = storage_client.get_organization_usage(str(org_usage.organization_id))
        if not _apply_remote_organization_usage(org_usage, remote_usage):
            continue
        org_usage.save()


def _first_remote_field(remote_usage, *fields):
    for field in fields:
        if field in remote_usage:
            return remote_usage[field], field
    return None, None


def _apply_remote_organization_usage(org_usage, remote_usage):
    if not remote_usage:
        return set()
    applied = set()
    mappings = (
        ('total_logical_bytes', ('logical_bytes', 'total_logical_bytes')),
        ('total_allocated_bytes', ('allocated_bytes', 'total_allocated_bytes')),
        ('total_archive_bytes', ('archive_bytes', 'total_archive_bytes')),
        ('total_auxiliary_bytes', ('auxiliary_bytes', 'total_auxiliary_bytes')),
        ('total_file_count', ('file_count', 'total_file_count')),
        ('problem_count', ('problem_count',)),
        ('quota_bytes', ('quota_bytes',)),
        ('problem_quota', ('problem_count_quota', 'problem_quota', 'problem_limit')),
        ('orphan_bytes', ('orphan_bytes',)),
        ('referenced_bytes', ('referenced_bytes',)),
    )
    for attr, fields in mappings:
        value, field = _first_remote_field(remote_usage, *fields)
        if field is not None:
            setattr(org_usage, attr, value or 0)
            applied.add(attr)
            applied.add(field)
    return applied


def _update_system_status():
    """Update StorageSystemStatus from volume metrics + aggregation."""
    status, _ = StorageSystemStatus.objects.get_or_create(id=1)
    volumes = storage_client.get_storage_volumes()
    if volumes:
        v = volumes[0] if isinstance(volumes, list) else volumes
        status.volume_total_bytes = v.get('total_bytes', 0)
        status.volume_free_bytes = v.get('free_bytes', 0)
        status.volume_available_bytes = v.get('available_bytes', 0)
        status.service_health = v.get('service_health') or v.get('health') or 'healthy'
    present = StorageProblemUsage.objects.filter(catalog_state='present')
    agg = present.aggregate(
        total_logical=models.Sum('logical_bytes'),
        total_allocated=models.Sum('allocated_bytes'),
        total_archive=models.Sum('archive_bytes'),
        total_auxiliary=models.Sum('auxiliary_bytes'),
        total_files=models.Sum('file_count'),
        count=models.Count('pk'),
        orphan_bytes=models.Sum('orphan_bytes'),
    )
    status.total_logical_bytes = agg.get('total_logical') or 0
    status.total_allocated_bytes = agg.get('total_allocated') or 0
    status.total_archive_bytes = agg.get('total_archive') or 0
    status.total_auxiliary_bytes = agg.get('total_auxiliary') or 0
    status.total_file_count = agg.get('total_files') or 0
    status.total_problem_count = agg.get('count') or 0
    status.orphan_bytes = agg.get('orphan_bytes') or 0
    status.orphan_count = StorageProblemUsage.objects.filter(catalog_state='orphan').count()
    status.observed_at = timezone.now()
    status.synced_at = timezone.now()
    status.stale = False
    status.save()


@shared_task(name='storage_pull_changes')
def storage_pull_changes():
    """Alias for storage_sync_catalog — explicit pull task for beat schedule."""
    return storage_sync_catalog()


@shared_task(name='storage_full_reconcile')
def storage_full_reconcile():
    """Trigger full reconciliation on the storage app side."""
    problems = Problem.objects.order_by('pk').prefetch_related('organizations').select_related(
        'storage_owner_organization', 'mirror_of', 'mirror_root',
    ).iterator(chunk_size=500)
    result = storage_client.reconcile_catalog(problems=problems)
    if result is None:
        storage_mark_stale()
    return result


@shared_task(name='storage_mark_stale')
def storage_mark_stale():
    """Mark all projections as stale (used when storage app is unreachable)."""
    StorageProblemUsage.objects.filter(stale=False).update(stale=True)
    StorageOrganizationUsage.objects.filter(stale=False).update(stale=True)
    StorageSystemStatus.objects.filter(id=1).update(stale=True, service_health='error')


@shared_task(name='storage_retry_judge_submission')
def storage_retry_judge_submission(submission_id, attempt=1, rejudge=False, judge_id=None, batch_rejudge=False):
    """Retry judge dispatch after storage restore progresses without blocking the request thread."""
    from django.core.cache import cache
    from judge.models import Submission

    cache.delete('storage:ensure-ready:submission:%s' % submission_id)
    try:
        submission = Submission.objects.select_related('problem', 'problem__mirror_root', 'language').get(pk=submission_id)
    except Submission.DoesNotExist:
        return
    if submission.status not in Submission.IN_PROGRESS_GRADING_STATUS:
        return
    kwargs = {}
    if judge_id is not None:
        kwargs['judge_id'] = judge_id
    if batch_rejudge:
        kwargs['batch_rejudge'] = batch_rejudge
    submission.judge(rejudge=rejudge, force_judge=True, ensure_ready_attempt=attempt, **kwargs)
