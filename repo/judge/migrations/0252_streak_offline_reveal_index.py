from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0251_streak_achievements')]

    operations = [
        migrations.AddIndex(
            model_name='streakevent',
            index=models.Index(fields=['offline_attempt_id', 'pending', 'id'], name='streak_offline_work_idx'),
        ),
    ]
