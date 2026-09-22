from datetime import datetime, timedelta, timezone
from django.db import migrations, models


def grant_initial_extension(apps, schema_editor):
    Organization = apps.get_model('judge', 'Organization')
    today = datetime.now(timezone(timedelta(hours=7))).date()
    Organization.objects.using(schema_editor.connection.alias).filter(
        paid_until__gte=today,
    ).update(temporary_extension_available=True)


class Migration(migrations.Migration):
    dependencies = [('judge', '0241_organization_paid_until')]
    operations = [
        migrations.AddField(model_name='organization', name='temporary_extension_available',
                            field=models.BooleanField(default=False, editable=False, verbose_name='Temporary extension available')),
        migrations.AddField(model_name='organization', name='temporary_paid_until',
                            field=models.DateField(null=True, blank=True, editable=False, verbose_name='Temporary expiration date')),
        migrations.RunPython(grant_initial_extension, migrations.RunPython.noop),
    ]
