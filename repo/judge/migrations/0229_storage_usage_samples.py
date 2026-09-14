from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0228_storage_passive_local_eviction'),
    ]

    operations = [
        migrations.CreateModel(
            name='StorageUsageSample',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('sampled_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('total_logical_bytes', models.BigIntegerField(default=0)),
                ('total_allocated_bytes', models.BigIntegerField(default=0)),
                ('total_archive_bytes', models.BigIntegerField(default=0)),
                ('total_auxiliary_bytes', models.BigIntegerField(default=0)),
                ('total_file_count', models.IntegerField(default=0)),
                ('problem_count', models.IntegerField(default=0)),
                ('organization', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='storage_usage_samples', to='judge.organization')),
            ],
            options={
                'ordering': ['-sampled_at'],
            },
        ),
        migrations.AddIndex(
            model_name='storageusagesample',
            index=models.Index(fields=['organization', '-sampled_at'], name='judge_stora_organiz_9c2f66_idx'),
        ),
    ]
