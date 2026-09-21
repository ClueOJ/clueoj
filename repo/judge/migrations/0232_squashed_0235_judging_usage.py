from django.db import migrations, models


class Migration(migrations.Migration):
    # Fresh installations create only the final daily table. Databases that have
    # applied all four development migrations retain their existing totals.
    replaces = [
        ('judge', '0232_judging_usage'),
        ('judge', '0233_judging_usage_daily'),
        ('judge', '0234_judging_usage_org_only'),
        ('judge', '0235_remove_judging_attempt_history'),
    ]

    dependencies = [('judge', '0231_exam_score_milestones')]

    operations = [
        migrations.CreateModel(
            name='JudgingUsageDaily',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('organization_key', models.PositiveIntegerField(default=0)),
                ('organization_name', models.CharField(blank=True, max_length=256)),
                ('usage_date', models.DateField(db_index=True)),
                ('seconds', models.FloatField(default=0)),
                ('attempts', models.PositiveIntegerField(default=0)),
                ('rejudges', models.PositiveIntegerField(default=0)),
            ],
            options={
                'constraints': [models.UniqueConstraint(
                    fields=('organization_key', 'usage_date'), name='judging_daily_unique',
                )],
            },
        ),
    ]
