from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('judge', '0234_judging_usage_org_only')]
    operations = [migrations.DeleteModel(name='JudgingUsage')]
