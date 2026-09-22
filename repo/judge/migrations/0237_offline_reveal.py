from django.db import migrations, models


def preserve_published(apps, schema_editor):
    Attempt = apps.get_model('judge', 'ExamOfflineAttempt')
    # Completed sessions from the previous version were already public.
    Attempt.objects.filter(ended_at__isnull=False).update(revealed_at=models.F('ended_at'))


class Migration(migrations.Migration):
    dependencies = [('judge', '0236_exam_offline')]
    operations = [
        migrations.AddField(model_name='examofflineattempt', name='revealed_at',
                            field=models.DateTimeField(null=True, blank=True)),
        migrations.RunPython(preserve_published, migrations.RunPython.noop),
    ]
