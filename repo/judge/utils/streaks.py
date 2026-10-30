"""Append-only streak achievements, independent of mutable judging history."""
import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation

import pytz
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.utils import timezone

from judge.models import (Problem, Profile, Submission, StreakSummary, StreakContribution,
                          StreakDay, StreakRun, StreakEpoch, StreakEvent, StreakProgress)


STREAK_TIMEZONE_NAME = 'Asia/Ho_Chi_Minh'
STREAK_TIMEZONE = pytz.timezone(STREAK_TIMEZONE_NAME)


def streak_day(when=None):
    """One calendar for all streak writes and reads, independent of user settings."""
    return (when or timezone.now()).astimezone(STREAK_TIMEZONE).date()


def enabled():
    return getattr(settings, 'STREAKS_ENABLED', False)


def visible_for(profile):
    """Unlisted profiles keep earning streaks, but never expose them in the UI."""
    return enabled() and not profile.is_unlisted


def eligible_problems():
    return Problem.objects.filter(is_public=True, is_organization_private=False).annotate(
        streak_has_org=Exists(Problem.organizations.through.objects.filter(problem_id=OuterRef('pk'))),
    ).filter(streak_has_org=False)


def score_decimal(value):
    try:
        score = Decimal(str(value))
        if not score.is_finite() or score < 0 or score >= Decimal('1e17'):
            return Decimal('0')
        return score.quantize(Decimal('.001'))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal('0')


def progress_generation(problem):
    cumulative = problem.subtask_score_state == 'ON'
    policy = {'mode': 'cumulative' if cumulative else 'normal', 'revision': problem.subtask_score_revision,
              'points': str(score_decimal(problem.points)), 'partial': bool(problem.partial),
              'full': problem.subtask_score_config.get('full', []) if cumulative else []}
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def baseline_progress(user_id, problem, submissions=None):
    """Read once at cutover/mode change, never to reconcile existing progress."""
    from judge.utils.subtask_scoring import cumulative_breakdown
    rows = (Submission.visible.filter(status='D').filter(
        Q(is_pretested=False) | Q(batch_scoring__pretests=[])) if submissions is None else submissions)
    rows = rows.filter(user_id=user_id, problem_id=problem.pk).exclude(
        Q(contest_object__offline_mode=True) | Q(contest_object__has_hidden_subtasks=True) |
        Q(contest_object__end_time__gt=timezone.now()))
    if problem.subtask_score_state == 'ON':
        breakdown = cumulative_breakdown(user_id, problem, submissions=rows)
        return score_decimal(breakdown['points']), {str(row['batch']): row['points'] for row in breakdown['subtasks']}
    return score_decimal(rows.aggregate(points=Max('points'))['points']), {}


def prepare_streak_run(submission, problem, *, initial):
    """Called under the dispatch Problem lock, BEFORE resetting an old result."""
    metadata = {'kind': 'initial' if initial else 'rejudge', 'eligible': False}
    if not initial or not enabled():
        return metadata
    epoch = StreakEpoch.objects.filter(pk=1).first()
    if (epoch is None or submission.pk <= epoch.submission_id or submission.date < epoch.started_at or
            problem.subtask_score_state not in ('OFF', 'ON') or
            not eligible_problems().filter(pk=problem.pk).exists()):
        return metadata
    generation = progress_generation(problem)
    progress = StreakProgress.objects.filter(user_id=submission.user_id, problem_id=problem.pk,
                                             generation=generation).first()
    if progress is None:
        points, subtasks = baseline_progress(submission.user_id, problem)
        progress = StreakProgress.objects.create(user_id=submission.user_id, problem_id=problem.pk,
                                                generation=generation, best_points=points, subtasks=subtasks)
    day = streak_day(submission.date)
    metadata.update(eligible=True, progress_id=progress.pk, day=day.isoformat(),
                    started_at=epoch.started_at.isoformat(), generation=generation,
                    mode='cumulative' if problem.subtask_score_state == 'ON' else 'normal',
                    timezone=STREAK_TIMEZONE_NAME)
    return metadata


def _update_progress(progress, snapshot, *, apply=True):
    """Compare an immutable result while the caller holds the progress lock."""
    before = progress.best_points
    subtasks = progress.subtasks
    if snapshot['mode'] == 'cumulative':
        from judge.utils.subtask_scoring import cumulative_vector_points
        groups = {int(n): group for n, group in snapshot['groups'].items()}
        best = {int(n): max(float(subtasks.get(str(n), 0)), points)
                for n, points in snapshot['result'].items()}
        after = max(before, score_decimal(cumulative_vector_points(best, groups, snapshot['maximum'])))
        subtasks = {str(n): points for n, points in best.items()}
    else:
        after = max(before, score_decimal(snapshot['result']))
    if apply:
        progress.best_points, progress.subtasks = after, subtasks
        progress.save(update_fields=['best_points', 'subtasks'])
    return before, after


def record_completion(submission):
    """Capture the initial final result atomically with its score/batch results.

    The high-water mark advances here, in completion order, rather than when a
    delayed worker happens to consume it. Rejudge/publication signals never call
    this function. The outbox contains no FK to mutable submissions/problems.
    """
    from judge.models import ExamOfflineSubmission
    from judge.utils.subtask_scoring import cumulative_result_vector
    from judge.utils.batch_scoring import scoring_dataset

    metadata = (submission.batch_scoring or {}).get('streak', {})
    if (not enabled() or metadata.get('kind') != 'initial' or not metadata.get('eligible') or
            submission.status != 'D' or scoring_dataset(submission) != 'full'):
        return
    epoch = StreakEpoch.objects.filter(pk=1).first()
    if epoch is None or metadata.get('started_at') != epoch.started_at.isoformat():
        return
    with transaction.atomic():
        progress = StreakProgress.objects.select_for_update().get(pk=metadata['progress_id'])
        if StreakEvent.objects.filter(submission_id=submission.pk).exists():
            return
        problem = submission.problem
        if (progress.generation != metadata['generation'] or progress_generation(problem) != progress.generation or
                not eligible_problems().filter(pk=problem.pk).exists()):
            return
        snapshot = {'mode': metadata['mode'], 'generation': progress.generation,
                    'offline_hidden': submission.offline_hidden}
        if metadata['mode'] == 'cumulative':
            if (submission.batch_scoring or {}).get('cumulative_revision') != problem.subtask_score_revision:
                return
            groups = {item['batch']: item for item in problem.subtask_score_config.get('full', [])}
            vector = cumulative_result_vector(submission, groups)
            if vector is None:
                return
            snapshot.update(result=vector, groups=groups, maximum=problem.points)
        else:
            snapshot['result'] = str(score_decimal(submission.points))
        # Read publication AFTER taking the progress lock. A callback may carry
        # offline_hidden=True from before reveal while waiting for this lock.
        offline = ExamOfflineSubmission.objects.filter(submission_id=submission.pk).values(
            'attempt_problem__attempt_id', 'attempt_problem__attempt__revealed_at').first()
        revealed_at = offline['attempt_problem__attempt__revealed_at'] if offline else None
        # A withheld score must not influence a public award/no-award decision
        # for another submission. Defer its ledger update until publication.
        applied = (bool(revealed_at) and _contest_published(submission.contest_object)
                   if offline else results_published(submission))
        before, after = _update_progress(progress, snapshot, apply=applied)
        day = streak_day(revealed_at or submission.date)
        if offline:
            snapshot['day_source'] = 'offline_reveal'
            if revealed_at:
                snapshot['offline_revealed_at'] = revealed_at.isoformat()
        # Malformed/future dated rows must not produce an invisible future run.
        today = streak_day()
        attempt_id = offline['attempt_problem__attempt_id'] if offline else None
        available_at = timezone.now()
        if submission.contest_object_id:
            available_at = max(available_at, submission.contest_object.end_time)
        StreakEvent.objects.create(progress=progress, submission_id=submission.pk, day=day,
            previous_points=before, points=after, snapshot=snapshot, offline_attempt_id=attempt_id,
            contest_id=submission.contest_object_id, applied=applied,
            pending=(after > before or not applied) and day <= today,
            available_at=available_at)


def results_published(submission):
    """Shared privacy policy for deferred awards and evidence, including contests."""
    if submission.offline_hidden:
        return False
    return _contest_published(submission.contest_object)


def _contest_published(contest):
    return not contest or (contest.end_time <= timezone.now() and
                           not contest.offline_mode and not contest.has_hidden_subtasks)


def _event_published(event, offline_attempt=None):
    from judge.models import Contest, ExamOfflineAttempt
    if event.snapshot.get('offline_hidden') and event.offline_attempt_id is None:
        if not Submission.visible.filter(pk=event.submission_id).exists():
            return False
    if event.offline_attempt_id:
        if offline_attempt is None:
            offline_attempt = ExamOfflineAttempt.objects.filter(pk=event.offline_attempt_id).first()
        if offline_attempt is None or not offline_attempt.revealed_at:
            return False
    if event.contest_id:
        contest = Contest.objects.filter(pk=event.contest_id).first()
        if contest is None or contest.end_time > timezone.now() or contest.offline_mode or contest.has_hidden_subtasks:
            return False
    return True


def publish_offline_streaks(attempt):
    """Freeze ready score improvements at reveal; the worker only awards days.

    Reveal updates submissions before entering here, matching callbacks' lock
    order. Lock ALL event rows before ANY progress rows so a worker cannot hold
    an event while waiting for a progress row held by this transaction.
    """
    if not enabled():
        return 0
    from judge.models import ExamOfflineAttempt
    with transaction.atomic():
        attempt = ExamOfflineAttempt.objects.get(pk=attempt.pk)
        if not attempt.revealed_at:
            return 0
        events = list(StreakEvent.objects.select_for_update().filter(
            offline_attempt_id=attempt.pk, pending=True).order_by('id'))
        if not events:
            return 0
        progress_ids = sorted({event.progress_id for event in events if not event.applied})
        progresses = {row.pk: row for row in StreakProgress.objects.select_for_update().filter(
            pk__in=progress_ids).order_by('pk')}
        earned = set(StreakContribution.objects.filter(
            submission_id__in=[event.submission_id for event in events]).values_list('submission_id', flat=True))
        day, today = streak_day(attempt.revealed_at), streak_day()
        done = 0
        for event in events:
            if event.submission_id in earned or (event.applied and event.snapshot.get('offline_revealed_at')):
                continue
            if not _event_published(event, offline_attempt=attempt):
                continue
            if not event.applied:
                event.previous_points, event.points = _update_progress(progresses[event.progress_id], event.snapshot)
                event.applied = True
            # Only pending work changes policy; already-earned days stay intact.
            event.day = day
            event.snapshot.update(day_source='offline_reveal', offline_revealed_at=attempt.revealed_at.isoformat())
            event.pending = event.points > event.previous_points and day <= today
            event.retry_at = None
            event.save(update_fields=['day', 'snapshot', 'previous_points', 'points', 'applied', 'pending', 'retry_at'])
            done += 1
        return done


def _add_day(user_id, day, summary):
    """Only add/extend/merge runs; earned days can never be removed."""
    _, created = StreakDay.objects.get_or_create(user_id=user_id, day=day)
    if created:
        left = StreakRun.objects.filter(user_id=user_id, end=day - timedelta(days=1)).first()
        right = StreakRun.objects.filter(user_id=user_id, start=day + timedelta(days=1)).first()
        start, end = left.start if left else day, right.end if right else day
        if left:
            left.delete()
        if right:
            right.delete()
        length = (end - start).days + 1
        StreakRun.objects.create(user_id=user_id, start=start, end=end, length=length)
        summary.longest = max(summary.longest, length)
        if summary.last_day is None or end >= summary.last_day:
            summary.last_day, summary.current_length = end, length
    summary.save()


def process_event(event_id):
    initial = StreakEvent.objects.filter(pk=event_id).values(
        'progress__user_id', 'offline_attempt_id', 'applied', 'pending', 'snapshot').first()
    if initial is None or not initial['pending']:
        return False
    if initial['offline_attempt_id'] and (not initial['applied'] or
                                         not initial['snapshot'].get('offline_revealed_at')):
        from judge.models import ExamOfflineAttempt
        attempt = ExamOfflineAttempt.objects.filter(pk=initial['offline_attempt_id']).first()
        if attempt and attempt.revealed_at:
            # Also handles pending events left by the previous deployment.
            publish_offline_streaks(attempt)
    with transaction.atomic():
        # Shared user lock prevents two problems racing to extend the same day.
        profile = Profile.objects.get(pk=initial['progress__user_id'])
        summary, _ = StreakSummary.objects.get_or_create(user=profile, defaults={'timezone': STREAK_TIMEZONE_NAME})
        summary = StreakSummary.objects.select_for_update().get(pk=profile.pk)
        summary.timezone = STREAK_TIMEZONE_NAME
        event = StreakEvent.objects.select_for_update().select_related('progress').get(pk=event_id)
        now = timezone.now()
        if not event.pending or event.available_at > now or (event.retry_at and event.retry_at > now):
            return False
        if not event.applied and not _event_published(event):
            event.retry_at = now + timedelta(seconds=60)
            event.save(update_fields=['retry_at'])
            return False
        if not event.applied:
            progress = StreakProgress.objects.select_for_update().get(pk=event.progress_id)
            before, after = _update_progress(progress, event.snapshot)
            event.previous_points, event.points, event.applied = before, after, True
        if event.points <= event.previous_points:
            event.pending = False
            event.retry_at = None
            event.save(update_fields=['previous_points', 'points', 'applied', 'pending', 'retry_at'])
            return False
        StreakContribution.objects.get_or_create(submission_id=event.submission_id, defaults={
            'user_id': profile.pk, 'problem_id': event.progress.problem_id, 'day': event.day,
            'previous_points': event.previous_points, 'points': event.points})
        _add_day(profile.pk, event.day, summary)
        event.pending = False
        event.retry_at = None
        event.save(update_fields=['previous_points', 'points', 'applied', 'pending', 'retry_at'])
        return True


# Rating-style color ladder (Codeforces/AtCoder order), keyed by streak length.
STREAK_TIERS = [(365, 'legend'), (200, 'crimson'), (100, 'red'), (60, 'orange'), (30, 'violet'),
                (14, 'blue'), (7, 'cyan'), (3, 'green'), (1, 'gray'), (0, 'muted')]


def _tier(current):
    return next(name for minimum, name in STREAK_TIERS if current >= minimum)


def _public_summary(profile, summary, now):
    today = streak_day(now)
    current = 0
    if summary and summary.last_day in (today, today - timedelta(days=1)):
        current = summary.current_length
    return {'current': current, 'longest': summary.longest if summary else 0,
            'today': today, 'kept_today': bool(summary and summary.last_day == today),
            'tier': _tier(current), 'updated_at': summary.updated_at if summary else None}


def public_summary(profile, now=None):
    if profile.is_unlisted:
        return None
    summary = StreakSummary.objects.filter(user_id=profile.pk).first() if enabled() else None
    return _public_summary(profile, summary, now or timezone.now())


def leaderboard_annotations(now=None):
    """Use the same Vietnam day bounds as public_summary for every profile."""
    from django.db.models import Case, F, IntegerField, Value, When
    from django.db.models.functions import Coalesce

    today = streak_day(now)
    stored = Coalesce(F('streaksummary__current_length'), Value(0), output_field=IntegerField())
    current = Case(
        When(streaksummary__last_day__range=(today - timedelta(days=1), today), then=stored),
        default=Value(0), output_field=IntegerField(),
    )
    return {
        'streak_current': current,
        'streak_longest': Coalesce(F('streaksummary__longest'), Value(0), output_field=IntegerField()),
    }


def public_summary_map(profiles, now=None):
    """One StreakSummary query for a whole page of profile objects."""
    result = {}
    if not enabled():
        return result
    unique = {}
    for profile in profiles:
        if not profile.is_unlisted:
            unique.setdefault(profile.pk, profile)
    if not unique:
        return result
    summaries = StreakSummary.objects.in_bulk(unique.keys())
    now = now or timezone.now()
    for pk, profile in unique.items():
        result[pk] = _public_summary(profile, summaries.get(pk), now)
    return result


def record_terminal_update(submission_id, **updates):
    """Error/abort paths never award or revoke achievements."""
    return Submission.objects.filter(pk=submission_id).update(**updates)


# Compatibility for previously imported/queued v1 callers during a rolling
# deployment. No history scan or invalidation may run after the cutover.
def queue_pair(*args, **kwargs):
    return None


def queue_scope(*args, **kwargs):
    return None
