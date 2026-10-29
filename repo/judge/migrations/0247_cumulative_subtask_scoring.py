from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0246_exam_host_locations')]

    operations = [
        migrations.AddField('problem', 'subtask_score_state', models.CharField(
            max_length=12, default='OFF', editable=False, verbose_name='cumulative subtask scoring',
            choices=[('OFF', 'Off'), ('REBUILDING', 'Rebuilding'), ('ON', 'On'),
                     ('STOPPING', 'Stopping'), ('FAILED', 'Failed')])),
        migrations.AddField('problem', 'subtask_score_revision', models.PositiveIntegerField(default=0, editable=False)),
        migrations.AddField('problem', 'subtask_score_task_id', models.CharField(max_length=36, blank=True, editable=False)),
        migrations.AddField('problem', 'subtask_score_config', models.JSONField(default=dict, blank=True, editable=False)),
        migrations.AddField('problem', 'subtask_score_progress', models.JSONField(default=dict, blank=True, editable=False)),
        migrations.AddField('problem', 'subtask_score_error', models.TextField(blank=True, editable=False)),
    ]
