"""Practice scores, shared by profiles, Users, PP breakdown and problem pages.

Batch numbers are meaningful only within one locked rebuild revision. Individual
submission scores and contest scoring remain independent of this aggregation.
"""
from contextlib import contextmanager
from math import fsum, isclose, isfinite

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.utils.batch_scoring import has_full_score, scaled_score, scoring_dataset, scoring_snapshot


PROTECTED_PROBLEM_FIELDS = frozenset((
    'code', 'points', 'partial', 'short_circuit', 'time_limit', 'memory_limit', 'is_organization_private',
    'is_manually_managed', 'mirror_of', 'mirror_of_id', 'mirror_root', 'mirror_root_id',
))
LOCKED_MESSAGE = _(
    'Cumulative subtask scoring locks the test data. Turn it off before editing tests or scoring settings.')
ACTIVE_STATUSES = ('QU', 'P', 'G')


@contextmanager
def test_data_edit(problem_ids):
    """Serialize test writes with enable/disable, including bulk writes/imports."""
    from judge.models import Problem, Submission

    ids = sorted(set(problem_ids))
    with transaction.atomic():
        rows = list(Problem.objects.select_for_update().filter(pk__in=ids).order_by('pk'))
        if any(row.subtask_tests_locked for row in rows):
            raise ValidationError(LOCKED_MESSAGE)
        selected = [row.pk for row in rows if row.subtask_score_revision]
        if selected and Submission.objects.filter(problem_id__in=selected, status__in=ACTIVE_STATUSES).exists():
            raise ValidationError(_('Wait for queued and running submissions to finish before editing these tests.'))
        yield


def test_data_instance_edit(instance, field):
    ids = [getattr(instance, field)]
    if instance.pk and not instance._state.adding:
        previous = type(instance).objects.filter(pk=instance.pk).values_list(field, flat=True).first()
        if previous is not None:
            ids.append(previous)
    return test_data_edit(ids)


def validate_problem_edit(problem):
    from judge.models import Problem

    if problem.__dict__.get('mirror_of_id'):
        if Problem.objects.filter(pk=problem.mirror_of_id).exclude(subtask_score_state='OFF').exists():
            raise ValidationError(_('A problem with locked cumulative scoring tests cannot become a mirror source.'))
    if not problem.pk or problem._state.adding:
        return
    # Avoid loading deferred values on the instance (which can recurse through save).
    fields = [name for name in PROTECTED_PROBLEM_FIELDS if name in problem.__dict__]
    if not fields:
        return
    previous = Problem.objects.filter(pk=problem.pk).values('subtask_score_state', *fields).first()
    if previous and previous['subtask_score_state'] != 'OFF':
        if any(previous[name] != problem.__dict__[name] for name in fields):
            raise ValidationError(LOCKED_MESSAGE)


def _validate_subtask_layout(snapshot):
    full = snapshot.get('full')
    if not full:
        raise ValidationError(_('Define at least one full-test subtask before enabling cumulative scoring.'))
    if fsum(item['total'] or 0 for item in full) <= 0:
        raise ValidationError(_('The full-test subtasks must have a positive total score.'))
    for dataset in ('full', 'pretests'):
        for batch in snapshot.get(dataset) or []:
            if not batch['batch']:
                raise ValidationError(_(
                    'Every test, including zero-point samples and pretests, must belong to a subtask.'))
            if batch['count'] <= 0 or batch['total'] is None or batch['total'] < 0:
                raise ValidationError(_('Every subtask must contain tests and have non-negative points.'))
            if batch['mode'] not in ('min', 'sum'):
                raise ValidationError(_('Unsupported subtask scoring mode.'))


def _managed_configuration(problem, check_files):
    """Validate web-managed tests against their DB rows and generated init.yml."""
    from judge.models import ProblemData, problem_data_storage

    data = ProblemData.objects.filter(problem=problem).first()
    if data is None or data.generator:
        raise ValidationError(_('Cumulative scoring requires an explicit test dataset without a generator.'))
    snapshot = scoring_snapshot(problem)
    _validate_subtask_layout(snapshot)
    if check_files:
        if data.feedback or not problem_data_storage.exists('%s/init.yml' % problem.code):
            raise ValidationError(_('Compile valid test data before enabling cumulative scoring.'))
        # Compile the canonical structure in memory as well: the formset alone
        # does not reject empty groups or missing input/checker files.
        from zipfile import ZipFile
        import yaml
        from judge.utils.problem_data import ProblemDataCompiler
        try:
            with data.zipfile.open('rb') as archive:
                with ZipFile(archive) as zipfile:
                    files = zipfile.namelist()
            compiler = ProblemDataCompiler(problem, data, problem.cases.order_by('order', 'id'), files,
                                           persist_cases=False)
            expected = compiler.make_init()
            with problem_data_storage.open('%s/init.yml' % problem.code) as source:
                actual = yaml.safe_load(source)
            if actual != expected:
                raise ValueError(_('The compiled test configuration is outdated. Apply the test data before enabling.'))
        except Exception as exc:
            raise ValidationError(_('The test archive or configuration could not be validated: %(error)s'),
                                  params={'error': str(exc)})
    return snapshot


def validate_configuration(problem, check_files=True):
    from judge.models import Problem, ProblemData, Submission

    if problem.is_organization_private:
        raise ValidationError(_('Cumulative subtask scoring is not available for organization-private problems.'))
    if not problem.partial or not isfinite(problem.points) or problem.points <= 0:
        raise ValidationError(_('Enable partial scoring and set positive problem points first.'))
    if problem.is_mirror or Problem.objects.filter(mirror_root_id=problem.pk).exists():
        raise ValidationError(_('Shared or mirrored test data is not supported for cumulative scoring.'))
    if problem.has_external_problem:
        raise ValidationError(_('Cumulative scoring requires locally managed, explicit subtasks.'))
    if Submission.objects.filter(problem=problem, locked_after__lt=timezone.now()).exists():
        raise ValidationError(_(
            'This problem has locked submissions. Resolve their rejudging policy before enabling cumulative scoring.'))
    if Submission.objects.filter(problem=problem, contest_object__end_time__gt=timezone.now()).exists():
        raise ValidationError(_('Wait until contests using this problem have ended before rebuilding scores.'))
    if Submission.objects.filter(problem=problem, offline_hidden=True).exists():
        raise ValidationError(_('Publish offline results before rebuilding this problem.'))

    if problem.is_manually_managed:
        # Manual files are authoritative; do not run the web's data compiler.
        from judge.utils.manual_subtasks import manual_configuration

        data = ProblemData.objects.filter(problem=problem).first()
        if data and data.generator:
            raise ValidationError(_('Cumulative scoring requires an explicit test dataset without a generator.'))
        snapshot = manual_configuration(problem, scoring_snapshot(problem))
        _validate_subtask_layout(snapshot)
        return snapshot
    else:
        return _managed_configuration(problem, check_files)


def eligible_submissions():
    from judge.models import Submission

    # A public cumulative score must not reveal an unfinished contest's results.
    return (Submission.visible.filter(status='D')
            .exclude(contest_object__end_time__gt=timezone.now()))


def cumulative_breakdown(user_id, problem):
    """Return test-scale scores for display and scaled points for user totals."""
    rows = (eligible_submissions().filter(user_id=user_id, problem=problem,
                batch_scoring__cumulative_revision=problem.subtask_score_revision)
            .order_by('date', 'id').prefetch_related('batch_results'))
    groups = {item['batch']: item for item in problem.subtask_score_config.get('full', [])}
    best = {number: {'batch': number, 'points': 0.0, 'total': item['total'], 'submission_id': None,
                     'date': None} for number, item in groups.items()}
    for submission in rows:
        if scoring_dataset(submission) != 'full':
            continue
        results = list(submission.batch_results.all())
        if {row.batch for row in results} != set(groups):
            continue
        for row in results:
            if (not isfinite(row.points) or not isfinite(row.total) or row.total < 0 or
                    not isclose(row.total, groups[row.batch]['total'], rel_tol=1e-8, abs_tol=1e-8) or row.points < 0 or
                    (row.points > row.total and not has_full_score(row.points, row.total))):
                break
        else:
            for row in results:
                current = best[row.batch]
                if row.points > current['points']:
                    current.update(points=min(row.points, row.total), submission_id=submission.pk,
                                   date=submission.date)
    case_total = fsum(item['total'] for item in best.values())
    case_points = fsum(item['points'] for item in best.values())
    points = scaled_score(case_points, case_total, problem.points)
    return {'points': points, 'total': problem.points, 'case_points': case_points, 'case_total': case_total,
            'subtasks': list(best.values()), 'cumulative': True}


def user_problem_scores(user_id, problems=None, *, rebuilding_problem=None):
    """One score source for totals and displays; unselected problems keep MAX."""
    from judge.models import Problem, Submission

    if problems is None:
        problems = Problem.get_public_problems()
    legacy = (Submission.visible.filter(user_id=user_id, problem__in=problems, points__isnull=False)
              .values('problem_id').annotate(points=Max('points')))
    scores = {row['problem_id']: row['points'] for row in legacy}
    cumulative = Q(subtask_score_state='ON')
    # Only the rebuild's atomic profile refresh may see its unpublished mode.
    # Normal requests keep the old rule until profiles and ON commit together.
    if rebuilding_problem is not None:
        cumulative |= Q(pk=rebuilding_problem.pk, subtask_score_state='REBUILDING',
                        subtask_score_revision=rebuilding_problem.subtask_score_revision)
    enabled = problems.filter(
        cumulative, is_organization_private=False,
        pk__in=Submission.visible.filter(user_id=user_id).values('problem_id'),
    )
    for problem in enabled:
        scores[problem.pk] = cumulative_breakdown(user_id, problem)['points']
    return scores


def refresh_problem_profiles(problem_id, *, rebuilding_problem=None):
    from judge.models import Profile, Submission

    ids = Submission.objects.filter(problem_id=problem_id).values_list('user_id', flat=True).distinct()
    for profile in Profile.objects.filter(pk__in=ids).order_by('pk').iterator():
        profile._updating_stats_only = True
        profile.calculate_points(rebuilding_problem=rebuilding_problem)


def require_submission_admission(problem_id):
    from judge.models import Problem

    if Problem.objects.filter(pk=problem_id, subtask_score_state__in=('REBUILDING', 'STOPPING', 'FAILED')).exists():
        raise ValidationError(_('Subtask scores are being rebuilt. Please submit again after the rebuild finishes.'))


def schedule_contest_score_refresh(submission):
    """Release full-test practice scores when an active contest finishes."""
    if submission.problem.subtask_score_state != 'ON' or not submission.contest_object_id:
        return
    end = submission.contest_object.end_time
    remaining = (end - timezone.now()).total_seconds()
    if remaining <= 0:
        return
    from django.core.cache import cache
    from judge.tasks.subtask_scoring import refresh_contest_subtask_scores

    def enqueue():
        key = 'cumulative:contest-refresh:%s:%s:%s' % (
            submission.contest_object_id, submission.problem_id, end.isoformat())
        if cache.add(key, True, int(remaining) + 60):
            try:
                refresh_contest_subtask_scores.apply_async(
                    args=[submission.contest_object_id, submission.problem_id], eta=end)
            except Exception:
                cache.delete(key)
                raise
    transaction.on_commit(enqueue)
