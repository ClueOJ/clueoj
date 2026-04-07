from celery import shared_task
from django.core.cache import cache

from judge.utils.exams import build_exam_snapshots

__all__ = ('rebuild_exams_snapshots',)


@shared_task(bind=True)
def rebuild_exams_snapshots(self):
    try:
        payload = build_exam_snapshots()
        return payload['summary']['total']
    finally:
        cache.delete('exams:snapshot:queued')
