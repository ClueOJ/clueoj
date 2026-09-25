"""Incremental high-water marks; deterministic repair for out-of-order results."""
from datetime import timedelta
from decimal import Decimal

import pytz
from django.conf import settings
from django.db import transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.utils import timezone

from judge.models import (Problem, Profile, Submission, StreakSummary, StreakProblemState,
                          StreakContribution, StreakDay, StreakRun, StreakRebuildRequest)


def enabled():
    return getattr(settings, 'STREAKS_ENABLED', False)


def eligible_problems():
    return Problem.objects.filter(is_public=True, is_organization_private=False).annotate(
        streak_has_org=Exists(Problem.organizations.through.objects.filter(problem_id=OuterRef('pk'))),
    ).filter(streak_has_org=False)


def queue_pair(user_id, problem_id, submission=None):
    if not enabled():
        return
    with transaction.atomic():
        state, _ = StreakProblemState.objects.select_for_update().get_or_create(
            user_id=user_id, problem_id=problem_id,
        )
        state.pending = True
        state.retry_at = None
        if submission is None:
            state.rebuild = True
        elif state.dirty_date is None or (submission.date, submission.pk) < (state.dirty_date, state.dirty_id):
            state.dirty_date, state.dirty_id = submission.date, submission.pk
        state.save(update_fields=['pending', 'rebuild', 'dirty_date', 'dirty_id', 'retry_at'])


def queue_scope(kind, object_id):
    if enabled():
        # update_or_create locks an existing request; resetting the cursor cannot
        # lose a metadata edit arriving while a dispatcher is expanding it.
        StreakRebuildRequest.objects.update_or_create(kind=kind, object_id=object_id, defaults={'cursor': 0})


def expand_request(request_id, batch_size=100):
    with transaction.atomic():
        request = StreakRebuildRequest.objects.select_for_update().filter(pk=request_id).first()
        if request is None:
            return
        by_user = request.kind == 'user'
        field = 'problem_id' if by_user else 'user_id'
        lookup = {'user_id' if by_user else 'problem_id': request.object_id}
        # The state table retains deleted submissions/problems for repair.
        ids = set(Submission.objects.filter(**lookup, **{field + '__gt': request.cursor})
                  .order_by(field).values_list(field, flat=True).distinct()[:batch_size])
        ids.update(StreakProblemState.objects.filter(**lookup, **{field + '__gt': request.cursor})
                   .order_by(field).values_list(field, flat=True).distinct()[:batch_size])
        ids = sorted(ids)[:batch_size]
        for value in ids:
            user_id, problem_id = (request.object_id, value) if by_user else (value, request.object_id)
            if Profile.objects.filter(pk=user_id).exists():
                queue_pair(user_id, problem_id)
        if ids:
            request.cursor = ids[-1]
            request.save(update_fields=['cursor'])
        else:
            request.delete()


def _tier(current):
    return next(name for minimum, name in [(365, 'legend'), (100, 'purple'), (30, 'red'),
                                           (7, 'orange'), (1, 'warm'), (0, 'muted')] if current >= minimum)


def _refresh_days_and_runs(user_id, affected, summary):
    """Repair only run segments adjacent to changed days, never the full history."""
    changed = set()
    for start in range(0, len(affected), 500):
        days = affected[start:start + 500]
        existing = set(StreakDay.objects.filter(user_id=user_id, day__in=days).values_list('day', flat=True))
        valid = set(StreakContribution.objects.filter(user_id=user_id, day__in=days)
                    .values_list('day', flat=True).distinct())
        removed, added = existing - valid, valid - existing
        if removed:
            StreakDay.objects.filter(user_id=user_id, day__in=removed).delete()
        if added:
            StreakDay.objects.bulk_create([StreakDay(user_id=user_id, day=d) for d in added], batch_size=500)
        changed |= removed | added
    if not changed:
        # Further improvements on an already credited day need no run rewrite.
        summary.save()
        return
    # A changed day can split, extend or merge runs touching itself or its
    # neighbours, so repair windows expand by one day and overlap-merge.
    windows = []
    for day in sorted(changed):
        if windows and day - timedelta(days=1) <= windows[-1][1]:
            windows[-1][1] = day + timedelta(days=1)
        else:
            windows.append([day - timedelta(days=1), day + timedelta(days=1)])
    for low, high in windows:
        stale = list(StreakRun.objects.filter(user_id=user_id, start__lte=high, end__gte=low))
        span_low = min([low] + [run.start for run in stale])
        span_high = max([high] + [run.end for run in stale])
        runs = []
        dates = StreakDay.objects.filter(user_id=user_id, day__range=(span_low, span_high)) \
            .order_by('day').values_list('day', flat=True)
        for day in dates.iterator(chunk_size=1000):
            if runs and day == runs[-1].end + timedelta(days=1):
                runs[-1].end = day
                runs[-1].length += 1
            else:
                runs.append(StreakRun(user_id=user_id, start=day, end=day, length=1))
        StreakRun.objects.filter(pk__in=[run.pk for run in stale]).delete()
        StreakRun.objects.bulk_create(runs, batch_size=500)
    last_day = StreakDay.objects.filter(user_id=user_id).order_by('-day').values_list('day', flat=True).first()
    summary.last_day = last_day
    summary.current_length = 0
    if last_day is not None:
        summary.current_length = StreakRun.objects.filter(
            user_id=user_id, end=last_day).values_list('length', flat=True).first() or 0
    summary.longest = StreakRun.objects.filter(user_id=user_id).aggregate(m=Max('length'))['m'] or 0
    summary.save()


def process_pair(state_id):
    initial = StreakProblemState.objects.filter(pk=state_id).values('user_id').first()
    if not initial:
        return
    with transaction.atomic():
        # All workers use this lock order, separate from Profile/offline locks.
        profile = Profile.objects.filter(pk=initial['user_id']).only('timezone').first()
        if profile is None:
            return
        summary, _ = StreakSummary.objects.get_or_create(user=profile, defaults={'timezone': profile.timezone})
        summary = StreakSummary.objects.select_for_update().get(pk=profile.pk)
        state = StreakProblemState.objects.select_for_update().get(pk=state_id)
        if not state.pending:
            return
        tz = pytz.timezone(profile.timezone)
        rebuild = state.rebuild or state.last_date is None or (
            state.dirty_date is not None and (state.dirty_date, state.dirty_id) <= (state.last_date, state.last_id)
        )
        contributions = StreakContribution.objects.filter(user_id=profile.pk, problem_id=state.problem_id)
        affected = set()
        if rebuild:
            affected.update(contributions.values_list('day', flat=True))
            contributions.delete()
            state.best_points, state.solved = Decimal('0'), False
            state.last_date, state.last_id = None, 0
        is_eligible = eligible_problems().filter(pk=state.problem_id).exists()
        if is_eligible:
            rows = Submission.visible.filter(user_id=profile.pk, problem_id=state.problem_id,
                                             status='D', is_pretested=False, points__isnull=False)
            if not rebuild:
                rows = rows.filter(Q(date__gt=state.last_date) | Q(date=state.last_date, id__gt=state.last_id))
            rows = rows.order_by('date', 'id').values_list('id', 'date', 'points', 'result', 'case_points', 'case_total')
            inserts = []
            for sid, date, points, result, case_points, case_total in rows.iterator(chunk_size=1000):
                score = Decimal(str(points)).quantize(Decimal('.001'))
                if score.is_finite() and score > state.best_points and not state.solved:
                    day = date.astimezone(tz).date()
                    inserts.append(StreakContribution(user_id=profile.pk, problem_id=state.problem_id,
                                                      submission_id=sid, day=day,
                                                      previous_points=state.best_points, points=score))
                    affected.add(day)
                state.best_points = max(state.best_points, score)
                state.solved |= result == 'AC' and case_total > 0 and case_points >= case_total
                state.last_date, state.last_id = date, sid
                if len(inserts) >= 500:
                    StreakContribution.objects.bulk_create(inserts, batch_size=500)
                    inserts = []
            StreakContribution.objects.bulk_create(inserts, batch_size=500)
        state.pending = state.rebuild = False
        state.dirty_date = state.dirty_id = state.retry_at = None
        state.failures = 0
        state.save()
        summary.timezone = profile.timezone
        if affected:
            _refresh_days_and_runs(profile.pk, sorted(affected), summary)
        else:
            summary.save()


def _public_summary(profile, summary, now):
    today = now.astimezone(pytz.timezone(profile.timezone)).date()
    current = 0
    if summary and summary.last_day in (today, today - timedelta(days=1)):
        current = summary.current_length
    return {'current': current, 'longest': summary.longest if summary else 0,
            'today': today, 'kept_today': bool(summary and summary.last_day == today),
            'tier': _tier(current), 'updated_at': summary.updated_at if summary else None}


def public_summary(profile, now=None):
    summary = StreakSummary.objects.filter(user_id=profile.pk).first() if enabled() else None
    return _public_summary(profile, summary, now or timezone.now())


def leaderboard_annotations(now=None):
    """Live current streak matches public_summary: last_day is today or yesterday in the profile timezone."""
    from collections import defaultdict

    from django.db.models import Case, F, IntegerField, Value, When
    from django.db.models.functions import Coalesce

    now = now or timezone.now()
    buckets = defaultdict(list)
    for name in pytz.all_timezones:
        local_today = now.astimezone(pytz.timezone(name)).date()
        buckets[local_today - timedelta(days=1)].append(name)
    stored = Coalesce(F('streaksummary__current_length'), Value(0), output_field=IntegerField())
    current = Case(
        *[When(timezone__in=names, streaksummary__last_day__gte=cutoff, then=stored) for cutoff, names in buckets.items()],
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
        unique.setdefault(profile.pk, profile)
    if not unique:
        return result
    summaries = StreakSummary.objects.in_bulk(unique.keys())
    now = now or timezone.now()
    for pk, profile in unique.items():
        result[pk] = _public_summary(profile, summaries.get(pk), now)
    return result


def record_terminal_update(submission_id, **updates):
    """Bulk update bridge/error paths with durable invalidation in one commit."""
    with transaction.atomic():
        changed = Submission.objects.filter(pk=submission_id).update(**updates)
        if changed and enabled():
            sub = Submission.visible.filter(pk=submission_id).only('user_id', 'problem_id', 'date').first()
            if sub:
                queue_pair(sub.user_id, sub.problem_id, sub)
        return changed
