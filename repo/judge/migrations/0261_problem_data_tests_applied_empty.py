from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0260_problem_data_manifest')]

    operations = [
        migrations.AddField(
            model_name='problemdata',
            name='tests_applied_empty',
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
