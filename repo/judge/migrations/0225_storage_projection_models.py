from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0224_storage_owner_organization'),
    ]

    operations = [
        migrations.CreateModel(
            name='StorageProblemUsage',
            fields=[
                ('problem', models.OneToOneField(primary_key=True, on_delete=models.CASCADE, related_name='storage_usage', serialize=False, to='judge.problem')),
                ('code', models.CharField(db_index=True, max_length=32)),
                ('owner_organization_id', models.IntegerField(blank=True, db_index=True, null=True)),
                ('is_manually_managed', models.BooleanField(default=False)),
                ('mirror_of_external_id', models.CharField(blank=True, max_length=64, null=True)),
                ('mirror_root_external_id', models.CharField(blank=True, max_length=64, null=True)),
                ('catalog_state', models.CharField(default='present', max_length=20)),
                ('logical_bytes', models.BigIntegerField(default=0)),
                ('allocated_bytes', models.BigIntegerField(default=0)),
                ('archive_bytes', models.BigIntegerField(default=0)),
                ('auxiliary_bytes', models.BigIntegerField(default=0)),
                ('file_count', models.IntegerField(default=0)),
                ('local_status', models.CharField(default='present', max_length=20)),
                ('r2_status', models.CharField(default='none', max_length=20)),
                ('snapshot_generation', models.IntegerField(blank=True, null=True)),
                ('orphan_bytes', models.BigIntegerField(default=0)),
                ('referenced_bytes', models.BigIntegerField(default=0)),
                ('observed_at', models.DateTimeField(blank=True, null=True)),
                ('stale', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'indexes': [
                    models.Index(fields=['owner_organization_id']),
                    models.Index(fields=['catalog_state']),
                    models.Index(fields=['r2_status']),
                ],
            },
        ),
        migrations.CreateModel(
            name='StorageOrganizationUsage',
            fields=[
                ('organization', models.OneToOneField(primary_key=True, on_delete=models.CASCADE, related_name='storage_usage', serialize=False, to='judge.organization')),
                ('total_logical_bytes', models.BigIntegerField(default=0)),
                ('total_allocated_bytes', models.BigIntegerField(default=0)),
                ('total_archive_bytes', models.BigIntegerField(default=0)),
                ('total_auxiliary_bytes', models.BigIntegerField(default=0)),
                ('total_file_count', models.IntegerField(default=0)),
                ('problem_count', models.IntegerField(default=0)),
                ('orphan_bytes', models.BigIntegerField(default=0)),
                ('referenced_bytes', models.BigIntegerField(default=0)),
                ('observed_at', models.DateTimeField(blank=True, null=True)),
                ('stale', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.CreateModel(
            name='StorageSystemStatus',
            fields=[
                ('id', models.IntegerField(default=1, primary_key=True, serialize=False)),
                ('volume_total_bytes', models.BigIntegerField(default=0)),
                ('volume_free_bytes', models.BigIntegerField(default=0)),
                ('volume_available_bytes', models.BigIntegerField(default=0)),
                ('total_logical_bytes', models.BigIntegerField(default=0)),
                ('total_allocated_bytes', models.BigIntegerField(default=0)),
                ('total_archive_bytes', models.BigIntegerField(default=0)),
                ('total_auxiliary_bytes', models.BigIntegerField(default=0)),
                ('total_file_count', models.IntegerField(default=0)),
                ('total_problem_count', models.IntegerField(default=0)),
                ('orphan_count', models.IntegerField(default=0)),
                ('orphan_bytes', models.BigIntegerField(default=0)),
                ('stale', models.BooleanField(default=True)),
                ('observed_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]