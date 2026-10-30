from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0247_cumulative_subtask_scoring')]

    # Schema only: every existing and new contest starts OFF. No job or rejudge.
    operations = [
        migrations.AddField('problem', 'conservative_config', models.JSONField(default=dict, blank=True, editable=False)),
        migrations.AddField('contest', 'conservative_state', models.CharField(max_length=10, default='OFF', editable=False,
            choices=[('OFF', 'Off'), ('BUILDING', 'Building'), ('ON', 'On'), ('FAILED', 'Failed')])),
        migrations.AddField('contest', 'conservative_revision', models.PositiveIntegerField(default=0, editable=False)),
        migrations.AddField('contest', 'conservative_config', models.JSONField(default=dict, blank=True, editable=False)),
        migrations.AddField('contest', 'conservative_progress', models.JSONField(default=dict, blank=True, editable=False)),
        migrations.AddField('contest', 'conservative_error', models.TextField(blank=True, editable=False)),
        migrations.AddField('contestsubmission', 'conservative_points', models.FloatField(null=True, editable=False)),
        migrations.AddField('contestsubmission', 'conservative_data', models.JSONField(default=dict, blank=True, editable=False)),
    ]
