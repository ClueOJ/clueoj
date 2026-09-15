import logging

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Max, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, TemplateView

from judge.models.storage import (
    StorageEvictionRule, StorageOrganizationUsage, StorageProblemUsage,
    StorageSystemStatus,
)
from judge.tasks.storage import (
    _eviction_candidate_queryset, storage_apply_eviction_rules, storage_evict_problem,
    storage_full_reconcile, storage_sync_after_restore, storage_sync_catalog,
)
from judge.utils import storage_client
from judge.utils.views import DiggPaginatorMixin, TitleMixin

logger = logging.getLogger('judge.views.storage_admin')

BULK_EVICT_LIMIT = 200
BULK_RESTORE_LIMIT = 200
SCHEDULE_PREVIEW_LIMIT = 100

ACTION_MESSAGES = {
    'sync': _('Catalog sync queued.'),
    'reconcile': _('Full reconciliation queued.'),
    'apply': _('Clear rules applied.'),
    'evict': _('Local clear queued for problem.'),
    'evict_bulk': _('Local clear queued for selected problems.'),
    'evict_in_progress': _('A local clear is already queued for this problem.'),
    'restore_bulk': _('Bulk restore from R2 queued.'),
    'restore_ready': _('Problem is already available locally.'),
    'restore_in_progress': _('A restore from R2 is already queued for this problem.'),
    'restore_unavailable': _('Restore from R2 could not be queued.'),
    'add_rule': _('Clear rule created.'),
    'delete_rule': _('Clear rule deleted.'),
    'toggle_rule': _('Clear rule updated.'),
}


def _format_bytes(value):
    value = int(value or 0)
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return ('%.1f %s' % (size, unit)) if unit != 'B' else ('%d B' % value)
        size /= 1024


def _live_summary_value(live_summary, key, fallback):
    """Use a live dashboard field when present; otherwise the projection.

    Older storage apps omit newer keys. Explicit null also falls back.
    Auth/HTTP failures are not passed in as a dict — they propagate.
    """
    if not isinstance(live_summary, dict) or key not in live_summary:
        return fallback
    value = live_summary[key]
    return fallback if value is None else value


class StorageAdminOverview(LoginRequiredMixin, DiggPaginatorMixin, TitleMixin, ListView):
    """System-wide storage overview for superusers.

    Shows global accounting, volume health, organization rollups, the admin
    clear-rule table, per-problem clear actions, and the recent eviction log.
    """
    template_name = 'status/storage-admin.html'
    context_object_name = 'usages'
    paginate_by = 50
    PAGE_SIZE_CHOICES = (50, 100, 200, 500)
    title = _('Storage overview')

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise Http404()
        return super(StorageAdminOverview, self).dispatch(request, *args, **kwargs)

    def get_paginate_by(self, queryset):
        try:
            limit = int(self.request.GET.get('limit') or self.paginate_by)
        except (TypeError, ValueError):
            return self.paginate_by
        return limit if limit in self.PAGE_SIZE_CHOICES else self.paginate_by

    def _redirect(self, action):
        url = reverse('status_storage')
        section = self.request.POST.get('section') or ''
        params = []
        if section in self.SECTIONS:
            params.append('section=%s' % section)
        if action:
            params.append('done=%s' % action)
        if params:
            url += '?' + '&'.join(params)
        return HttpResponseRedirect(url)

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        if action == 'sync':
            storage_sync_catalog.delay()
        elif action == 'reconcile':
            storage_full_reconcile.delay()
        elif action == 'apply':
            rule_id = request.POST.get('rule_id')
            storage_apply_eviction_rules.delay(int(rule_id) if rule_id else None)
        elif action == 'evict':
            problem_id = request.POST.get('problem_id')
            if problem_id and problem_id.isdigit():
                if self._evict_lock_map().get(str(problem_id)):
                    action = 'evict_in_progress'
                else:
                    storage_evict_problem.delay(int(problem_id))
            else:
                return self._redirect(None)
        elif action == 'restore':
            problem_id = request.POST.get('problem_id')
            usage = StorageProblemUsage.objects.filter(
                problem_id=problem_id,
                catalog_state='present',
                local_status='missing',
                r2_status__iexact='ready',
            ).first() if problem_id and problem_id.isdigit() else None
            if usage and self._restore_lock_map().get(str(usage.problem_id)):
                # This problem was already pulled from R2 and has not been
                # cleared again; do not issue another ensure-ready call.
                action = 'restore_in_progress'
            elif usage:
                result = storage_client.ensure_problem_ready(str(usage.problem_id))
                if result.get('ready') is True:
                    action = 'restore_ready'
                    # The data is on disk but the catalog may not reflect it
                    # yet; the immediate sync below can race the storage
                    # app's own catalog update. Schedule a follow-up sync a
                    # few seconds later to catch the projection up.
                    storage_sync_catalog.apply_async(countdown=3)
                elif result.get('state') == storage_client.READY_STATE_RESTORING:
                    action = 'restore'
                    if result.get('job_id'):
                        # The immediate sync below races the restore job; poll
                        # it to terminal and sync again once it settles.
                        storage_sync_after_restore.delay(result['job_id'])
                else:
                    action = 'restore_unavailable'
                storage_sync_catalog.delay()
            else:
                action = 'restore_unavailable'
        elif action == 'restore_bulk':
            problem_ids = [pid for pid in request.POST.getlist('problem_ids') if pid.isdigit()]
            locks = self._restore_lock_map()
            restorable = StorageProblemUsage.objects.filter(
                problem_id__in=problem_ids[:BULK_RESTORE_LIMIT],
                catalog_state='present',
                local_status='missing',
                r2_status__iexact='ready',
            ).values_list('problem_id', flat=True)
            queued = 0
            for pid in restorable:
                if locks.get(str(pid)):
                    continue
                result = storage_client.ensure_problem_ready(str(pid))
                if result.get('ready') is True or result.get('state') == storage_client.READY_STATE_RESTORING:
                    queued += 1
                    if result.get('job_id'):
                        storage_sync_after_restore.delay(result['job_id'])
            if queued:
                storage_sync_catalog.delay()
        elif action == 'evict_bulk':
            problem_ids = [pid for pid in request.POST.getlist('problem_ids') if pid.isdigit()]
            clearable = StorageProblemUsage.objects.filter(
                problem_id__in=problem_ids[:BULK_EVICT_LIMIT],
                catalog_state='present',
                local_status='present',
                r2_status__iexact='ready',
            ).values_list('problem_id', flat=True)
            for pid in clearable:
                storage_evict_problem.delay(int(pid))
        elif action == 'add_rule':
            name = (request.POST.get('name') or '').strip()
            idle_hours = request.POST.get('idle_hours')
            max_size_mb = request.POST.get('max_size_mb')
            if name:
                StorageEvictionRule.objects.create(
                    name=name[:100],
                    idle_hours=max(1, int(idle_hours or 24)),
                    max_size_bytes=(int(float(max_size_mb) * 1024 * 1024)
                                    if max_size_mb not in (None, '', '0') else None),
                )
        elif action == 'delete_rule':
            StorageEvictionRule.objects.filter(pk=request.POST.get('rule_id')).delete()
        elif action == 'toggle_rule':
            rule = StorageEvictionRule.objects.filter(pk=request.POST.get('rule_id')).first()
            if rule:
                rule.enabled = not rule.enabled
                rule.save(update_fields=['enabled', 'updated_at'])
        else:
            return self._redirect(None)
        return self._redirect(action)

    LOG_ACTIONS = (
        'problem.evict', 'problem.restore', 'problem.snapshot', 'problem.scan',
        'problem.dirty', 'problem.deleted', 'auto-scan', 'auto-snapshot',
        'ensure-ready-restore', 'ensure-ready-snapshot', 'scan', 'snapshot',
        'restore', 'evict', 'gc_collect', 'scheduled-gc', 'backfill', 'backfill-page',
    )
    LOG_PAGE_SIZE = 50

    def _app_logs(self):
        """Live audit log read straight from the storage app on every request.

        Nothing is persisted to the OJ database — the audit feed lives in the
        storage app and this projection is rendered and thrown away.
        """
        from urllib.parse import urlencode

        from django.utils.dateparse import parse_datetime

        action = (self.request.GET.get('action') or '').strip() or None
        problem = (self.request.GET.get('problem') or '').strip()
        problem_id = problem if problem.isdigit() else None
        cursor = self.request.GET.get('cursor') or None
        items, next_cursor, has_more = storage_client.get_audit_events(
            action=action, problem_id=problem_id, cursor=cursor, limit=self.LOG_PAGE_SIZE,
        )
        items = items or []
        def _pid(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        ids = [pid for pid in (_pid(i.get('problem_id')) for i in items) if pid is not None]
        codes = dict(
            StorageProblemUsage.objects.filter(problem_id__in=ids).values_list('problem_id', 'code')
        )
        rows = []
        for item in items:
            metadata = item.get('metadata') or {}
            parts = []
            for key in sorted(metadata):
                value = metadata.get(key)
                if isinstance(value, bool):
                    value = 'yes' if value else 'no'
                elif isinstance(value, int) and key.endswith('bytes'):
                    value = _format_bytes(value)
                parts.append('%s: %s' % (key, value))
            created = item.get('created_at')
            if isinstance(created, str):
                created = parse_datetime(created.replace('Z', '+00:00')) or created
            rows.append({
                'created_at': created,
                'action': item.get('action'),
                'problem_id': item.get('problem_id'),
                'code': codes.get(_pid(item.get('problem_id'))),
                'actor': item.get('actor'),
                'detail': ' · '.join(parts),
            })
        filters = {'section': 'logs'}
        if action:
            filters['action'] = action
        if problem:
            filters['problem'] = problem
        older_href = None
        if has_more and next_cursor:
            older_href = '?' + urlencode(dict(filters, cursor=next_cursor))
        return {
            'rows': rows,
            'older_href': older_href,
            'newest_href': '?' + urlencode(filters),
            'action': action or '',
            'problem': problem,
            'log_actions': self.LOG_ACTIONS,
        }

    def _restore_lock_map(self):
        """Map problem_id -> locked, straight from the storage job history.

        The newest restore/evict job per problem tells whether its local
        copy exists: a restore that is pending, running or completed means
        the data is on its way back or already restored, so the manual
        restore stays locked until an evict clears the folder again. This
        does not depend on the 5-minute projection sync, so the lock is
        correct immediately after a restore finishes.
        """
        locked = {}
        for job in storage_client.get_recent_jobs() or []:
            if job.get('job_type') not in ('restore', 'evict'):
                continue
            pid = str(job.get('problem_id') or '')
            if not pid or pid in locked:
                continue  # the newest job per problem decides
            locked[pid] = (
                job.get('job_type') == 'restore'
                and job.get('state') in ('pending', 'running', 'completed')
            )
        return locked


    def _evict_lock_map(self):
        """Map problem_id -> locked, the mirror of _restore_lock_map.

        The newest evict/restore job per problem decides: an evict that is
        pending, running or completed means the local copy is gone (or on
        its way out), so the manual clear stays locked until a restore
        brings the data back. This does not depend on the 5-minute
        projection sync, so the lock is correct immediately after an
        evict finishes — preventing redundant evict jobs from repeated
        clicks while the projection still shows local_status=present.
        """
        locked = {}
        for job in storage_client.get_recent_jobs() or []:
            if job.get('job_type') not in ('restore', 'evict'):
                continue
            pid = str(job.get('problem_id') or '')
            if not pid or pid in locked:
                continue  # the newest job per problem decides
            locked[pid] = (
                job.get('job_type') == 'evict'
                and job.get('state') in ('pending', 'running', 'completed')
            )
        return locked

    def _queue_rows(self):
        """Live pending/running storage jobs grouped by action for the queue tab."""
        from django.utils.dateparse import parse_datetime

        active = storage_client.get_active_jobs() or []

        def _pid(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        ids = [pid for pid in (_pid(job.get('problem_id')) for job in active) if pid is not None]
        codes = dict(
            StorageProblemUsage.objects.filter(problem_id__in=ids).values_list('problem_id', 'code')
        )

        def _row(job):
            created = job.get('created_at')
            if isinstance(created, str):
                created = parse_datetime(created.replace('Z', '+00:00')) or created
            return {
                'job_id': str(job.get('id') or '')[:8],
                'code': codes.get(_pid(job.get('problem_id'))),
                'state': job.get('state'),
                'created_at': created,
                'attempt': job.get('attempt'),
            }

        groups = {
            'restores': [ _row(job) for job in active if job.get('job_type') == 'restore' ],
            'evictions': [ _row(job) for job in active if job.get('job_type') == 'evict' ],
            'uploads': [ _row(job) for job in active if job.get('job_type') in ('scan', 'snapshot') ],
        }
        return {'groups': groups, 'total': len(active)}

    def get_queryset(self):
        queryset = StorageProblemUsage.objects.select_related('problem').annotate(
            last_submission=Max('problem__submission__date'),
        ).order_by('-allocated_bytes')
        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(Q(code__icontains=search) | Q(problem__name__icontains=search))
        local_status = self.request.GET.get('local_status')
        if local_status:
            queryset = queryset.filter(local_status=local_status)
        r2_status = self.request.GET.get('r2_status')
        if r2_status == 'no_ready':
            queryset = queryset.exclude(r2_status__iexact='ready')
        elif r2_status:
            queryset = queryset.filter(r2_status__iexact=r2_status)
        return queryset

    def _rule_candidates(self, idle_hours, max_bytes=None, limit=SCHEDULE_PREVIEW_LIMIT):
        """Problems a rule will clear, with the time each becomes eligible.

        Includes problems that are not idle enough yet, so admins can see the
        upcoming schedule instead of only already-eligible problems. Problems
        with recent/active submissions or grading are never eligible and stay
        hidden until they go quiet.
        """
        now = timezone.now()
        window = timezone.timedelta(hours=max(1, idle_hours))
        candidates = (
            _eviction_candidate_queryset(now, max_bytes=max_bytes)
            .annotate(last_submission=Max('problem__submission__date'))
            .order_by('local_ready_at', 'problem_id')[:limit]
        )
        rows = []
        for usage in candidates:
            idle_since = usage.local_ready_at
            if usage.last_submission and usage.last_submission > idle_since:
                idle_since = usage.last_submission
            eligible_at = idle_since + window
            rows.append({
                'problem_id': usage.problem_id,
                'code': usage.code,
                'allocated_label': _format_bytes(usage.allocated_bytes),
                'last_submission': usage.last_submission,
                'idle_since': idle_since,
                'eligible_at': eligible_at,
                'eligible_now': eligible_at <= now,
            })
        rows.sort(key=lambda row: row['eligible_at'])
        return rows

    def _rules_with_counts(self):
        rules = []
        for rule in StorageEvictionRule.objects.all():
            schedule = self._rule_candidates(rule.idle_hours, rule.max_size_bytes)
            rules.append({
                'rule': rule,
                'candidate_count': len(schedule),
                'max_size_label': _format_bytes(rule.max_size_bytes) if rule.max_size_bytes else None,
                'sample': schedule[:3],
                'schedule': schedule,
            })
        return rules

    def _passive_sweep_schedule(self):
        """The always-on passive sweep, shown like a built-in rule."""
        if not getattr(settings, 'STORAGE_LOCAL_EVICTION_ENABLED', False):
            return None
        idle_hours = max(1, int(getattr(settings, 'STORAGE_LOCAL_EVICTION_IDLE_HOURS', 24)))
        return {
            'name': _('Passive sweep'),
            'idle_hours': idle_hours,
            'enabled': True,
            'schedule': self._rule_candidates(idle_hours),
        }


    SECTIONS = ('overview', 'rules', 'problems', 'queue', 'logs')

    def get_context_data(self, **kwargs):
        context = super(StorageAdminOverview, self).get_context_data(**kwargs)
        section = self.request.GET.get('section') or 'overview'
        if section not in self.SECTIONS:
            section = 'overview'
        status, _created = StorageSystemStatus.objects.get_or_create(id=1)

        context_data = {
            'title': _('Storage overview'),
            'content_title': _('Storage overview'),
            'section': section,
            'status': status,
            'done_action': self.request.GET.get('done'),
            'done_message': ACTION_MESSAGES.get(self.request.GET.get('done')),
            'filters': self.request.GET,
            'format_bytes': _format_bytes,
        }

        mirror_root_ids = {
            str(root_id) for root_id in (
                (usage.mirror_root_external_id or usage.mirror_of_external_id)
                for usage in context['usages']
            ) if root_id
        }
        mirror_root_codes = dict(
            StorageProblemUsage.objects
            .filter(problem_id__in=mirror_root_ids)
            .values_list('problem_id', 'code')
        ) if mirror_root_ids else {}
        for usage in context['usages']:
            usage.allocated_label = _format_bytes(usage.allocated_bytes)
            usage.logical_label = _format_bytes(usage.logical_bytes)
            usage.r2_status_normalized = (usage.r2_status or '').upper()
            usage.mirror_root_code = mirror_root_codes.get(
                int(usage.mirror_root_external_id or usage.mirror_of_external_id or 0)
            )
            usage.clearable = (
                usage.catalog_state == 'present' and usage.local_status == 'present'
                and usage.r2_status_normalized == 'READY'
            )
            usage.restoreable = (
                usage.catalog_state == 'present' and usage.local_status == 'missing'
                and usage.r2_status_normalized == 'READY'
            )

        if section == 'problems':
            restore_locks = self._restore_lock_map()
            evict_locks = self._evict_lock_map()
            for usage in context['usages']:
                usage.restore_locked = restore_locks.get(str(usage.problem_id), False)
                usage.evict_locked = evict_locks.get(str(usage.problem_id), False)

        if section == 'overview':
            active = StorageProblemUsage.objects.filter(catalog_state__in=('present', 'mirror'))
            aggregates = active.aggregate(
                total_logical=Sum('logical_bytes'),
                total_files=Sum('file_count'),
                problem_count=Count('pk'),
            )
            total_problem_count = aggregates['problem_count'] or 0
            r2_stats = active.filter(r2_status__iexact='ready').aggregate(
                archive=Sum('archive_bytes'), count=Count('pk'),
            )
            local_stats = active.filter(local_status='present').aggregate(
                allocated=Sum('allocated_bytes'), count=Count('pk'),
            )
            live_summary = storage_client.get_dashboard_summary()
            r2_total_bytes = _live_summary_value(
                live_summary, 'r2_snapshot_bytes', r2_stats['archive'] or 0,
            )
            r2_problem_count = _live_summary_value(
                live_summary, 'r2_snapshot_problem_count', r2_stats['count'] or 0,
            )
            local_total_bytes = _live_summary_value(
                live_summary, 'local_allocated_bytes', local_stats['allocated'] or 0,
            )
            local_problem_count = _live_summary_value(
                live_summary, 'local_problem_count', local_stats['count'] or 0,
            )
            total_problem_count = _live_summary_value(
                live_summary, 'active_problem_count', total_problem_count,
            )
            missing_backup_count = max(0, (total_problem_count or 0) - (r2_problem_count or 0))
            volume_total = status.volume_total_bytes or 0
            volume_used = max(0, volume_total - (status.volume_free_bytes or 0))
            org_rows = [
                {
                    'organization': row.organization,
                    'problem_count': row.problem_count,
                    'logical_label': _format_bytes(row.total_logical_bytes),
                    'allocated_label': _format_bytes(row.total_allocated_bytes),
                }
                for row in (
                    StorageOrganizationUsage.objects.select_related('organization')
                    .order_by('-total_allocated_bytes')[:20]
                )
            ]
            context_data.update({
                'total_problem_count': total_problem_count,
                'missing_backup_count': missing_backup_count,
                'r2_snapshot_label': _format_bytes(r2_total_bytes),
                'r2_problems': r2_problem_count or 0,
                'local_folder_label': _format_bytes(local_total_bytes),
                'local_problems': local_problem_count or 0,
                'volume_total_label': _format_bytes(volume_total),
                'volume_used_label': _format_bytes(volume_used),
                'volume_percent': int(volume_used * 100 / volume_total) if volume_total else 0,
                'org_rows': org_rows,
            })
        elif section == 'rules':
            rules = self._rules_with_counts()
            passive_sweep = self._passive_sweep_schedule()
            context_data.update({
                'rules': rules,
                'passive_sweep': passive_sweep,
                'scheduled_total': sum(
                    len(entry['schedule'])
                    for entry in rules if entry['rule'].enabled
                ) + (len(passive_sweep['schedule']) if passive_sweep else 0),
                'rule_sweep_seconds': int(getattr(settings, 'STORAGE_RULE_SWEEP_SECONDS', 3600)),
            })
        elif section == 'problems':
            params = self.request.GET.copy()
            params.pop('page', None)
            params['section'] = 'problems'
            context_data.update({
                'problems_page_prefix': '?%s&page=' % params.urlencode(),
                'limit': self.get_paginate_by(None),
                'page_size_choices': self.PAGE_SIZE_CHOICES,
            })
        elif section == 'queue':
            context_data['queue'] = self._queue_rows()
        elif section == 'logs':
            context_data['logs'] = self._app_logs()

        context.update(context_data)
        return context


class StorageAdminOrganizations(LoginRequiredMixin, TitleMixin, TemplateView):
    """Per-organization storage usage statistics for superusers."""
    template_name = 'status/storage-admin-orgs.html'
    title = _('Organization storage')

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise Http404()
        return super(StorageAdminOrganizations, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(StorageAdminOrganizations, self).get_context_data(**kwargs)
        usages = list(
            StorageOrganizationUsage.objects
            .filter(problem_count__gt=0)
            .select_related('organization')
            .order_by('-total_allocated_bytes')
        )
        org_rows = [
            {
                'organization': row.organization,
                'problem_count': row.problem_count,
                'file_count': row.total_file_count,
                'logical_label': _format_bytes(row.total_logical_bytes),
                'allocated_label': _format_bytes(row.total_allocated_bytes),
                'archive_label': _format_bytes(row.total_archive_bytes),
                'orphan_label': _format_bytes(row.orphan_bytes),
                'observed_at': row.observed_at,
            }
            for row in usages
        ]
        unassigned = StorageProblemUsage.objects.filter(
            catalog_state='present', owner_organization_id__isnull=True,
        ).aggregate(
            count=Count('pk'),
            allocated=Sum('allocated_bytes'),
            logical=Sum('logical_bytes'),
        )
        totals = StorageOrganizationUsage.objects.filter(problem_count__gt=0).aggregate(
            problems=Sum('problem_count'),
            logical=Sum('total_logical_bytes'),
            allocated=Sum('total_allocated_bytes'),
            archive=Sum('total_archive_bytes'),
        )
        context.update({
            'title': _('Organization storage'),
            'content_title': _('Organization storage'),
            'org_rows': org_rows,
            'org_count': len(org_rows),
            'total_problems': totals['problems'] or 0,
            'total_logical_label': _format_bytes(totals['logical']),
            'total_allocated_label': _format_bytes(totals['allocated']),
            'total_archive_label': _format_bytes(totals['archive']),
            'unassigned_problems': unassigned['count'] or 0,
            'unassigned_allocated_label': _format_bytes(unassigned['allocated']),
            'format_bytes': _format_bytes,
        })
        return context
