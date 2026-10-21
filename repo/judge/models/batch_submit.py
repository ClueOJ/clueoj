import uuid

from django.conf import settings
from django.db import models


class SubmissionBatch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    contest = models.ForeignKey('Contest', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    expected_count = models.PositiveIntegerField()


class SubmissionBatchItem(models.Model):
    batch = models.ForeignKey(SubmissionBatch, related_name='items', on_delete=models.CASCADE)
    path = models.CharField(max_length=512)
    username = models.CharField(max_length=150)
    problem_code = models.CharField(max_length=128)
    language_key = models.CharField(max_length=32)
    source = models.TextField()
    state = models.CharField(max_length=16, default='pending')
    message = models.TextField(blank=True)
    transition = models.TextField(blank=True)
    submission = models.OneToOneField('Submission', null=True, blank=True, on_delete=models.PROTECT)

    class Meta:
        unique_together = [('batch', 'path')]
        ordering = ['id']
