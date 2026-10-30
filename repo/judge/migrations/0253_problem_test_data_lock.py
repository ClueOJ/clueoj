from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('judge', '0252_streak_offline_reveal_index'),
        ('judge', '0250_merge_practice_streaks_conservative'),
    ]

    operations = [
        migrations.AddField(
            model_name='problem', name='is_locked',
            field=models.BooleanField(default=True, editable=False, verbose_name='test data locked'),
        ),
    ]
