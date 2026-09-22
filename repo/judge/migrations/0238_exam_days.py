from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0237_offline_reveal')]
    operations = [
        migrations.AddField(model_name='examtag', name='day_count',
            field=models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)], verbose_name='Số ngày thi')),
        migrations.AddField(model_name='examtagproblempoint', name='day_number',
            field=models.PositiveIntegerField(default=1, validators=[MinValueValidator(1)], verbose_name='Ngày thi')),
        migrations.AddField(model_name='examofflineattempt', name='day_number',
            field=models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])),
    ]
