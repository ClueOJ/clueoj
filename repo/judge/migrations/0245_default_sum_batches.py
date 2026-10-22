from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0244_per_batch_scoring')]

    operations = [
        migrations.AlterField(
            model_name='problemtestcase',
            name='batch_type',
            field=models.CharField(choices=[('min', 'Min'), ('sum', 'Sum')], default='sum',
                                   max_length=3, verbose_name='batch scoring'),
        ),
    ]
