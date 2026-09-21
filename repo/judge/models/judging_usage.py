from django.db import models


class JudgingUsageDaily(models.Model):
    """Daily totals; organization_key=0 represents unowned problems."""

    organization_key = models.PositiveIntegerField(default=0)
    organization_name = models.CharField(max_length=256, blank=True)
    usage_date = models.DateField(db_index=True)
    seconds = models.FloatField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    rejudges = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['organization_key', 'usage_date'],
                                    name='judging_daily_unique'),
        ]
