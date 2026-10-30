import json
import logging
import socket
import struct
import zlib
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from judge import event_poster as event
from judge.judge_priority import BATCH_REJUDGE_PRIORITY, CONTEST_SUBMISSION_PRIORITY, DEFAULT_PRIORITY, REJUDGE_PRIORITY

logger = logging.getLogger('judge.judgeapi')
size_pack = struct.Struct('!I')


def _recv_exact(sock, size):
    buffer = []
    while size:
        data = sock.recv(size)
        if not data:
            raise ValueError('Judge did not respond')
        buffer.append(data)
        size -= len(data)
    return b''.join(buffer)


def _post_update_submission(submission, done=False):
    if submission.problem.is_public:
        event.post('submissions', {'type': 'done-submission' if done else 'update-submission',
                                   'id': submission.id,
                                   'contest': submission.contest_key,
                                   'user': submission.user_id, 'problem': submission.problem_id,
                                   'status': submission.status, 'language': submission.language.key,
                                   'organizations':
                                   [x[0] for x in submission.user.organizations.get_queryset().values_list('id')],
                                   })


def update_submission_stats(submission):
    """Refresh derived scores after a committed terminal result, including bulk AB."""
    from judge.models import Problem, ExamTagProblemPoint
    from judge.utils.subtask_scoring import schedule_contest_score_refresh

    problem = submission.problem
    schedule_contest_score_refresh(submission)
    if problem.is_public and not problem.is_organization_private:
        submission.user._updating_stats_only = True
        submission.user.calculate_points()
    problem._updating_stats_only = True
    problem.update_stats()
    submission.update_contest()
    if (Problem.exam_tags.through.objects.filter(problem_id=problem.pk).exists() or
            ExamTagProblemPoint.objects.filter(problem_id=problem.pk).exists()):
        from judge.tasks import sync_exam_progress_for_user_problem
        sync_exam_progress_for_user_problem.delay(submission.user_id, problem.pk)


def judge_request(packet, reply=True):
    output = json.dumps(packet, separators=(',', ':'))
    output = zlib.compress(output.encode('utf-8'))
    with socket.create_connection(
            settings.BRIDGED_DJANGO_CONNECT or settings.BRIDGED_DJANGO_ADDRESS[0],
            timeout=settings.BRIDGED_DJANGO_TIMEOUT) as sock:
        sock.sendall(size_pack.pack(len(output)) + output)

        if reply:
            length = size_pack.unpack(_recv_exact(sock, size_pack.size))[0]
            input = _recv_exact(sock, length)
            return json.loads(zlib.decompress(input).decode('utf-8'))


def judge_submission(submission, rejudge=False, batch_rejudge=False, judge_id=None, storage_claimed=False,
                     force_full=False, cumulative_revision=None):
    from .models import Problem

    result = []
    from judge.utils.test_data_lock import locked_problem, require_judging_allowed
    with locked_problem(submission.problem_id) as problem:
        require_judging_allowed(problem.pk)
        accepted = _queue_submission(submission, problem, rejudge, batch_rejudge, judge_id, storage_claimed,
                                     force_full, cumulative_revision, result)
    return result[0] if result else accepted


def _queue_submission(submission, problem, rejudge, batch_rejudge, judge_id, storage_claimed,
                      force_full, cumulative_revision, result):
    from judge.utils.streaks import prepare_streak_run
    from .models import ContestSubmission, Submission, SubmissionTestCase
    from django.core.exceptions import ValidationError

    updates = {'time': None, 'memory': None, 'points': None, 'result': None, 'case_points': 0, 'case_total': 0,
               'error': None, 'rejudged_date': timezone.now() if rejudge or batch_rejudge else None,
               'status': 'P' if storage_claimed else 'QU', 'batch_scoring': None}
    try:
        # This is set proactively; it might get unset in judgecallback's on_grading_begin if the problem doesn't
        # actually have pretests stored on the judge.
        updates['is_pretested'] = all(ContestSubmission.objects.filter(submission=submission)
                                      .values_list('problem__contest__run_pretests_only', 'problem__is_pretested')[0])
    except IndexError:
        priority = DEFAULT_PRIORITY
    else:
        priority = CONTEST_SUBMISSION_PRIORITY

    if problem.subtask_submissions_paused and not (
            problem.subtask_score_state == 'REBUILDING' and cumulative_revision == problem.subtask_score_revision):
        raise ValidationError('Subtask scores are being rebuilt.')
    if force_full:
        updates['is_pretested'] = False
    updates['batch_scoring'] = {'dispatch_id': str(uuid.uuid4()),
                                'cumulative_revision': problem.subtask_score_revision}

    current = Submission.objects.filter(pk=submission.pk).values('status', 'batch_scoring').first()
    if current and current['status'] == 'AB' and not (rejudge or batch_rejudge):
        return False
    if (not storage_claimed and current and current['status'] == 'QU' and
            (current['batch_scoring'] or {}).get('dispatch_id')):
        return False

    updates['batch_scoring']['streak'] = prepare_streak_run(
        submission, problem, initial=not (rejudge or batch_rejudge) and current is not None and
        current['status'] in ('QU', 'P') and not (current['batch_scoring'] or {}).get('dispatch_id'))

    # This should prevent double rejudge issues by permitting only the judging of
    # QU (which is the initial state) and D (which is the final state).
    # Even though the bridge will not queue a submission already being judged,
    # we will destroy the current state by deleting all SubmissionTestCase objects.
    # A queued run with a committed token is already reserved above, including
    # the interval before its network handoff reaches the bridge.
    if storage_claimed:
        changed = Submission.objects.filter(id=submission.id, status='P').update(**updates)
    else:
        changed = Submission.objects.filter(id=submission.id).exclude(status__in=('P', 'G')).update(**updates)
    if not changed:
        return False

    SubmissionTestCase.objects.filter(submission_id=submission.id).delete()
    submission.batch_results.all().delete()
    from judge.utils.conservative import schedule_submission
    schedule_submission(submission.pk)

    # A separate bridge connection must see the committed dispatch token and
    # full-test policy. The committed QU/P row also makes enable/disable drain
    # this request even if it starts before the network handoff completes.
    transaction.on_commit(lambda: result.append(
        _send_submission(submission, rejudge, batch_rejudge, judge_id, priority, updates['batch_scoring']['dispatch_id'])))
    return True


def _send_submission(submission, rejudge, batch_rejudge, judge_id, priority, dispatch_id=None):
    from .models import Submission

    pending = Submission.objects.filter(pk=submission.pk, status__in=Submission.IN_PROGRESS_GRADING_STATUS)
    if dispatch_id:
        pending = pending.filter(batch_scoring__dispatch_id=dispatch_id)
    run = pending.select_related('problem__mirror_of', 'language', 'user', 'source').first()
    if run is None:
        return False
    dispatch_id = (run.batch_scoring or {}).get('dispatch_id')
    submission = run

    if submission.problem.has_external_problem:
        from judge.external_judge import schedule_external_submission
        return schedule_external_submission(submission, rejudge=rejudge, batch_rejudge=batch_rejudge)

    banned_judges = []
    if hasattr(submission, 'contest'):
        # This is not an actual contest, this is a ContestSubmission
        contest = submission.contest
        # ContestParticipation
        participation = contest.participation
        if participation.live or participation.spectate:
            banned_judges = list(participation.contest.banned_judges.values_list('name', flat=True))

    judge_problem_code = submission.problem.code

    if submission.problem.mirror_of_id is not None:
        # Use the root problem's code for the judge
        judge_problem_code = submission.problem.mirror_of.code

    try:
        response = judge_request({
            'name': 'submission-request',
            'submission-id': submission.id,
            'dispatch-id': dispatch_id,
            'problem-id': judge_problem_code,
            'language': submission.language.key,
            'source': submission.source.source,
            'judge-id': judge_id,
            'banned-judges': banned_judges,
            'priority': BATCH_REJUDGE_PRIORITY if batch_rejudge else (REJUDGE_PRIORITY if rejudge else priority),
        })
    except BaseException:
        logger.exception('Failed to send request to judge')
        Submission.objects.filter(pk=submission.id, status__in=Submission.IN_PROGRESS_GRADING_STATUS,
                                  batch_scoring__dispatch_id=dispatch_id).update(status='IE', result='IE')
        success = False
    else:
        if response['name'] != 'submission-received' or response['submission-id'] != submission.id:
            Submission.objects.filter(pk=submission.id, status__in=Submission.IN_PROGRESS_GRADING_STATUS,
                                      batch_scoring__dispatch_id=dispatch_id).update(status='IE', result='IE')
        _post_update_submission(submission)
        success = True
    return success


def disconnect_judge(judge, force=False):
    judge_request({'name': 'disconnect-judge', 'judge-id': judge.name, 'force': force}, reply=False)


def update_disable_judge(judge):
    judge_request({'name': 'disable-judge', 'judge-id': judge.name, 'is-disabled': judge.is_disabled})


def abort_submission(submission):
    from judge.utils.streaks import record_terminal_update
    from .models import Submission
    # We only want to try to abort a submission if it's still grading, otherwise this can lead to fully graded
    # submissions marked as aborted.
    if submission.status == 'D':
        return
    response = judge_request({'name': 'terminate-submission', 'submission-id': submission.id})
    # This defaults to true, so that in the case the JudgeList fails to remove the submission from the queue,
    # and returns a bad-request, the submission is not falsely shown as "Aborted" when it will still be judged.
    if not response.get('judge-aborted', True):
        record_terminal_update(submission.id, status='AB', result='AB', points=0)
        event.post('sub_%s' % Submission.get_id_secret(submission.id), {'type': 'aborted-submission'})
        _post_update_submission(submission, done=True)
