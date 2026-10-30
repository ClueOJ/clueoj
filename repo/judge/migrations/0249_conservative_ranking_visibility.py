from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('judge', '0248_conservative_ranking')]

    # Preserve existing access. The independent feature switch still defaults OFF.
    operations = [
        migrations.AddField('contest', 'show_conservative_ranking',
                            models.BooleanField(default=True, editable=False)),
    ]
