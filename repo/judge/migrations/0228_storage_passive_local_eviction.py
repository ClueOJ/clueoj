from django.db import migrations, models
from django.utils import timezone


def initialize_local_ready_at(apps, schema_editor):
    StorageProblemUsage = apps.get_model('judge', 'StorageProblemUsage')
    StorageProblemUsage.objects.filter(local_status='present', local_ready_at__isnull=True).update(
        local_ready_at=timezone.now(),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0227_storage_org_problem_quota'),
    ]

    operations = [
        migrations.AddField(
            model_name='storageproblemusage',
            name='local_ready_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddIndex(
            model_name='submission',
            index=models.Index(
                fields=['problem', '-date'],
                name='judge_sub_problem_date_idx',
            ),
        ),
        # Existing local copies get a fresh 24-hour grace period when the
        # feature is installed instead of being evicted immediately.
        migrations.RunPython(initialize_local_ready_at, migrations.RunPython.noop),
    ]
