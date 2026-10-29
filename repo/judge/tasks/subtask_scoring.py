"""A bounded, resumable rebuild. Tests stay locked until explicit disable.

Each task dispatches at most one submission then yields to the queue. Its
progress is in the DB, so duplicate delivery cannot dispatch the next item
twice. STOPPING drains work already handed to judges before unlocking tests.
"""
import uuid
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import Problem, Submission
from judge.utils.batch_scoring import scoring_dataset
from judge.utils.manual_subtasks import validate_manual_files
from judge.utils.subtask_scoring import (ACTIVE_STATUSES, refresh_problem_profiles,
                                         validate_configuration)

__all__ = ('rebuild_subtask_scores', 'refresh_contest_subtask_scores', 'set_subtask_scoring')


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=3, acks_late=True)
def refresh_contest_subtask_scores(contest_id, problem_id):
    from judge.models import Contest
    contest = Contest.objects.filter(pk=contest_id).first()
    if not contest or not Problem.objects.filter(pk=problem_id, subtask_score_state='ON').exists():
        return
    if contest.end_time > timezone.now():
        # Contest duration can be extended after the first result is graded.
        refresh_contest_subtask_scores.apply_async(args=[contest_id, problem_id], eta=contest.end_time)
    else:
        refresh_problem_profiles(problem_id)


def _enqueue(problem_id, revision, task_id, countdown=0):
    try:
        rebuild_subtask_scores.apply_async(args=[problem_id, revision], task_id=task_id, countdown=countdown)
    except Exception as exc:
        Problem.objects.filter(pk=problem_id, subtask_score_revision=revision,
                               subtask_score_state='REBUILDING').update(
            subtask_score_state='FAILED', subtask_score_error=str(exc))
        Problem.objects.filter(pk=problem_id, subtask_score_revision=revision,
                               subtask_score_state='STOPPING').update(subtask_score_error=str(exc))
        raise ValidationError(_('Could not queue the score rebuild. Check Celery and retry.')) from exc


def set_subtask_scoring(problem_id, user, enabled):
    if not user.is_superuser:
        raise PermissionDenied
    with transaction.atomic():
        problem = Problem.objects.select_for_update().get(pk=problem_id)
        if enabled:
            if problem.subtask_score_state not in ('OFF', 'FAILED'):
                raise ValidationError(_('A score rebuild is already active.'))
            # Retrying a failed build uses the same locked, validated config.
            if problem.subtask_score_state == 'OFF':
                config = validate_configuration(problem)
            else:
                validate_manual_files(problem)
                config = problem.subtask_score_config
            ceiling = Submission.objects.filter(problem=problem).aggregate(pk=Max('pk'))['pk'] or 0
            progress = {'phase': 'drain', 'ceiling': ceiling, 'cursor': 0, 'pending': None, 'dispatched': False,
                        'completed': 0, 'total': Submission.objects.filter(problem=problem, pk__lte=ceiling).count(),
                        'started_at': timezone.now().isoformat(), 'last_progress_at': timezone.now().isoformat()}
            updates = {'subtask_score_state': 'REBUILDING', 'subtask_score_config': config,
                       'subtask_score_progress': progress, 'subtask_score_error': ''}
        else:
            if problem.subtask_score_state == 'OFF':
                return
            updates = {'subtask_score_state': 'STOPPING', 'subtask_score_error': ''}
        revision = problem.subtask_score_revision + 1
        task_id = str(uuid.uuid4())
        updates.update(subtask_score_revision=revision, subtask_score_task_id=task_id)
        Problem.objects.filter(pk=problem_id).update(**updates)
        transaction.on_commit(lambda: _enqueue(problem_id, revision, task_id))


def _reschedule(problem):
    transaction.on_commit(lambda: _enqueue(problem.pk, problem.subtask_score_revision,
                                          problem.subtask_score_task_id, countdown=3))


@shared_task(bind=True, max_retries=3, acks_late=True, reject_on_worker_lost=True)
def rebuild_subtask_scores(self, problem_id, revision):
    try:
        return _rebuild_step(problem_id, revision)
    except Exception as exc:
        Problem.objects.filter(pk=problem_id, subtask_score_revision=revision,
                               subtask_score_state='REBUILDING').update(
            subtask_score_state='FAILED', subtask_score_error=str(exc))
        # STOPPING must remain locked if its profile refresh failed. Retrying
        # the disable action schedules a fresh generation and refreshes again.
        Problem.objects.filter(pk=problem_id, subtask_score_revision=revision,
                               subtask_score_state='STOPPING').update(subtask_score_error=str(exc))
        raise


class _ObsoleteRebuild(Exception):
    """Roll back unpublished profile changes when the generation changes."""


def _rebuild_step(problem_id, revision):
    with transaction.atomic():
        problem = Problem.objects.select_for_update().get(pk=problem_id)
        if problem.subtask_score_revision != revision or problem.subtask_score_state not in ('REBUILDING', 'STOPPING'):
            return 'obsolete'
        refresh_state = problem.subtask_score_state
        active = Submission.objects.filter(problem=problem, status__in=ACTIVE_STATUSES)
        if problem.subtask_score_state == 'STOPPING':
            if active.exists():
                _reschedule(problem)
                return 'draining'
            # STOPPING uses MAX again. Keep tests/admission locked until the
            # profile refresh completes and OFF can be published.
            progress = {'phase': 'disable-refresh'}
            Problem.objects.filter(pk=problem_id).update(subtask_score_progress=progress)
        else:
            validate_manual_files(problem)
            progress = dict(problem.subtask_score_progress)
            started = timezone.datetime.fromisoformat(progress.get('last_progress_at', progress['started_at']))
            if timezone.now() - started > timedelta(seconds=getattr(settings, 'SUBTASK_SCORE_REBUILD_TIMEOUT', 10800)):
                raise ValidationError(_('Score rebuild timed out. Check the judge and retry or turn the feature off.'))
            if progress['phase'] == 'drain':
                if active.exists():
                    _reschedule(problem)
                    return 'draining'
                progress['phase'] = 'rejudge'
            pending = progress.get('pending')
            if pending:
                if not progress.get('dispatched'):
                    transaction.on_commit(lambda: _dispatch(pending, problem_id, revision))
                    return 'dispatching'
                submission = Submission.objects.filter(pk=pending).first()
                if submission and submission.status in ACTIVE_STATUSES:
                    _reschedule(problem)
                    return 'judging'
                if submission:
                    if submission.status not in ('D', 'CE'):
                        raise ValidationError(_('Submission %(id)s did not finish successfully. Retry the rebuild.'),
                                              params={'id': pending})
                    if (submission.batch_scoring or {}).get('cumulative_revision') != revision:
                        raise ValidationError(_('A submission was graded with a different score revision.'))
                    if submission.status == 'D':
                        expected = {row['batch'] for row in problem.subtask_score_config['full']}
                        if (scoring_dataset(submission) != 'full' or
                                set(submission.batch_results.values_list('batch', flat=True)) != expected):
                            raise ValidationError(_('Full subtask results are missing from a rebuilt submission.'))
                progress['cursor'] = pending
                progress['pending'] = None
                progress['dispatched'] = False
                progress['completed'] += 1
                progress['last_progress_at'] = timezone.now().isoformat()
            candidate = (Submission.objects.filter(problem=problem, pk__gt=progress['cursor'],
                                                   pk__lte=progress['ceiling']).order_by('pk').first())
            if candidate:
                if candidate.is_locked or candidate.offline_hidden:
                    raise ValidationError(_('A submission became locked or hidden during the rebuild.'))
                progress['pending'] = candidate.pk
                progress['dispatched'] = False
                Problem.objects.filter(pk=problem_id).update(subtask_score_progress=progress)
                # Serialize dispatch with disable. The bridge snapshot is read
                # after commit by the separate bridge process, as usual.
                transaction.on_commit(lambda: _dispatch(candidate.pk, problem_id, revision))
                return 'dispatched'
            progress['phase'] = 'refresh'
            Problem.objects.filter(pk=problem_id).update(subtask_score_progress=progress)

    # Lock profiles before Problem, matching offline admission. All profile/PP
    # writes and publication commit together; errors or a superseding generation
    # roll the entire refresh back, including organization aggregates.
    try:
        with transaction.atomic():
            refresh_problem_profiles(problem_id,
                rebuilding_problem=problem if refresh_state == 'REBUILDING' else None)
            problem = Problem.objects.select_for_update().get(pk=problem_id)
            if problem.subtask_score_revision != revision or problem.subtask_score_state != refresh_state:
                raise _ObsoleteRebuild()
            if refresh_state == 'REBUILDING':
                validate_manual_files(problem)
            state = 'OFF' if problem.subtask_score_state == 'STOPPING' else 'ON'
            Problem.objects.filter(pk=problem_id).update(subtask_score_state=state,
                subtask_score_progress={**progress, 'phase': 'done'}, subtask_score_error='')
    except _ObsoleteRebuild:
        return 'obsolete'
    return state


def _dispatch(submission_id, problem_id, revision):
    with transaction.atomic():
        problem = Problem.objects.select_for_update().get(pk=problem_id)
        if problem.subtask_score_revision != revision or problem.subtask_score_state != 'REBUILDING':
            return
        if (problem.subtask_score_progress.get('pending') != submission_id or
                problem.subtask_score_progress.get('dispatched')):
            return
        validate_manual_files(problem)
        submission = Submission.objects.select_related('problem', 'language', 'source').get(pk=submission_id)
        # This path deliberately does not force locked submissions or rewrite
        # contest pretest policy. force_full applies to this judging run only.
        submission.judge(rejudge=True, batch_rejudge=True, force_full=True, cumulative_revision=revision)
        progress = {**problem.subtask_score_progress, 'dispatched': True,
                    'last_progress_at': timezone.now().isoformat()}
        Problem.objects.filter(pk=problem_id).update(subtask_score_progress=progress)
        _reschedule(problem)
