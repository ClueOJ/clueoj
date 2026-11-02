import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction

from judge.models import ProblemInvocationItem
from judge.utils.test_data_lock import locked_problem

logger = logging.getLogger(__name__)


@shared_task
def dispatch_invocation(invocation_id):
    # Dispatch existing submissions only. Submission's dispatch token protects
    # concurrent/redelivered tasks and data-restoration retries.
    items = ProblemInvocationItem.objects.filter(invocation_id=invocation_id, dispatch_state='pending')
    for item_id in list(items.values_list('pk', flat=True)[:25]):
        try:
            problem_id = ProblemInvocationItem.objects.values_list('invocation__problem_id', flat=True).get(pk=item_id)
            # Match source edits: acquire Problem before its item.
            with locked_problem(problem_id) as problem:
                item = ProblemInvocationItem.objects.select_for_update().get(pk=item_id)
                item.submission.problem = problem
                if item.dispatch_state != 'pending':
                    continue
                if item.submission.status not in ('QU', 'IE'):
                    item.dispatch_state = 'sent'
                    item.dispatch_error = ''
                    item.save(update_fields=['dispatch_state', 'dispatch_error'])
                    continue
                previous_token = (item.submission.batch_scoring or {}).get('dispatch_id')
                item.submission.judge(force_judge=True)
                item.submission.refresh_from_db(fields=['status', 'batch_scoring'])
                token = (item.submission.batch_scoring or {}).get('dispatch_id')
                if item.submission.status == 'QU' and token and token == previous_token:
                    from judge.judgeapi import _send_submission
                    from judge.models.submission import _problem_data_local_usable
                    from judge.judge_priority import BATCH_REJUDGE_PRIORITY
                    if not settings.STORAGE_ENSURE_READY_ENABLED or _problem_data_local_usable(item.submission.problem):
                        # Recover a committed reservation whose worker died
                        # before handoff. Same token means bridge deduplication
                        # remains effective if the original handoff succeeded.
                        sub = item.submission
                        transaction.on_commit(lambda sub=sub, token=token: _send_submission(
                            sub, False, False, None, BATCH_REJUDGE_PRIORITY, token))
                item.dispatch_state = 'sent'
                item.dispatch_error = ''
                item.save(update_fields=['dispatch_state', 'dispatch_error'])
            item.submission.refresh_from_db(fields=['status'])
            if item.submission.status == 'IE':
                raise RuntimeError('Judge dispatch returned internal error')
        except Exception:
            logger.exception('Invocation dispatch failed for item %s', item_id)
            ProblemInvocationItem.objects.filter(pk=item_id).update(
                dispatch_state='error',
                dispatch_error='Could not send to the judging queue. Use Retry dispatch.')
    if items.exists():
        dispatch_invocation.apply_async(args=[str(invocation_id)], countdown=1)
