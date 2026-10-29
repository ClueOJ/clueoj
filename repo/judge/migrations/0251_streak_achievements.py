from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('judge', '0248_merge_practice_streaks')]

    operations = [
        migrations.CreateModel(name='StreakEpoch', fields=[
            ('id', models.PositiveSmallIntegerField(default=1, primary_key=True, serialize=False)),
            ('submission_id', models.PositiveIntegerField()),
            ('started_at', models.DateTimeField()),
        ]),
        migrations.CreateModel(name='StreakProgress', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('problem_id', models.PositiveIntegerField()),
            ('generation', models.CharField(max_length=64)),
            ('best_points', models.DecimalField(max_digits=20, decimal_places=3, default=0)),
            ('subtasks', models.JSONField(default=dict)),
            ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='judge.profile')),
        ], options={'unique_together': {('user', 'problem_id', 'generation')}}),
        migrations.CreateModel(name='StreakEvent', fields=[
            ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('submission_id', models.PositiveIntegerField(unique=True)),
            ('day', models.DateField()),
            ('previous_points', models.DecimalField(max_digits=20, decimal_places=3)),
            ('points', models.DecimalField(max_digits=20, decimal_places=3)),
            ('snapshot', models.JSONField(default=dict)),
            ('applied', models.BooleanField(default=True)),
            ('offline_attempt_id', models.PositiveIntegerField(null=True)),
            ('contest_id', models.PositiveIntegerField(null=True)),
            ('pending', models.BooleanField(default=True)),
            ('available_at', models.DateTimeField()),
            ('failures', models.PositiveIntegerField(default=0)),
            ('retry_at', models.DateTimeField(null=True)),
            ('progress', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='judge.streakprogress')),
        ]),
        migrations.AddIndex(model_name='streakevent', index=models.Index(
            fields=['pending', 'available_at', 'id'], name='streak_event_pending_idx')),
    ]
