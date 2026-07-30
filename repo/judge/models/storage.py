from django.db import models
from django.utils import timezone


class StorageProblemUsage(models.Model):
    """Projection of storage app's problem usage data.

    Populated by Celery sync tasks pulling from the storage app's /api/v1/sync/changes.
    ClueOJ queries this local table — NOT the storage app API — for page rendering.
    """
    problem = models.OneToOneField(
        'Problem',
        primary_key=True,
        on_delete=models.CASCADE,
        related_name='storage_usage',
    )
    code = models.CharField(max_length=32, db_index=True)
    owner_organization_id = models.IntegerField(null=True, blank=True, db_index=True)
    is_manually_managed = models.BooleanField(default=False)
    mirror_of_external_id = models.CharField(max_length=64, blank=True, null=True)
    mirror_root_external_id = models.CharField(max_length=64, blank=True, null=True)
    catalog_state = models.CharField(max_length=20, default='present')

    logical_bytes = models.BigIntegerField(default=0)
    allocated_bytes = models.BigIntegerField(default=0)
    archive_bytes = models.BigIntegerField(default=0)
    auxiliary_bytes = models.BigIntegerField(default=0)
    file_count = models.IntegerField(default=0)
    quota_bytes = models.BigIntegerField(default=0)

    local_status = models.CharField(max_length=20, default='present')
    r2_status = models.CharField(max_length=20, default='none')
    snapshot_generation = models.IntegerField(null=True, blank=True)
    schema_version = models.IntegerField(default=1)
    downloadable = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    orphan_bytes = models.BigIntegerField(default=0)
    referenced_bytes = models.BigIntegerField(default=0)

    observed_at = models.DateTimeField(null=True, blank=True)
    stale = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=['owner_organization_id'], name='judge_stora_owner_o_8d6d7e_idx'),
            models.Index(fields=['catalog_state'], name='judge_stora_catalog_4c4a30_idx'),
            models.Index(fields=['r2_status'], name='judge_stora_r2_stat_c90714_idx'),
            models.Index(fields=['downloadable', 'r2_status'], name='judge_stora_downloa_0fd4ad_idx'),
        ]


class StorageOrganizationUsage(models.Model):
    """Aggregated storage usage per organization.

    Populated by sync tasks from StorageProblemUsage aggregation.
    """
    organization = models.OneToOneField(
        'Organization',
        primary_key=True,
        on_delete=models.CASCADE,
        related_name='storage_usage',
    )
    total_logical_bytes = models.BigIntegerField(default=0)
    total_allocated_bytes = models.BigIntegerField(default=0)
    total_archive_bytes = models.BigIntegerField(default=0)
    total_auxiliary_bytes = models.BigIntegerField(default=0)
    total_file_count = models.IntegerField(default=0)
    problem_count = models.IntegerField(default=0)
    quota_bytes = models.BigIntegerField(default=0)
    problem_quota = models.IntegerField(default=0)
    orphan_bytes = models.BigIntegerField(default=0)
    referenced_bytes = models.BigIntegerField(default=0)
    observed_at = models.DateTimeField(null=True, blank=True)
    stale = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)


class StorageSystemStatus(models.Model):
    """Singleton row storing overall storage system health + volume metrics."""
    id = models.IntegerField(primary_key=True, default=1)
    sync_cursor = models.CharField(max_length=255, blank=True, default='')
    service_health = models.CharField(max_length=32, default='unknown')
    volume_total_bytes = models.BigIntegerField(default=0)
    volume_free_bytes = models.BigIntegerField(default=0)
    volume_available_bytes = models.BigIntegerField(default=0)
    total_logical_bytes = models.BigIntegerField(default=0)
    total_allocated_bytes = models.BigIntegerField(default=0)
    total_archive_bytes = models.BigIntegerField(default=0)
    total_auxiliary_bytes = models.BigIntegerField(default=0)
    total_file_count = models.IntegerField(default=0)
    total_problem_count = models.IntegerField(default=0)
    orphan_count = models.IntegerField(default=0)
    orphan_bytes = models.BigIntegerField(default=0)
    stale = models.BooleanField(default=True)
    observed_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if self.id != 1:
            self.id = 1
        super().save(*args, **kwargs)


class StorageSyncLease(models.Model):
    """Durable singleton lease for catalog sync tasks."""
    name = models.CharField(max_length=64, primary_key=True)
    owner = models.CharField(max_length=64)
    expires_at = models.DateTimeField(db_index=True)
    renewed_at = models.DateTimeField(default=timezone.now)

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()


class StorageSyncDeadLetter(models.Model):
    """Change-feed item that could not be applied after bounded retries."""
    id = models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')
    change_key = models.CharField(max_length=255, unique=True)
    external_id = models.CharField(max_length=64, blank=True, default='')
    reason = models.CharField(max_length=255)
    payload = models.JSONField(default=dict)
    retry_count = models.IntegerField(default=0)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['external_id', 'resolved_at'], name='judge_stora_externa_bdef18_idx'),
        ]
