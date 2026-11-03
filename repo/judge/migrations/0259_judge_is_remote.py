from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0258_invocation_source_unique_filename')]

    operations = [
        migrations.AddField(
            model_name='judge',
            name='is_remote',
            field=models.BooleanField(
                default=False,
                verbose_name='remote judge',
                help_text='Uses independent problem data. Remote judges cannot judge manually '
                          'managed problems or mirrors whose root is manually managed.',
            ),
        ),
    ]
