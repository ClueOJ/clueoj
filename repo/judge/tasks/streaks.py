import logging
from datetime import timedelta
from time import monotonic

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from judge.models import StreakEvent
from judge.utils.streaks import enabled, process_event

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def drain_streak_work():
    if not enabled():
        return 0
    deadline = monotonic() + getattr(settings, 'STREAKS_WORK_SECONDS', 10)
    ids = list(StreakEvent.objects.filter(pending=True, available_at__lte=timezone.now()).filter(
        Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()),
    ).order_by('id').values_list('id', flat=True)[:getattr(settings, 'STREAKS_BATCH_SIZE', 50)])
    done = 0
    for event_id in ids:
        try:
            done += bool(process_event(event_id))
        except Exception:
            # Leave durable work pending. A failed event must not starve the queue.
            state = StreakEvent.objects.filter(pk=event_id).first()
            if state:
                state.failures += 1
                state.retry_at = timezone.now() + timedelta(seconds=min(3600, 15 * 2 ** min(state.failures, 8)))
                state.save(update_fields=['failures', 'retry_at'])
            logger.exception('Streak achievement processing failed for work item %s', event_id)
        if monotonic() >= deadline:
            break
    return done
