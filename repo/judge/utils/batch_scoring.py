"""Web-side batch scoring and versioned policies for optional judge early exit."""
from collections import defaultdict
from copy import deepcopy
from itertools import groupby
from math import fsum, isclose
from sys import float_info

from django.core.exceptions import ObjectDoesNotExist


# Allow only floating-point representation noise, not rounded checker scores.
FULL_SCORE_REL_TOL = 8 * float_info.epsilon


def has_full_score(points, total):
    return total > 0 and isclose(points, total, rel_tol=FULL_SCORE_REL_TOL, abs_tol=0)


def scaled_score(points, total, maximum, partial=True):
    if total <= 0 or (not partial and not has_full_score(points, total)):
        return 0
    return round(points / total * maximum, 3)


def legacy_batch_type(problem):
    try:
        return problem.data_files.legacy_batch_type or 'min'
    except ObjectDoesNotExist:
        return 'min'


def score_cases(cases, scoring_type):
    total = fsum(case.total or 0 for case in cases)
    if scoring_type == 'sum':
        return fsum(case.points or 0 for case in cases), total
    if scoring_type != 'min':
        raise ValueError('Unknown batch scoring type: %s' % scoring_type)
    ratios = [(case.points or 0) / case.total for case in cases if case.total and case.total > 0]
    return total * min(ratios, default=0), total


def scoring_snapshot(problem):
    """Capture both possible datasets before dispatch; grading-begin chooses one.

    A mirror executes its root's test structure. Old mirror-specific policies
    and manually managed files retain their migration-time default.
    """
    source = problem.mirror_root if problem.mirror_root_id else problem
    try:
        data = problem.data_files
    except ObjectDoesNotExist:
        data = None
    default = data.legacy_batch_type if data and data.legacy_batch_type else 'sum'
    overrides = (data.batch_scoring_overrides if data else None) or {}
    snapshot = {'version': 1, 'default': default, 'full': None, 'pretests': None, 'overrides': overrides,
                'cumulative_revision': problem.subtask_score_revision}
    if source.is_manually_managed:
        if not problem.is_mirror and problem.subtask_tests_locked:
            snapshot = deepcopy(problem.subtask_score_config)
            snapshot['cumulative_revision'] = problem.subtask_score_revision
        return snapshot
    rows = list(source.cases.order_by('order', 'id'))
    if not rows:
        return snapshot
    if not problem.is_mirror:
        overrides = {}
        snapshot['overrides'] = overrides
    groups = []
    active = None
    for row in rows:
        if row.type == 'S':
            mode = row.batch_type
            if problem.is_mirror and data and data.legacy_batch_type:
                mode = default
            active = {'batch': True, 'count': 0, 'mode': mode, 'total': row.points, 'case_id': row.pk,
                      'pretest': row.is_pretest}
            groups.append(active)
        elif row.type == 'E':
            active = None
        elif active is not None:
            active['count'] += 1
        else:
            groups.append({'batch': False, 'count': 1, 'mode': 'sum', 'total': row.points,
                           'pretest': row.is_pretest})
    for key, pretest in (('full', False), ('pretests', True)):
        layout = []
        number = 0
        for group in groups:
            if group['pretest'] != pretest:
                continue
            item = dict(group)
            if item['batch']:
                number += 1
                item['batch'] = number
                item['mode'] = overrides.get('%s:%s' % (key, number), item['mode'])
            else:
                item['batch'] = 0
            if layout and not item['batch'] and not layout[-1]['batch']:
                layout[-1]['count'] += item['count']
                layout[-1]['total'] = (layout[-1]['total'] or 0) + (item['total'] or 0)
            else:
                layout.append(item)
        snapshot[key] = layout
    return snapshot


def editable_batch_policies(problem):
    """Expose policies even when archives are evicted or a mirror shares tests.

    For manually managed test structures, discover batch numbers from the last
    completed run. No archive reads, restores, or writes are needed.
    """
    snapshot = scoring_snapshot(problem)
    rows = []
    for key in ('full', 'pretests'):
        layout = snapshot[key]
        if layout is None:
            previous = problem.submission_set.filter(status='D', is_pretested=key == 'pretests').order_by('-id').first()
            numbers = (sorted(set(previous.test_cases.exclude(batch=None).values_list('batch', flat=True)))
                       if previous else [])
            layout = [{'batch': number, 'mode': snapshot['default']} for number in numbers]
        for item in layout:
            if not item['batch']:
                continue
            name = '%s:%s' % (key, item['batch'])
            rows.append({'key': name, 'batch': item['batch'], 'dataset': key,
                         'mode': snapshot['overrides'].get(name, item['mode']),
                         'case_id': item.get('case_id') if not problem.is_mirror and not problem.is_manually_managed else None})
    return rows


def save_batch_policies(problem, values):
    from django.core.exceptions import ValidationError
    from django.db import transaction
    from judge.models import ProblemData, ProblemTestCase

    from judge.utils.subtask_scoring import test_data_edit
    with test_data_edit([problem.pk]), transaction.atomic():
        rows = editable_batch_policies(problem)
        expected = {'batch-mode-' + row['key'] for row in rows}
        supplied = {name for name in values if name.startswith('batch-mode-')}
        if not rows or supplied != expected or any(values.get(name) not in ('min', 'sum') for name in expected):
            raise ValidationError('Invalid batch scoring configuration. Reload the test editor and try again.')
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        # Serialize concurrent DB-only edits without touching dataset files.
        data = ProblemData.objects.select_for_update().get(pk=data.pk)
        overrides = dict(data.batch_scoring_overrides or {})
        for row in rows:
            mode = values['batch-mode-' + row['key']]
            if row['case_id']:
                ProblemTestCase.objects.filter(pk=row['case_id'], dataset=problem, type='S').update(batch_type=mode)
            else:
                overrides[row['key']] = mode
        data.batch_scoring_overrides = overrides
        data.save(update_fields=['batch_scoring_overrides'])


def scoring_dataset(submission):
    config = submission.batch_scoring or {}
    # Existing judges fall back to full tests when a pretest run is requested
    # but no pretests are defined; some still report pretested=True.
    return 'pretests' if submission.is_pretested and config.get('pretests') != [] else 'full'


def batch_modes(submission):
    config = submission.batch_scoring or {}
    key = scoring_dataset(submission)
    modes = {item['batch']: item['mode'] for item in config.get(key) or [] if item['batch']}
    for name, mode in config.get('overrides', {}).items():
        dataset, number = name.split(':')
        if dataset == key:
            modes[int(number)] = mode
    return modes, config['default'] if 'default' in config else legacy_batch_type(submission.problem)


def finalize_batch_results(submission, cases):
    from judge.models import SubmissionBatchResult

    config = submission.batch_scoring
    if config is None:
        raise ValueError('Missing batch scoring snapshot')
    layout = config.get(scoring_dataset(submission))
    if layout is not None:
        actual = [(batch or 0, list(group)) for batch, group in groupby(cases, key=lambda c: c.batch)]
        if len(actual) != len(layout) or any(
            batch != expected['batch'] or len(group) != expected['count'] or
            not isclose(fsum(case.total or 0 for case in group), expected['total'] or 0, rel_tol=1e-8, abs_tol=1e-8)
            for (batch, group), expected in zip(actual, layout)
        ):
            raise ValueError('Judge test structure differs from the saved scoring configuration; sync test data and rejudge.')
    grouped = defaultdict(list)
    for case in cases:
        grouped[case.batch or 0].append(case)
    modes, default = batch_modes(submission)
    results = []
    for batch, group in sorted(grouped.items()):
        mode = modes.get(batch, default) if batch else 'sum'
        points, total = score_cases(group, mode)
        results.append(SubmissionBatchResult(submission=submission, batch=batch, scoring_type=mode,
                                             points=points, total=total))
    SubmissionBatchResult.objects.filter(submission=submission).delete()
    SubmissionBatchResult.objects.bulk_create(results)
    submission.__dict__.pop('_batch_results_cache', None)
    return fsum(row.points for row in results), fsum(row.total for row in results)


def display_batch_score(submission, batch, cases):
    """Historical submissions keep their original display policy until rejudge."""
    if submission.batch_scoring is None:
        mode = legacy_batch_type(submission.problem)
        points = (fsum(case.points or 0 for case in cases) if mode == 'sum' else
                  min((case.points or 0 for case in cases), default=0) * len(cases))
        return points, fsum(case.total or 0 for case in cases), mode
    if submission.status == 'D':
        if not hasattr(submission, '_batch_results_cache'):
            submission._batch_results_cache = {row.batch: row for row in submission.batch_results.all()}
        result = submission._batch_results_cache.get(batch)
        if result is not None:
            return result.points, result.total, result.scoring_type
    modes, default = batch_modes(submission)
    mode = modes.get(batch, default)
    points, total = score_cases(cases, mode)
    return points, total, mode
