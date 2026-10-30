"""Conservative scoring is a derived view; it never changes a judge policy/result.

Defaults live on Problem. A contest pins its own configuration until an editor
explicitly rebuilds it. Readers use persisted results, never raw testcases.
"""
import hashlib
import json
import logging
from collections import defaultdict
from math import fsum, isclose, isfinite

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.utils.batch_scoring import has_full_score, scaled_score, scoring_dataset, scoring_snapshot
from judge.utils.problem_mirror import test_data_source

logger = logging.getLogger(__name__)
ACTIVE = ('BUILDING', 'ON')
SCORING_VERSION = 2


def aborted_filter(prefix='submission__'):
    return Q(**{prefix + 'status': 'AB'}) | Q(**{prefix + 'result': 'AB'})


def is_aborted(submission):
    return submission.status == 'AB' or getattr(submission, 'result', None) == 'AB'


def ignored_abort():
    return {'status': 'ignored', 'reason': 'aborted'}


def subtask_layout(layout):
    """Only batch numbering and maxima define conservative compatibility."""
    if not layout or any(not row.get('batch') or row.get('total') is None for row in layout):
        raise ValueError('Every testcase must belong to a subtask with a known maximum')
    result = [{'batch': int(row['batch']), 'total': float(row['total'])} for row in layout]
    if len({row['batch'] for row in result}) != len(result):
        raise ValueError('Duplicate subtask numbers')
    return sorted(result, key=lambda row: row['batch'])


def same_subtasks(left, right):
    try:
        left, right = subtask_layout(left), subtask_layout(right)
        return len(left) == len(right) and all(
            a['batch'] == b['batch'] and isclose(a['total'], b['total'], rel_tol=1e-8, abs_tol=1e-8)
            for a, b in zip(left, right))
    except (ValueError, TypeError, KeyError):
        return False


def dependencies_match_layout(stored, layout):
    if not stored:
        return True
    if 'subtasks' in stored:
        return same_subtasks(stored['subtasks'], layout)
    # Earlier defaults stored an opaque testcase identity hash. Preserve their
    # explicit numbering instead of requiring the old testcase structure.
    graph = stored.get('dependencies', {})
    return not graph or set(map(int, graph)) == {row['batch'] for row in layout}


def validate_dependencies(dependencies, numbers):
    numbers = set(map(int, numbers))
    if not isinstance(dependencies, dict):
        raise ValidationError(_('Invalid conservative dependencies.'))
    graph = {str(number): [] for number in numbers}
    try:
        for number, required in dependencies.items():
            if int(number) not in numbers or not isinstance(required, list):
                raise ValueError()
            graph[str(int(number))] = sorted(set(map(int, required)))
            if not set(graph[str(int(number))]) <= numbers or int(number) in graph[str(int(number))]:
                raise ValueError()
    except (ValueError, TypeError):
        raise ValidationError(_('Dependencies must reference other existing subtasks.'))
    visiting, visited = set(), set()

    def visit(number):
        if number in visiting:
            raise ValidationError(_('Subtask dependencies must not contain a cycle.'))
        if number not in visited:
            visiting.add(number)
            for required in graph[str(number)]:
                visit(required)
            visiting.remove(number)
            visited.add(number)

    for number in numbers:
        visit(number)
    return graph


def problem_layout(problem):
    problem = test_data_source(problem)
    layout = scoring_snapshot(problem)['full']
    if layout is None:
        # Manually managed datasets have no web-owned layout. A finalized full
        # run provides positions/weights, not proof of an unknown dependency.
        previous = problem.submission_set.filter(status='D', is_pretested=False).exclude(result='AB').order_by('-id').first()
        groups = defaultdict(list)
        if previous:
            for case in previous.test_cases.all():
                groups[case.batch or 0].append(case)
        layout = [{'batch': batch, 'count': len(group), 'total': fsum(c.total or 0 for c in group)}
                  for batch, group in sorted(groups.items())]
    if (not layout or not any(item['batch'] for item in layout) or
            any(item['count'] <= 0 or item['total'] is None or not isfinite(item['total']) or item['total'] < 0 for item in layout) or
            fsum(item['total'] or 0 for item in layout) <= 0):
        raise ValidationError(_('Define full-test subtasks before configuring Conservative Ranking.'))
    if any(not item['batch'] for item in layout):
        raise ValidationError(_('Every full-test testcase must belong to a subtask for Conservative Ranking.'))
    token = hashlib.sha256(json.dumps(subtask_layout(layout), sort_keys=True).encode()).hexdigest()
    return layout, token


def dependency_context(problem):
    if problem.is_mirror:
        return {'conservative_rows': [], 'conservative_layout': ''}
    try:
        layout, token = problem_layout(problem)
    except ValidationError as exc:
        return {'conservative_rows': [], 'conservative_layout': '', 'conservative_dependency_error': ' '.join(exc.messages)}
    stored = problem.conservative_config or {}
    stale = not dependencies_match_layout(stored, layout)
    graph = stored.get('dependencies', {}) if not stale else {}
    rows = [{'number': item['batch'], 'total': item['total'], 'required': graph.get(str(item['batch']), [])}
            for item in layout if item['batch']]
    return {'conservative_rows': rows, 'conservative_layout': token, 'conservative_dependency_stale': stale}


def save_dependencies(problem, values):
    from judge.models import Problem
    with transaction.atomic():
        problem = Problem.objects.select_for_update().get(pk=problem.pk)
        if problem.is_mirror:
            raise ValidationError(_('Mirror problems inherit Conservative dependencies from their root. Edit the root instead.'))
        layout, token = problem_layout(problem)
        if values.get('conservative-layout') != token:
            raise ValidationError(_('The test structure changed. Reload this page before saving dependencies.'))
        numbers = [item['batch'] for item in layout if item['batch']]
        expected = {'conservative-dep-%s' % number for number in numbers}
        if not {key for key in values if key.startswith('conservative-dep-')} <= expected:
            raise ValidationError(_('Invalid conservative dependencies.'))
        graph = validate_dependencies({str(n): values.getlist('conservative-dep-%s' % n) for n in numbers}, numbers)
        config = {'version': 2, 'layout': token, 'subtasks': subtask_layout(layout), 'dependencies': graph,
                  'revision': (problem.conservative_config or {}).get('revision', 0) + 1}
        Problem.objects.filter(pk=problem.pk).update(conservative_config=config)
        return config


def snapshot_problem(problem):
    layout, token = problem_layout(problem)
    source = test_data_source(problem)
    stored = source.conservative_config or {}
    if not dependencies_match_layout(stored, layout):
        raise ValidationError(_('The test structure changed. Save Conservative dependencies again for %(code)s.'),
                              params={'code': source.code})
    graph = validate_dependencies(stored.get('dependencies', {}), [i['batch'] for i in layout if i['batch']])
    native = {}
    if source.is_manually_managed:
        import yaml
        from judge.models import problem_data_storage
        try:
            with problem_data_storage.open('%s/init.yml' % source.code) as stream:
                init = yaml.safe_load(stream) or {}
            number = 0
            for item in init.get('test_cases', []):
                if 'batched' in item:
                    number += 1
                    native[str(number)] = item.get('dependencies', [])
            native = validate_dependencies(native, graph)
        except (OSError, ValueError, TypeError):
            # Unknown native dependencies cannot justify skipped tests below.
            native = {}
    combined = {n: sorted(set(required + native.get(n, []))) for n, required in graph.items()}
    validate_dependencies(combined, graph)
    return {'layout': layout, 'layout_token': token, 'dependencies': combined, 'native_dependencies': native,
            'dependency_revision': stored.get('revision', 0), 'problem_id': problem.pk,
            'scoring_version': SCORING_VERSION}


def calculate(submission, config, maximum, partial):
    """MIN over each batch's own tests and its dependency closure.

    Propagate normalized ratios, not awarded points: a 30-point dependency with
    ratio .6 caps a 50-point dependent batch at 30 points, not at zero or 18.
    Borrowing tests never changes the dependent batch's maximum or test weights.
    """
    if is_aborted(submission):
        return ignored_abort()
    if submission.status == 'CE':
        return {'status': 'valid', 'points': 0.0, 'total': 0.0, 'batches': {}, 'full': False}
    if submission.status in ('QU', 'P', 'G'):
        return {'status': 'pending'}
    if submission.status != 'D':
        raise ValidationError(_('This submission has no completed test results.'))
    if scoring_dataset(submission) != 'full':
        raise ValidationError(_('Full-test results are required for Conservative Ranking.'))
    cases = list(submission.test_cases.order_by('case'))
    layout = config['layout']
    groups = defaultdict(list)
    for case in cases:
        if case.batch is None or case.batch <= 0:
            raise ValidationError(_('Every full-test testcase must belong to a subtask for Conservative Ranking.'))
        if (case.points is None or case.total is None or not isfinite(case.points) or not isfinite(case.total) or
                case.total < 0 or case.points < 0 or (case.points > case.total and not has_full_score(case.points, case.total))):
            raise ValidationError(_('Stored testcase points are incomplete or invalid.'))
        groups[case.batch or 0].append(case)
    actual = [{'batch': batch, 'total': fsum(c.total for c in rows)} for batch, rows in groups.items()]
    if not same_subtasks(actual, layout):
        raise ValidationError(_('Subtask counts or corresponding maximum points do not match.'))
    own = {}
    for batch, rows in groups.items():
        total = fsum(c.total for c in rows)
        ran = [c for c in rows if c.status != 'SC']
        failed = any(c.total > 0 and c.points == 0 for c in ran)
        skipped = len(ran) != len(rows)
        ratios = [c.points / c.total for c in ran if c.total > 0]
        if total > 0:
            ratio = 0.0 if failed else (None if skipped else min(ratios, default=0))
        else:
            # A zero-point group can still contribute its binary test
            # outcomes to another group. It must not pass just because 0=0.
            zero_failed = any(c.status != 'AC' for c in ran)
            ratio = 0.0 if zero_failed else (None if skipped else 1.0)
        points = total * ratio if ratio is not None else (0.0 if total == 0 else None)
        own[str(batch)] = {'raw_points': points, 'points': points, 'total': total,
                          'raw_ratio': ratio, 'ratio': ratio, 'limited_by_dependencies': False}
    graph = config['dependencies']
    resolved = {}

    def resolve(number):
        key = str(number)
        if key in resolved:
            return resolved[key]
        item = dict(own[key])
        required = [resolve(n) for n in graph.get(key, [])]
        ratios = [item['raw_ratio']] + [row['ratio'] for row in required]
        # A known zero determines MIN even if other tests were skipped.
        # A positive partial ratio cannot justify assigning unknown tests 0.
        ratio = 0.0 if 0 in ratios else (None if None in ratios else min(ratios))
        item.update(ratio=ratio, points=item['total'] * ratio if ratio is not None else (
            0.0 if item['total'] == 0 else None),
            limited_by_dependencies=ratio is not None and (
                item['raw_ratio'] is None or ratio < item['raw_ratio']))
        resolved[key] = item
        return item

    for number in own:
        resolve(number)
    if any(row['points'] is None for row in resolved.values()):
        raise ValidationError(_('Skipped tests cannot be resolved from the stored dependency results.'))
    total = fsum(row['total'] for row in resolved.values())
    raw = fsum(row['points'] for row in resolved.values())
    full = has_full_score(raw, total)
    return {'status': 'valid', 'points': scaled_score(raw, total, maximum, partial), 'raw_points': raw,
            'total': total, 'full': full, 'batches': resolved}


def invalidate(contest_id, force=False):
    # Scores share a five-second refresh window. Invalidating on every submission
    # would make a popular contest continually cold. Policy changes invalidate
    # immediately, across all permission/freeze/language variants.
    if not force:
        return
    import uuid
    cache.set('conservative:version:%s' % contest_id, uuid.uuid4().hex, None)


def start_build(contest_id):
    from judge.models import Contest, ContestSubmission
    from judge.tasks.conservative import build_conservative
    with transaction.atomic():
        contest = Contest.objects.select_for_update().get(pk=contest_id)
        configs = {}
        for cp in contest.contest_problems.select_related('problem').order_by('pk'):
            configs[str(cp.pk)] = snapshot_problem(cp.problem)
        if not configs:
            raise ValidationError(_('Add contest problems before enabling Conservative Ranking.'))
        revision = contest.conservative_revision + 1
        submissions = ContestSubmission.objects.filter(participation__contest_id=contest_id)
        cutoff = submissions.aggregate(n=Max('pk'))['n'] or 0
        progress = {'cursor': 0, 'cutoff': cutoff, 'done': 0, 'total': submissions.count(), 'errors': 0}
        Contest.objects.filter(pk=contest_id).update(conservative_state='BUILDING', conservative_revision=revision,
            conservative_config=configs, conservative_progress=progress, conservative_error='')
        # Generation checks exclude old rows without a long, eager table update.
        def enqueue():
            try:
                build_conservative.delay(contest_id, revision)
            except Exception:
                logger.exception('Could not enqueue Conservative build for contest %s', contest_id)
                Contest.objects.filter(pk=contest_id, conservative_revision=revision,
                    conservative_state='BUILDING').update(conservative_state='FAILED',
                        conservative_error='Could not queue the build. Check Celery and rebuild Conservative Ranking.')
        transaction.on_commit(enqueue)
        transaction.on_commit(lambda: invalidate(contest_id))
        return revision


def stop(contest_id):
    from django.db.models import F
    from judge.models import Contest
    with transaction.atomic():
        Contest.objects.filter(pk=contest_id).update(conservative_state='OFF',
            conservative_revision=F('conservative_revision') + 1, conservative_error='')
        transaction.on_commit(lambda: invalidate(contest_id))


def refresh_one(cs_id, revision, verified_layouts=None):
    from judge.models import Contest, ContestSubmission, Submission
    reference = ContestSubmission.objects.filter(pk=cs_id).values('submission_id', 'participation__contest_id').first()
    if reference is None:
        return
    contest_id = reference['participation__contest_id']
    with transaction.atomic():
        sub = Submission.objects.select_for_update().get(pk=reference['submission_id'])
        # Lock only these result rows. A FOR UPDATE join to Problem would invert
        # the judge admission lock order (Problem -> Submission) during rejudge.
        cs = ContestSubmission.objects.select_for_update().filter(pk=cs_id).first()
        contest = Contest.objects.filter(pk=contest_id, conservative_revision=revision,
                                         conservative_state__in=ACTIVE).first()
        if not cs or not contest:
            return
        config = contest.conservative_config.get(str(cs.problem_id))
        try:
            if is_aborted(sub):
                # Abort is excluded, not a zero score or an incomplete result.
                # It must not require any dataset/snapshot/testcase validation.
                result = ignored_abort()
            elif config is None or config['problem_id'] != sub.problem_id:
                raise ValidationError(_('Rebuild Conservative Ranking after changing contest problems.'))
            else:
                if config.get('scoring_version') != SCORING_VERSION:
                    raise ValidationError(_('The scoring rules changed. Rebuild Conservative Ranking.'))
                current_layout = (verified_layouts or {}).get(cs.problem_id)
                if current_layout is None:
                    current_layout, _ = problem_layout(cs.problem.problem)
                if not same_subtasks(config['layout'], current_layout):
                    raise ValidationError(_('Subtask counts or corresponding maximum points changed. Rebuild Conservative Ranking.'))
                result = calculate(sub, config, cs.problem.points, cs.problem.partial and cs.problem.problem.partial)
        except ValidationError as exc:
            result = {'status': 'error', 'error': ' '.join(exc.messages)}
        result.update(generation=revision, scoring_version=SCORING_VERSION,
                      dispatch=(sub.batch_scoring or {}).get('dispatch_id'),
                      calculated_at=timezone.now().isoformat())
        # The SQL generation predicate also serializes publication with a
        # concurrent disable/rebuild, including one that happens after our read.
        ContestSubmission.objects.filter(pk=cs_id, participation__contest_id=contest_id,
            participation__contest__conservative_revision=revision,
            participation__contest__conservative_state__in=ACTIVE).update(
                conservative_points=result.get('points'), conservative_data=result)
        transaction.on_commit(lambda: invalidate(contest_id))
    return result['status']


def schedule_submission(submission_id):
    from judge.models import Contest, ContestSubmission
    from judge.tasks.conservative import refresh_conservative_submission
    row = ContestSubmission.objects.filter(submission_id=submission_id,
        participation__contest__conservative_state__in=ACTIVE).values(
            'pk', 'participation__contest_id', 'participation__contest__conservative_revision',
            'submission__status', 'submission__result').first()
    if row:
        revision = row['participation__contest__conservative_revision']
        contest_id = row['participation__contest_id']
        if row['submission__status'] == 'AB' or row['submission__result'] == 'AB':
            # Clearing an aborted run does not depend on Celery availability.
            refresh_one(row['pk'], revision)
            return
        # Persist the dirty state before enqueueing. An unavailable worker must
        # never leave a previous judged run looking like a current valid score.
        ContestSubmission.objects.filter(pk=row['pk'], participation__contest__conservative_revision=revision,
            participation__contest__conservative_state__in=ACTIVE).update(conservative_points=None,
                conservative_data={'generation': revision, 'status': 'pending'})

        def enqueue():
            current = ContestSubmission.objects.filter(pk=row['pk'],
                participation__contest__conservative_state__in=ACTIVE).values(
                    'participation__contest__conservative_revision').first()
            if current is None:
                return
            current_revision = current['participation__contest__conservative_revision']
            if current_revision != revision:
                # A build may have started before this submission transaction
                # committed, so its cutoff may not contain this row yet.
                ContestSubmission.objects.filter(pk=row['pk'],
                    participation__contest__conservative_revision=current_revision).update(
                        conservative_points=None, conservative_data={'generation': current_revision, 'status': 'pending'})
            try:
                refresh_conservative_submission.delay(row['pk'], current_revision)
            except Exception:
                # Optional ranking delivery must not prevent the primary judge
                # dispatch callback (registered later) from running on commit.
                logger.exception('Could not enqueue Conservative refresh for submission %s', submission_id)
                message = 'Could not queue a score update. Check Celery and rebuild Conservative Ranking.'
                ContestSubmission.objects.filter(pk=row['pk'], conservative_data__generation=current_revision,
                    conservative_data__status='pending').update(conservative_data={
                        'generation': current_revision, 'status': 'error', 'error': message})
                Contest.objects.filter(pk=contest_id, conservative_revision=current_revision,
                    conservative_state__in=ACTIVE).update(conservative_error='#%s: %s' % (submission_id, message))
        transaction.on_commit(enqueue)
        transaction.on_commit(lambda: invalidate(row['participation__contest_id']))


def visible_points(data, contest_problem, hidden=()):
    if not data.get('total'):
        return 0.0
    raw = fsum(row['points'] for number, row in data['batches'].items() if int(number) not in hidden)
    partial = contest_problem.partial and contest_problem.problem.partial
    return scaled_score(raw, data['total'], contest_problem.points, partial or data['full']) if (
        partial or data['full']) else 0.0
