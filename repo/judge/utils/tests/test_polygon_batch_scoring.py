import io
import tempfile
import zipfile
from contextlib import redirect_stdout
from itertools import combinations
from math import fsum
from types import SimpleNamespace

from django.core.management.base import CommandError
from django.test import TestCase
from lxml import etree as ET

from judge.models import Language, ProblemData, Submission, SubmissionTestCase
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.utils.batch_scoring import finalize_batch_results, score_cases, scoring_snapshot
from judge.utils.polygon_import import _sync_problem_testcases, parse_tests
from judge.utils.problem_data import ProblemDataCompiler


class PolygonBatchScoringTests(CommonDataMixin, TestCase):
    def parse(self, groups, tests, **options):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = ET.fromstring('''<problem><judging><testset name="tests">
            <time-limit>1000</time-limit><memory-limit>67108864</memory-limit>
            <input-path-pattern>tests/%02d</input-path-pattern>
            <answer-path-pattern>tests/%02d.a</answer-path-pattern>
            <groups/><tests/>
        </testset></judging></problem>''')
        testset = root.find('.//testset')
        if groups is None:
            testset.remove(testset.find('groups'))
        for name, policy, points, dependencies in groups or []:
            attributes = {'name': name, 'points-policy': policy}
            if points is not None:
                attributes['points'] = str(points)
            group = ET.SubElement(testset.find('groups'), 'group', **attributes)
            if dependencies:
                node = ET.SubElement(group, 'dependencies')
                for dependency in dependencies:
                    ET.SubElement(node, 'dependency', group=dependency)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for number, (group, points) in enumerate(tests, 1):
                attributes = {}
                if group is not None:
                    attributes['group'] = group
                if points is not None:
                    attributes['points'] = str(points)
                ET.SubElement(testset.find('tests'), 'test', **attributes)
                archive.writestr('tests/%02d' % number, '%d\n' % number)
                archive.writestr('tests/%02d.a' % number, '%d\n' % number)
        meta = {'tmp_dir': directory, 'interactive': False,
                'ignore_zero_point_batches': True, 'ignore_zero_point_cases': True}
        meta.update(options)
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive, redirect_stdout(io.StringIO()):
            parse_tests(meta, root, archive)
        return meta

    def sync(self, meta):
        problem = create_problem(code='polygon_batch_modes', partial=True)
        _sync_problem_testcases(problem, meta)
        return problem

    def compile(self, problem, meta):
        files = [case[key] for case in meta['cases_data'] for key in ('input_file', 'output_file')]
        return ProblemDataCompiler(problem, ProblemData(problem=problem), problem.cases.order_by('order'), files).make_init()

    def test_mixed_groups_become_explicit_sum_and_min_batches(self):
        meta = self.parse([('A', 'each-test', 99, []), ('B', 'complete-group', 20, [])],
                          [('A', 5), ('A', 15), ('B', 100), ('B', 100), (None, 3)])
        problem = self.sync(meta)
        starts = list(problem.cases.filter(type='S').order_by('order'))
        self.assertEqual([(case.batch_type, case.points) for case in starts], [('sum', 20), ('min', 20)])
        self.assertEqual(list(problem.cases.order_by('order').values_list('type', flat=True)), list('SCCESCCEC'))
        self.assertEqual([item['mode'] for item in scoring_snapshot(problem)['full']], ['sum', 'min', 'sum'])
        self.assertEqual(meta['normal_cases'], [4])

    def test_each_test_group_without_group_points_keeps_its_own_batch(self):
        meta = self.parse([('A', 'each-test', None, [])], [('A', 5), ('A', 15)])
        self.assertEqual(meta['normal_cases'], [])
        self.assertEqual(meta['batches']['A']['points'], 20)
        problem = self.sync(meta)
        self.assertEqual(problem.cases.get(type='S').batch_type, 'sum')

    def test_each_test_missing_weight_defaults_to_one_but_explicit_zero_is_filtered(self):
        meta = self.parse([('A', 'each-test', None, [])], [('A', None), ('A', 2), ('A', 0)])
        self.assertEqual(meta['batches']['A']['points'], 3)
        self.assertEqual(meta['batches']['A']['cases'], [0, 1])
        problem = self.sync(meta)
        self.assertEqual([case['points'] for case in self.compile(problem, meta)['test_cases'][0]['batched']], [1, 2])

    def test_sum_batch_preserves_unequal_test_weights_through_compilation(self):
        meta = self.parse([('A', 'each-test', None, [])], [('A', 5), ('A', 15)])
        problem = self.sync(meta)
        init = self.compile(problem, meta)
        batch = init['test_cases'][0]
        self.assertEqual(batch['points'], 20)
        self.assertEqual([case['points'] for case in batch['batched']], [5, 15])
        self.assertEqual(list(problem.cases.filter(type='C').order_by('order').values_list('points', flat=True)), [5, 15])
        # Reproduce the existing judge's weighting when only the first test passes.
        weights = [case['points'] for case in batch['batched']]
        cases = [SimpleNamespace(total=batch['points'] * weight / sum(weights),
                                 points=batch['points'] * weight / sum(weights) if i == 0 else 0)
                 for i, weight in enumerate(weights)]
        self.assertEqual(score_cases(cases, 'sum'), (5, 20))
        self.assertEqual(self.compile(problem, meta), init)

    def test_complete_group_keeps_equal_weights_and_group_maximum(self):
        meta = self.parse([('A', 'complete-group', 20, [])], [('A', 5), ('A', 15)])
        problem = self.sync(meta)
        batch = self.compile(problem, meta)['test_cases'][0]
        self.assertEqual(problem.cases.get(type='S').batch_type, 'min')
        self.assertEqual(batch['points'], 20)
        self.assertEqual([case['points'] for case in batch['batched']], [1, 1])

    def test_fractional_weights_are_normalized_with_other_group_and_test_points(self):
        meta = self.parse([('A', 'each-test', None, []), ('B', 'complete-group', 3.5, [])],
                          [('A', 0.25), ('A', 0.75), ('B', None), (None, 1.25)])
        problem = self.sync(meta)
        init = self.compile(problem, meta)
        self.assertEqual([case['points'] for case in init['test_cases']], [4, 14, 5])
        self.assertEqual([case['points'] for case in init['test_cases'][0]['batched']], [1, 3])

    def test_complete_group_dependencies_keep_the_existing_flattening(self):
        meta = self.parse([('A', 'each-test', None, []), ('B', 'complete-group', 20, ['A'])],
                          [('A', 5), ('A', 15), ('B', None)])
        self.assertEqual(meta['batches']['A']['cases'], [0, 1])
        self.assertEqual(meta['batches']['B']['cases'], [0, 1, 2])
        problem = self.sync(meta)
        self.assertEqual(list(problem.cases.filter(type='S').order_by('order').values_list('batch_type', flat=True)),
                         ['sum', 'min'])

    def test_each_test_dependencies_are_still_rejected(self):
        with self.assertRaisesMessage(CommandError, 'dependencies are only supported'):
            self.parse([('A', 'complete-group', 20, []), ('B', 'each-test', None, ['A'])],
                       [('A', None), ('B', 10)])

    def test_zero_point_test_filter_also_applies_inside_each_test_groups(self):
        for ignore, expected in ((True, [1]), (False, [0, 1])):
            with self.subTest(ignore=ignore):
                meta = self.parse([('A', 'each-test', None, [])], [('A', 0), ('A', 10)],
                                  ignore_zero_point_cases=ignore)
                self.assertEqual(meta['batches']['A']['cases'], expected)
                self.assertEqual(meta['batches']['A']['points'], 10)

    def test_ungrouped_tests_remain_standalone(self):
        meta = self.parse([], [(None, 5), ('missing-group', 15)])
        problem = self.sync(meta)
        self.assertFalse(problem.cases.filter(type='S').exists())
        self.assertEqual([case['points'] for case in self.compile(problem, meta)['test_cases']], [5, 15])

    def test_reimport_sets_modes_from_the_new_package(self):
        first = self.parse([('A', 'complete-group', 20, [])], [('A', None)])
        problem = self.sync(first)
        second = self.parse([('A', 'each-test', None, []), ('B', 'complete-group', 30, [])],
                            [('A', 5), ('A', 15), ('B', None)])
        _sync_problem_testcases(problem, second)
        self.assertEqual(list(problem.cases.filter(type='S').order_by('order').values_list('batch_type', 'points')),
                         [('sum', 20), ('min', 30)])

    def test_multiple_groups_of_each_policy_keep_independent_maxima_and_weights(self):
        meta = self.parse([
            ('S1', 'each-test', None, []), ('M1', 'complete-group', 20, []),
            ('S2', 'each-test', None, []), ('M2', 'complete-group', 30, []),
        ], [('S2', 2), ('M2', None), ('S1', 1), ('M1', None),
            ('S2', 5), ('M2', None), ('S1', 3), ('M1', None), (None, 9)])
        problem = self.sync(meta)
        init = self.compile(problem, meta)
        self.assertEqual([node['points'] for node in init['test_cases']], [4, 20, 7, 30, 9])
        self.assertEqual([[child['points'] for child in node['batched']]
                          for node in init['test_cases'] if 'batched' in node], [[1, 3], [1, 1], [2, 5], [1, 1]])
        self.assertEqual([batch['mode'] for batch in scoring_snapshot(problem)['full']],
                         ['sum', 'min', 'sum', 'min', 'sum'])

    def test_reimport_between_ungrouped_and_mixed_packages_removes_old_markers(self):
        problem = create_problem(code='polygon_reimport', partial=True)
        scenarios = (
            (None, [(None, None), (None, None)], []),
            ([('S', 'each-test', None, [])], [('S', 1), ('S', 3)], [('sum', 4)]),
            ([('M', 'complete-group', 20, [])], [('M', None)], [('min', 20)]),
            ([('S', 'each-test', None, []), ('M', 'complete-group', 30, [])],
             [('M', None), (None, 2), ('S', 5)], [('sum', 5), ('min', 30)]),
            ([], [(None, 2), (None, 4)], []),
        )
        for groups, tests, expected_batches in scenarios:
            with self.subTest(groups=groups):
                meta = self.parse(groups, tests, ignore_zero_point_cases=False)
                _sync_problem_testcases(problem, meta)
                self.assertEqual(list(problem.cases.filter(type='S').order_by('order')
                                      .values_list('batch_type', 'points')), expected_batches)
                self.assertEqual(problem.cases.filter(type='E').count(), len(expected_batches))
                init = self.compile(problem, meta)
                self.assertEqual(sum(len(node.get('batched', [node])) for node in init['test_cases']), len(tests))

    def test_all_group_combinations_match_compiled_layout_and_final_scores(self):
        self.checked_combinations = 0
        for count in range(1, 4):
            for categories in combinations(('sum', 'min', 'standalone'), count):
                groups = [
                    ('sum', 'each-test', None, []) if kind == 'sum' else ('min', 'complete-group', 20, [])
                    for kind in categories if kind != 'standalone'
                ]
                orders = [groups, list(reversed(groups))] if len(groups) == 2 else [groups]
                for ordered_groups in orders:
                    with self.subTest(categories=categories, group_order=[group[0] for group in ordered_groups]):
                        # Interleave the XML test order, independently of group declaration order.
                        tests = []
                        for round_number in range(2):
                            for kind in reversed(categories):
                                points = {'sum': (1, 3), 'min': (None, None), 'standalone': (5, 7)}[kind]
                                tests.append((None if kind == 'standalone' else kind, points[round_number]))
                        meta = self.parse(ordered_groups if ordered_groups else None, tests)
                        problem = self.sync(meta)
                        init = self.compile(problem, meta)
                        expected_modes = ['sum' if group[1] == 'each-test' else 'min' for group in ordered_groups]
                        self.assertEqual(list(problem.cases.filter(type='S').order_by('order')
                                              .values_list('batch_type', flat=True)), expected_modes)
                        self.assertEqual(sum(1 for node in init['test_cases'] if 'batched' in node), len(ordered_groups))
                        expected_files = []
                        for group in ordered_groups:
                            expected_files.extend('%02d.inp' % (i+1) for i, (name, _) in enumerate(tests) if name == group[0])
                        expected_files.extend('%02d.inp' % (i+1) for i, (name, _) in enumerate(tests) if name is None)
                        compiled_files = [case['in'] for node in init['test_cases']
                                          for case in node.get('batched', [node])]
                        self.assertEqual(compiled_files, expected_files)
                        patterns = ([1] * len(tests), [i % 2 for i in range(len(tests))],
                                    [0.25 if i % 2 else 0.75 for i in range(len(tests))])
                        for ratios in patterns:
                            expected = []
                            for group in ordered_groups:
                                indexes = [i for i, (name, _) in enumerate(tests) if name == group[0]]
                                expected.append(fsum(tests[i][1] * ratios[i] for i in indexes)
                                                if group[1] == 'each-test' else 20 * min(ratios[i] for i in indexes))
                            expected.append(fsum(points * ratios[i] for i, (name, points) in enumerate(tests) if name is None))
                            sub = Submission.objects.create(problem=problem, user=self.users['normal'].profile,
                                language=Language.get_python3(), status='D', batch_scoring=scoring_snapshot(problem))
                            case_number = batch_number = 0
                            for node in init['test_cases']:
                                grouped = 'batched' in node
                                if grouped:
                                    batch_number += 1
                                children = node.get('batched', [node])
                                weight_total = fsum(child['points'] for child in children)
                                for child in children:
                                    case_number += 1
                                    index = int(child['in'].split('.')[0]) - 1
                                    maximum = node['points'] * child['points'] / weight_total if grouped else node['points']
                                    SubmissionTestCase.objects.create(submission=sub, case=case_number,
                                        batch=batch_number if grouped else None, total=maximum,
                                        points=maximum * ratios[index], status='AC' if ratios[index] else 'WA')
                            points, total = finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
                            self.assertAlmostEqual(points, fsum(expected))
                            self.assertEqual(total, sum({'sum': 4, 'min': 20, 'standalone': 12}[kind] for kind in categories))
                            self.assertEqual(sub.test_cases.count(), len(tests))
                        self.checked_combinations += 1
        self.assertEqual(self.checked_combinations, 9)

    def test_unscored_ungrouped_package_follows_zero_point_filter(self):
        for groups in (None, []):
            with self.subTest(groups=groups):
                with self.assertRaisesMessage(CommandError, 'No testcases remain'):
                    self.parse(groups, [(None, None), (None, None)])
                meta = self.parse(groups, [(None, None), (None, None)], ignore_zero_point_cases=False)
                self.assertEqual(meta['normal_cases'], [0, 1])
                self.assertFalse(meta['partial'])
                problem = self.sync(meta)
                self.assertEqual([case['points'] for case in self.compile(problem, meta)['test_cases']], [0, 0])

    def test_retained_each_test_group_with_zero_weight_total_is_rejected(self):
        with self.assertRaisesMessage(CommandError, 'at least one positive test weight'):
            self.parse([('S', 'each-test', None, [])], [('S', 0), ('S', 0)],
                       ignore_zero_point_batches=False, ignore_zero_point_cases=False)

    def test_zero_weight_group_can_be_filtered_without_removing_other_tests(self):
        meta = self.parse([('S', 'each-test', None, [])], [('S', 0), (None, 2)],
                          ignore_zero_point_batches=True, ignore_zero_point_cases=False)
        self.assertNotIn('S', meta['batches'])
        self.assertEqual(meta['normal_cases'], [1])

    def test_missing_ungrouped_points_stay_zero_in_explicitly_scored_packages(self):
        scenarios = ((None, [(None, None), (None, 2)]),
                     ([('S', 'each-test', None, [])], [(None, None), ('S', 2)]),
                     ([('M', 'complete-group', 20, [])], [(None, None), ('M', None)]))
        for groups, tests in scenarios:
            for ignore_cases in (False, True):
                with self.subTest(groups=groups, ignore_cases=ignore_cases):
                    meta = self.parse(groups, tests, ignore_zero_point_cases=ignore_cases)
                    self.assertTrue(meta['partial'])
                    self.assertEqual(meta['cases_data'][0]['points'], 0)
                    self.assertEqual(0 in meta['normal_cases'], not ignore_cases)

    def test_filtering_every_test_reports_an_import_error(self):
        scenarios = ((None, [(None, 0)]),
                     ([('A', 'each-test', None, [])], [('A', 0)]),
                     ([('A', 'complete-group', 0, [])], [('A', None)]))
        for groups, tests in scenarios:
            with self.subTest(groups=groups):
                with self.assertRaisesMessage(CommandError, 'No testcases remain'):
                    self.parse(groups, tests)

    def test_zero_point_filters_cover_all_combinations(self):
        for ignore_batches in (False, True):
            for ignore_cases in (False, True):
                with self.subTest(ignore_batches=ignore_batches, ignore_cases=ignore_cases):
                    meta = self.parse([('S', 'each-test', None, []), ('M', 'complete-group', 0, [])],
                        [('S', 0), ('S', 2), ('M', None), (None, 0), (None, 3)],
                        ignore_zero_point_batches=ignore_batches, ignore_zero_point_cases=ignore_cases)
                    self.assertEqual(meta['batches']['S']['cases'], [1] if ignore_cases else [0, 1])
                    self.assertEqual('M' in meta['batches'], not ignore_batches)
                    self.assertEqual(meta['normal_cases'], [4] if ignore_cases else [3, 4])
                    problem = self.sync(meta)
                    self.assertTrue(self.compile(problem, meta)['test_cases'])
