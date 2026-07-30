from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0226_storage_sync_contract'),
    ]

    operations = [
        migrations.AddField(
            model_name='storageorganizationusage',
            name='problem_quota',
            field=models.IntegerField(default=0),
        ),
    ]
