import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0222_external_judge'),
    ]

    operations = [
        migrations.AddField(
            model_name='problem',
            name='is_official_test',
            field=models.BooleanField(
                default=False,
                help_text='Whether this problem uses official tests.',
                verbose_name='official tests',
            ),
        ),
        migrations.AlterField(
            model_name='problem',
            name='points',
            field=models.FloatField(
                help_text="Points awarded for problem completion. Points are displayed with a 'p' suffix if partial.",
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(200),
                ],
                verbose_name='points',
            ),
        ),
    ]
