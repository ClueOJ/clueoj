import logging

from celery import shared_task
from django.core.exceptions import ValidationError

from judge.models import SubmissionBatch, SubmissionBatchItem
from judge.utils.batch_submit import QueueFull, admit_item

logger = logging.getLogger(__name__)


@shared_task
def run_submission_batch(batch_id):
    if not SubmissionBatch.objects.filter(pk=batch_id, started_at__isnull=False).exists():
        return
    # Keep tasks bounded. Users with a full judge queue can wait without blocking
    # other users. Each delivery can only claim a pending row once.
    ids = list(SubmissionBatchItem.objects.filter(batch_id=batch_id, state='pending').values_list('id', 'username'))
    processed = 0
    waiting_users = set()
    for item_id, username in ids:
        if username in waiting_users:
            continue
        if processed >= 25:
            break
        try:
            sub = admit_item(item_id)
        except QueueFull:
            waiting_users.add(username)
            continue
        except ValidationError as exc:
            SubmissionBatchItem.objects.filter(pk=item_id, state='pending').update(
                state='error', message=' '.join(exc.messages))
            processed += 1
            continue
        except Exception:
            logger.exception('Batch admission failed for item %s', item_id)
            SubmissionBatchItem.objects.filter(pk=item_id, state='pending').update(
                state='error', message='Lỗi hệ thống khi nhận bài. Xem log máy chủ.')
            processed += 1
            continue
        if sub is None:
            continue
        processed += 1
        try:
            sub.judge(force_judge=True)
        except Exception:
            logger.exception('Batch dispatch failed for submission %s', sub.pk)
            SubmissionBatchItem.objects.filter(pk=item_id).update(
                state='dispatch_error', message='Đã tạo bài nộp nhưng gửi chấm thất bại. Chấm lại submission này; không nộp lại file.')
        else:
            SubmissionBatchItem.objects.filter(pk=item_id).update(state='submitted', message='Đã gửi chấm.')
    if SubmissionBatchItem.objects.filter(batch_id=batch_id, state='pending').exists():
        run_submission_batch.apply_async(args=[batch_id], countdown=3)
