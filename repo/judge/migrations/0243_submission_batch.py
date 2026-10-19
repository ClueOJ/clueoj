import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('judge', '0242_organization_temporary_extension'),
    ]
    operations = [
        migrations.CreateModel(
            name='SubmissionBatch',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('expected_count', models.PositiveIntegerField()),
                ('contest', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to='judge.contest')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='SubmissionBatchItem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('path', models.CharField(max_length=512)),
                ('username', models.CharField(max_length=150)),
                ('problem_code', models.CharField(max_length=128)),
                ('language_key', models.CharField(max_length=32)),
                ('source', models.TextField()),
                ('state', models.CharField(default='pending', max_length=16)),
                ('message', models.TextField(blank=True)),
                ('transition', models.TextField(blank=True)),
                ('batch', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='judge.submissionbatch')),
                ('submission', models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='judge.submission')),
            ],
            options={'ordering': ['id'], 'unique_together': {('batch', 'path')}},
        ),
    ]
