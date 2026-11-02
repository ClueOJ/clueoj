"""The test editor and judging share the same Problem row locks."""
import logging
from contextlib import contextmanager

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


@contextmanager
def locked_problem(problem_id):
    from judge.models import Problem

    while True:
        with transaction.atomic():
            root_id = Problem.objects.values_list('mirror_of_id', flat=True).get(pk=problem_id)
            rows = {row.pk: row for row in Problem.objects.select_for_update().filter(
                pk__in={problem_id, root_id or problem_id}).order_by('pk')}
            problem = rows[problem_id]
            if problem.mirror_of_id != root_id:
                # Leave this transaction before retrying so lock ordering stays
                # stable even when the newly selected root has a lower PK.
                continue
            yield problem
            return


def require_judging_allowed(problem_id):
    from judge.models import Problem

    problem = Problem.objects.only('is_locked', 'mirror_of_id').get(pk=problem_id)
    if not problem.is_locked or (problem.mirror_of_id and not Problem.objects.filter(
            pk=problem.mirror_of_id, is_locked=True, mirror_of__isnull=True).exists()):
        raise ValidationError(_('Test data is being edited. Please submit again after the author applies the changes.'))


def require_editing(problem):
    if problem.is_locked:
        raise ValidationError(_('Unlock the test data before editing it.'))


def abort_test_submissions(problem_id):
    """Best effort delivery; the DB gate and file lock also protect failed sends."""
    from judge.judgeapi import judge_request

    try:
        judge_request({'name': 'abort-problem', 'problem-id': problem_id})
    except Exception:
        logger.warning('Could not deliver test editor abort for problem %s', problem_id, exc_info=True)


def unlock_tests(problem_id):
    from judge.models import Problem, Submission

    with locked_problem(problem_id) as problem:
        if problem.subtask_tests_locked:
            from judge.utils.subtask_scoring import LOCKED_MESSAGE
            raise ValidationError(LOCKED_MESSAGE)
        if not problem.is_locked:
            transaction.on_commit(lambda: abort_test_submissions(problem_id))
            return 0
        Problem.objects.filter(pk=problem_id).update(is_locked=False)
        from judge.utils.problem_mirror import mirror_problem_ids
        active = Submission.objects.filter(
            problem_id__in=mirror_problem_ids(problem_id),
            status__in=Submission.IN_PROGRESS_GRADING_STATUS,
        )
        ids = list(active.values_list('pk', flat=True))
        active.update(status='AB', result='AB', points=0, case_points=0, case_total=0,
                      error=_('Aborted because the author unlocked the test data.'), judged_date=timezone.now())
        transaction.on_commit(lambda: abort_test_submissions(problem_id))
        if ids:
            transaction.on_commit(lambda: _notify_aborted(ids))
        return len(ids)


def _notify_aborted(ids):
    from judge import event_poster as event
    from judge.models import Submission
    from judge.judgeapi import _post_update_submission, update_submission_stats
    from judge.caching import finished_submission

    for submission in Submission.objects.filter(pk__in=ids).select_related('problem', 'user', 'language'):
        update_submission_stats(submission)
        finished_submission(submission)
        event.post('sub_%s' % submission.id_secret, {'type': 'aborted-submission'})
        _post_update_submission(submission, done=True)
