import logging
from datetime import timedelta
from time import monotonic

from celery import shared_task
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from judge.models import StreakProblemState, StreakRebuildRequest
from judge.utils.streaks import enabled, expand_request, process_pair

logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def drain_streak_work():
    if not enabled():
        return 0
    deadline = monotonic() + getattr(settings, 'STREAKS_WORK_SECONDS', 10)
    for request_id in StreakRebuildRequest.objects.order_by('id').values_list('id', flat=True)[:5]:
        expand_request(request_id)
        if monotonic() >= deadline:
            return 0
    ids = list(StreakProblemState.objects.filter(pending=True).filter(
        Q(retry_at__isnull=True) | Q(retry_at__lte=timezone.now()),
    ).order_by('id').values_list('id', flat=True)[:getattr(settings, 'STREAKS_BATCH_SIZE', 50)])
    done = 0
    for state_id in ids:
        try:
            process_pair(state_id)
            done += 1
        except Exception:
            # Leave durable work pending. A broken pair must not starve the queue.
            state = StreakProblemState.objects.filter(pk=state_id).first()
            if state:
                state.failures += 1
                state.retry_at = timezone.now() + timedelta(seconds=min(3600, 15 * 2 ** min(state.failures, 8)))
                state.save(update_fields=['failures', 'retry_at'])
            logger.exception('Streak reconciliation failed for work item %s', state_id)
        if monotonic() >= deadline:
            break
    return done
