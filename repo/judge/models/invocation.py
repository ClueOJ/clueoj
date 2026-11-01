import uuid

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver


INVOCATION_EXPECTATIONS = (
    ('AC', 'AC'), ('PAC', 'PAC'), ('AC/TL', 'AC/TL'), ('WA', 'WA'),
    ('TLE', 'TLE'), ('RJ', 'RJ'), ('SKIP', 'Skipped'),
)


class ProblemInvocationSource(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    problem = models.ForeignKey('Problem', related_name='invocation_sources', on_delete=models.CASCADE)
    filename = models.CharField(max_length=255)
    path = models.CharField(max_length=512)
    language = models.ForeignKey('Language', null=True, blank=True, on_delete=models.PROTECT)
    expected_overall = models.CharField(max_length=5, choices=INVOCATION_EXPECTATIONS, blank=True)
    expected_subtasks = models.JSONField(default=dict, blank=True)
    expected_layout = models.JSONField(default=list, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('created_at', 'id')
        constraints = [
            models.UniqueConstraint(fields=('filename', 'problem'), name='invocation_source_unique_filename'),
        ]


class ProblemInvocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    problem = models.ForeignKey('Problem', related_name='invocations', on_delete=models.CASCADE)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    username = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    request_key = models.UUIDField(default=uuid.uuid4, unique=True)
    layout = models.JSONField(null=True)

    class Meta:
        ordering = ('-created_at',)


class ProblemInvocationItem(models.Model):
    invocation = models.ForeignKey(ProblemInvocation, related_name='items', on_delete=models.CASCADE)
    source = models.ForeignKey(ProblemInvocationSource, null=True, on_delete=models.SET_NULL)
    filename = models.CharField(max_length=255)
    language = models.ForeignKey('Language', on_delete=models.PROTECT)
    expected_overall = models.CharField(max_length=5, choices=INVOCATION_EXPECTATIONS)
    expected_subtasks = models.JSONField(default=dict)
    submission = models.OneToOneField('Submission', related_name='invocation_item', on_delete=models.PROTECT)
    dispatch_error = models.TextField(blank=True)
    dispatch_state = models.CharField(max_length=8, default='pending')

    class Meta:
        ordering = ('id',)


@receiver(post_delete, sender=ProblemInvocationSource)
def delete_invocation_source_file(sender, instance, **kwargs):
    from judge.utils.invocation_storage import delete_source
    transaction.on_commit(lambda: delete_source(instance.path))
