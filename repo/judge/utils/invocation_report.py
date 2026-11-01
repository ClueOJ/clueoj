"""Read stored judge results without changing submission verdicts or scores."""
from collections import defaultdict
from itertools import groupby
from math import fsum, isfinite

from django.db.models import Prefetch

from judge.models import SubmissionTestCase
from judge.utils.batch_scoring import has_full_score


ERROR_ORDER = ('WA', 'MLE', 'TLE', 'IR', 'RTE', 'OLE')
REJECTED = {*ERROR_ORDER, 'CE'}


def accepted_verdict(points, total):
    if points is None or total is None or not isfinite(points) or not isfinite(total):
        return 'IE'
    if (points == total == 0) or has_full_score(points, total):
        return 'AC'
    return 'PAC' if 0 <= points < total else 'IE'


def expectation_match(expected, actual):
    if not expected or expected == 'SKIP':
        return 'ignored'
    if actual in ('QU', 'P', 'G', 'PENDING'):
        return 'pending'
    matches = ({'AC', 'PAC', 'TLE'} if expected == 'AC/TL' else
               REJECTED if expected == 'RJ' else {expected})
    return 'match' if actual in matches else 'mismatch'


def classify(cases, points, total, *, complete=True, mode='sum'):
    if not complete or not cases:
        return 'PENDING'
    codes = {case.status for case in cases}
    errors = codes.intersection(ERROR_ORDER)
    if errors:
        # SC after an established MIN failure is intentional. An incomplete SUM
        # cannot be claimed as a fully evaluated group.
        if 'SC' in codes and mode != 'min':
            return 'SC'
        return max(errors, key=ERROR_ORDER.index)
    if codes != {'AC'}:
        return 'SC' if 'SC' in codes else sorted(codes)[0]
    return accepted_verdict(points, total)


def cell(actual, cases=(), points=None, total=None, expected='', max_time=False):
    times = [case.time for case in cases if case.status != 'SC' and case.time is not None]
    return {'actual': actual, 'expected': expected, 'match': expectation_match(expected, actual),
            'time_ms': round(max(times) * 1000, 3) if times else None,
            'points': points, 'total': total, 'max_time': max_time}


def layout_signature(layout):
    if layout is None:
        return None
    combined = []
    for row in layout:
        value = (row['batch'], row['count'], row['mode'], row.get('total'), row.get('case_id'))
        if combined and not row['batch'] and not combined[-1][0]:
            previous = combined.pop()
            value = (0, previous[1] + row['count'], 'sum', (previous[3] or 0) + (row.get('total') or 0), None)
        combined.append(value)
    return combined


def observed_layout(submission, cases):
    config = submission.batch_scoring or {}
    if config.get('full') is not None:
        return config['full']
    results = {row.batch: row for row in submission.batch_results.all()}
    layout = []
    for batch, group in groupby(cases, key=lambda case: case.batch or 0):
        group = list(group)
        result = results.get(batch) if batch else None
        layout.append({'batch': batch, 'count': len(group), 'mode': result.scoring_type if result else 'sum',
                       'total': result.total if result else fsum(case.total or 0 for case in group)})
    return layout


def build_report(invocation):
    items = list(invocation.items.select_related('submission__language').prefetch_related(
        Prefetch('submission__test_cases', queryset=SubmissionTestCase.objects.only(
            'submission_id', 'case', 'batch', 'status', 'time', 'points', 'total')),
        'submission__batch_results'))
    layout = invocation.layout
    if layout is None:
        for item in items:
            cases = sorted(item.submission.test_cases.all(), key=lambda case: case.case)
            if cases:
                layout = observed_layout(item.submission, cases)
                break
    layout = layout or []
    rows, group_rows = [], []
    number = 0
    for group in layout:
        start = number + 1
        number += group['count']
        batch = group['batch']
        if batch:
            group_rows.append({'batch': batch, 'mode': group['mode'], 'cells': []})
        if batch and group['mode'] == 'min':
            rows.append({'batch': batch, 'mode': 'min', 'case': None, 'cells': []})
        else:
            for case in range(start, number + 1):
                rows.append({'batch': batch, 'mode': 'sum', 'case': case, 'cells': []})
    overall, columns = [], []
    for item in items:
        sub = item.submission
        cases = sorted(sub.test_cases.all(), key=lambda case: case.case)
        by_number = {case.case: case for case in cases}
        grouped = defaultdict(list)
        for case in cases:
            grouped[case.batch or 0].append(case)
        scores = {result.batch: result for result in sub.batch_results.all()}
        terminal = sub.status not in ('QU', 'P', 'G')
        special = sub.status if sub.status in ('CE', 'IE', 'AB') else None
        current_layout = observed_layout(sub, cases)
        changed = bool(cases and invocation.layout is not None and
                       layout_signature(current_layout) != layout_signature(invocation.layout))
        overall_actual = special or (sub.result if terminal else sub.status)
        if overall_actual == 'AC':
            overall_actual = accepted_verdict(sub.case_points, sub.case_total)
        overall.append(cell(overall_actual or 'PENDING', cases, sub.case_points if terminal and not special else None,
                            sub.case_total if terminal and not special else None, item.expected_overall, max_time=True))
        group_cells = {}
        for group in layout:
            batch = group['batch']
            if not batch:
                continue
            children = grouped[batch]
            score = scores.get(batch)
            points = score.points if score else None
            total = score.total if score else None
            complete = terminal and len(children) == group['count'] and score is not None
            actual = special or classify(children, points, total, complete=complete, mode=group['mode'])
            if changed:
                actual = 'CHANGED'
            group_cells[batch] = cell(actual, children, points, total,
                                      item.expected_subtasks.get(str(batch), ''), max_time=True)
        for row in rows:
            if row['mode'] == 'min':
                row['cells'].append(group_cells[row['batch']])
            else:
                case = by_number.get(row['case']) if not changed else None
                actual = ('CHANGED' if changed else
                          case.status if case else special or ('SC' if terminal else sub.status))
                if actual == 'AC':
                    actual = accepted_verdict(case.points, case.total)
                row['cells'].append(cell(actual, [case] if case else [],
                                         case.points if case else None, case.total if case else None))
        for row in group_rows:
            row['cells'].append(group_cells[row['batch']])
        comparisons = [overall[-1], *group_cells.values()]
        columns.append({'id': sub.pk, 'filename': item.filename, 'language': sub.language.display_name,
                        'status': sub.short_status, 'error': sub.error or '', 'changed': changed,
                        'dispatch_error': item.dispatch_error, 'rejudged_at': sub.rejudged_date,
                        'mismatch': any(value['match'] == 'mismatch' for value in comparisons)})
    return {'columns': columns, 'overall': overall, 'rows': rows, 'groups': group_rows,
            'sum_groups': [row for row in group_rows if row['mode'] == 'sum'],
            'finished': all(item.submission.status not in ('QU', 'P', 'G') for item in items),
            'completed': sum(item.submission.status not in ('QU', 'P', 'G') for item in items), 'count': len(items)}
