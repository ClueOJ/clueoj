"""Immutable practice achievements. Never changes submissions or account scores."""
from django.db import models


class StreakSummary(models.Model):
    user = models.OneToOneField('Profile', primary_key=True, on_delete=models.CASCADE)
    timezone = models.CharField(max_length=50)
    last_day = models.DateField(null=True)
    current_length = models.PositiveIntegerField(default=0)
    longest = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)


class StreakProblemState(models.Model):
    # Legacy v1 storage, retained for rollback; no active producer/consumer.
    user = models.ForeignKey('Profile', on_delete=models.CASCADE)
    # Keep the work item after problem deletion so derived days can be repaired.
    problem_id = models.PositiveIntegerField()
    pending = models.BooleanField(default=True)
    rebuild = models.BooleanField(default=True)
    dirty_date = models.DateTimeField(null=True)
    dirty_id = models.PositiveIntegerField(null=True)
    last_date = models.DateTimeField(null=True)
    last_id = models.PositiveIntegerField(default=0)
    best_points = models.DecimalField(max_digits=20, decimal_places=3, default=0)
    solved = models.BooleanField(default=False)
    retry_at = models.DateTimeField(null=True)
    failures = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('user', 'problem_id')]
        indexes = [models.Index(fields=['pending', 'retry_at', 'id'], name='streak_pending_idx')]


class StreakContribution(models.Model):
    user = models.ForeignKey('Profile', on_delete=models.CASCADE)
    problem_id = models.PositiveIntegerField()
    # Deliberately no FK: deletion must not erase evidence before reconciliation.
    submission_id = models.PositiveIntegerField(unique=True)
    day = models.DateField()
    previous_points = models.DecimalField(max_digits=20, decimal_places=3)
    points = models.DecimalField(max_digits=20, decimal_places=3)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'day'], name='streak_contrib_day_idx'),
            models.Index(fields=['user', 'problem_id'], name='streak_contrib_pair_idx'),
        ]


class StreakDay(models.Model):
    user = models.ForeignKey('Profile', on_delete=models.CASCADE)
    day = models.DateField()

    class Meta:
        unique_together = [('user', 'day')]


class StreakRun(models.Model):
    user = models.ForeignKey('Profile', on_delete=models.CASCADE)
    start = models.DateField()
    end = models.DateField()
    length = models.PositiveIntegerField()

    class Meta:
        unique_together = [('user', 'start')]


class StreakRebuildRequest(models.Model):
    # Legacy v1 storage, retained for rollback; no new requests are produced.
    kind = models.CharField(max_length=10, choices=[('user', 'User'), ('problem', 'Problem')])
    object_id = models.PositiveIntegerField()
    cursor = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [('kind', 'object_id')]


class StreakEpoch(models.Model):
    """Singleton cutover marker; pre-cutover/in-flight work cannot award days."""
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    submission_id = models.PositiveIntegerField()
    started_at = models.DateTimeField()


class StreakProgress(models.Model):
    user = models.ForeignKey('Profile', on_delete=models.CASCADE)
    problem_id = models.PositiveIntegerField()
    generation = models.CharField(max_length=64)
    best_points = models.DecimalField(max_digits=20, decimal_places=3, default=0)
    subtasks = models.JSONField(default=dict)

    class Meta:
        unique_together = [('user', 'problem_id', 'generation')]


class StreakEvent(models.Model):
    """Transactional outbox: result snapshot survives rejudging and deletion."""
    progress = models.ForeignKey(StreakProgress, on_delete=models.CASCADE)
    submission_id = models.PositiveIntegerField(unique=True)
    day = models.DateField()
    previous_points = models.DecimalField(max_digits=20, decimal_places=3)
    points = models.DecimalField(max_digits=20, decimal_places=3)
    snapshot = models.JSONField(default=dict)
    applied = models.BooleanField(default=True)
    # Only publication of the original result releases a deferred achievement.
    # No FK: deleting a submission/problem must preserve an earned achievement.
    offline_attempt_id = models.PositiveIntegerField(null=True)
    contest_id = models.PositiveIntegerField(null=True)
    pending = models.BooleanField(default=True)
    available_at = models.DateTimeField()
    failures = models.PositiveIntegerField(default=0)
    retry_at = models.DateTimeField(null=True)

    class Meta:
        indexes = [
            models.Index(fields=['pending', 'available_at', 'id'], name='streak_event_pending_idx'),
            models.Index(fields=['offline_attempt_id', 'pending', 'id'], name='streak_offline_work_idx'),
        ]
