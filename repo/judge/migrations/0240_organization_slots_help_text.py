from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0239_exam_unsplit_default')]

    operations = [
        migrations.AlterField(
            model_name='organization', name='slots',
            field=models.IntegerField(
                blank=True, null=True, verbose_name='maximum size',
                help_text='Maximum amount of users in this organization, '
                          'applicable to both open and private organizations.',
            ),
        ),
    ]
