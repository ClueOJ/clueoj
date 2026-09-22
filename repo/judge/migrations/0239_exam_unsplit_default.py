from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0238_exam_days')]
    operations = [
        migrations.AlterField(model_name='examtag', name='day_count',
            field=models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)], verbose_name='Số ngày thi')),
        migrations.AlterField(model_name='examofflineattempt', name='day_number',
            field=models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])),
    ]
