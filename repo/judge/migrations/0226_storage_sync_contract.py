from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def backfill_storage_owner_organization(apps, schema_editor):
    Problem = apps.get_model('judge', 'Problem')
    through = Problem.organizations.through

    singles = (
        through.objects
        .values('problem_id')
        .annotate(count=models.Count('organization_id'), org_id=models.Min('organization_id'))
        .filter(count=1)
    )
    for row in singles.iterator():
        Problem.objects.filter(
            id=row['problem_id'],
            storage_owner_organization_id__isnull=True,
        ).update(storage_owner_organization_id=row['org_id'])


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0225_storage_projection_models'),
    ]

    operations = [
        migrations.AddField(
            model_name='storageproblemusage',
            name='deleted_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='storageproblemusage',
            name='downloadable',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name='storageproblemusage',
            name='schema_version',
            field=models.IntegerField(default=1),
        ),
        migrations.AddField(
            model_name='storageproblemusage',
            name='quota_bytes',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='storagesystemstatus',
            name='sync_cursor',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='storageorganizationusage',
            name='quota_bytes',
            field=models.BigIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='storagesystemstatus',
            name='service_health',
            field=models.CharField(default='unknown', max_length=32),
        ),
        migrations.AddField(
            model_name='storagesystemstatus',
            name='synced_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name='StorageSyncDeadLetter',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('change_key', models.CharField(max_length=255, unique=True)),
                ('external_id', models.CharField(blank=True, default='', max_length=64)),
                ('reason', models.CharField(max_length=255)),
                ('payload', models.JSONField(default=dict)),
                ('retry_count', models.IntegerField(default=0)),
                ('first_seen_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('last_seen_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('resolved_at', models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.CreateModel(
            name='StorageSyncLease',
            fields=[
                ('name', models.CharField(max_length=64, primary_key=True, serialize=False)),
                ('owner', models.CharField(max_length=64)),
                ('expires_at', models.DateTimeField(db_index=True)),
                ('renewed_at', models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
        migrations.AddIndex(
            model_name='storageproblemusage',
            index=models.Index(fields=['downloadable', 'r2_status'], name='judge_stora_downloa_0fd4ad_idx'),
        ),
        migrations.AddIndex(
            model_name='storagesyncdeadletter',
            index=models.Index(fields=['external_id', 'resolved_at'], name='judge_stora_externa_bdef18_idx'),
        ),
        migrations.RunPython(backfill_storage_owner_organization, migrations.RunPython.noop),
    ]
