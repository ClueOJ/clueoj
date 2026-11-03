import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0259_judge_is_remote')]

    operations = [
        migrations.CreateModel(
            name='ProblemDataManifest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('manifest', models.JSONField(default=dict)),
                ('sha256', models.CharField(blank=True, default='', max_length=64)),
                ('ready_sha256', models.CharField(blank=True, default='', max_length=64)),
                ('problem', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE, related_name='remote_data', to='judge.problem',
                )),
            ],
        ),
    ]
