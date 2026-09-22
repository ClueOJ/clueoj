from datetime import date, datetime, timedelta, timezone

from django.db import migrations, models
import judge.utils.organization


def populate_expiry(apps, schema_editor):
    Organization = apps.get_model('judge', 'Organization')
    orgs = Organization.objects.using(schema_editor.connection.alias)
    orgs.filter(plan='F').update(paid_until=date(2026, 1, 1))
    orgs.filter(plan='P').update(paid_until=date(2100, 1, 1))


def restore_plan(apps, schema_editor):
    Organization = apps.get_model('judge', 'Organization')
    orgs = Organization.objects.using(schema_editor.connection.alias)
    today = datetime.now(timezone(timedelta(hours=7))).date()
    orgs.filter(paid_until__lt=today).update(plan='F')
    orgs.filter(paid_until__gte=today).update(plan='P')


class Migration(migrations.Migration):
    dependencies = [('judge', '0240_organization_slots_help_text')]
    operations = [
        migrations.AddField(model_name='organization', name='paid_until',
                            field=models.DateField(null=True)),
        migrations.RunPython(populate_expiry, restore_plan),
        migrations.AlterField(model_name='organization', name='paid_until', field=models.DateField(
            default=judge.utils.organization.default_paid_until,
            verbose_name='Paid plan expiration date',
            help_text='The paid plan remains active through this date (UTC+7).')),
        # Create replacement indexes before removing the old ones: MariaDB may
        # use the creator-leading composite index for its foreign key constraint.
        migrations.AddIndex(model_name='organization', index=models.Index(
            fields=['paid_until', 'is_unlisted', 'name'], name='judge_org_expiry_unlist_idx')),
        migrations.AddIndex(model_name='organization', index=models.Index(
            fields=['creator', 'paid_until', '-creation_date'], name='judge_org_creator_expiry_idx')),
        migrations.RemoveIndex(model_name='organization', name='judge_org_plan_unlist_idx'),
        migrations.RemoveIndex(model_name='organization', name='judge_org_creator_plan_idx'),
        migrations.RemoveField(model_name='organization', name='plan'),
    ]
