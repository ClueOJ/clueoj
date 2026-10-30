from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.models import (Contest, ContestPublicRankingLink, ContestSubmission, Language, Problem,
                          ProblemTestCase, Submission, SubmissionTestCase)
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_participation,
                                     create_contest_problem, create_problem, create_user)
from judge.tasks.conservative import build_conservative
from judge.utils.batch_scoring import scoring_snapshot
from judge.utils.conservative import (calculate, dependency_context, refresh_one, save_dependencies,
                                      schedule_submission, snapshot_problem, start_build, stop, validate_dependencies)


@override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
class ConservativeTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='conservative', partial=True, points=100, is_public=True)
        for batch, total in enumerate((20, 30, 50)):
            order = batch * 4
            ProblemTestCase.objects.create(dataset=cls.problem, order=order, type='S', points=total,
                                          batch_type='sum', is_pretest=False)
            for n in (1, 2):
                ProblemTestCase.objects.create(dataset=cls.problem, order=order+n, type='C',
                    input_file='%s.in' % (order+n), output_file='%s.out' % (order+n), is_pretest=False)
            ProblemTestCase.objects.create(dataset=cls.problem, order=order+3, type='E', is_pretest=False)
        cls.contest = create_contest(key='conservative', is_visible=True, authors=['superuser'])
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem, partial=True, points=100)
        cls.part = create_contest_participation(contest=cls.contest, user='normal')
        cls.language = Language.get_python3()

    def setUp(self):
        cache.clear()

    def submit(self, ratios=((1, 1), (1, 1), (1, 1)), legacy=False, status='D', statuses=None):
        sub = Submission.objects.create(problem=self.problem, user=self.users['normal'].profile,
            language=self.language, status=status, result='AC' if status == 'D' else status,
            batch_scoring=None if legacy else scoring_snapshot(self.problem))
        cs = ContestSubmission.objects.create(submission=sub, problem=self.cp, participation=self.part)
        for batch, (maximum, group) in enumerate(zip((20, 30, 50), ratios), 1):
            for n, ratio in enumerate(group, 1):
                case = (batch-1)*2+n
                SubmissionTestCase.objects.create(submission=sub, case=case, batch=batch, total=maximum/2,
                    points=ratio*maximum/2, status=(statuses or {}).get(case, 'AC' if ratio == 1 else 'WA'))
        sub.case_total = 100
        sub.case_points = sum(tc.points for tc in sub.test_cases.all())
        sub.points = sub.case_points
        sub.save()
        return sub, cs

    def dependencies(self, graph):
        values = QueryDict(mutable=True)
        values['conservative-layout'] = dependency_context(self.problem)['conservative_layout']
        for number, required in graph.items():
            values.setlist('conservative-dep-%s' % number, list(map(str, required)))
        save_dependencies(self.problem, values)
        self.problem.refresh_from_db()
        return values

    def build(self):
        with patch('judge.tasks.conservative.build_conservative.delay'), self.captureOnCommitCallbacks(execute=True):
            revision = start_build(self.contest.pk)
        build_conservative.run(self.contest.pk, revision)
        self.contest.refresh_from_db()
        return revision

    def test_defaults_off_and_empty(self):
        self.assertEqual(self.contest.conservative_state, 'OFF')
        self.assertTrue(self.contest.show_conservative_ranking)
        self.assertEqual(self.problem.conservative_config, {})

    def test_mirror_inherits_root_dependencies_and_rejects_separate_defaults(self):
        self.dependencies({'2': [1]})
        mirror = create_problem(code='conservative_mirror', mirror_of=self.problem, partial=True,
                                conservative_config={'dependencies': {'2': [3]}, 'revision': 999})
        root_config = snapshot_problem(self.problem)
        mirror_config = snapshot_problem(mirror)
        self.assertEqual(mirror_config['dependencies'], root_config['dependencies'])
        self.assertEqual(mirror_config['dependency_revision'], root_config['dependency_revision'])
        self.assertEqual(mirror_config['layout'], root_config['layout'])
        self.assertEqual(mirror_config['problem_id'], mirror.pk)
        self.assertEqual(dependency_context(mirror)['conservative_rows'], [])
        with self.assertRaises(ValidationError):
            save_dependencies(mirror, QueryDict())
        mirror.refresh_from_db()
        self.assertEqual(mirror.conservative_config['revision'], 999)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.conservative_config['dependencies']['2'], [1])

    def test_contest_pins_mirror_root_dependencies_until_rebuild(self):
        mirror = create_problem(code='conservative_mirror_pinned', mirror_of=self.problem, partial=True)
        cp = create_contest_problem(contest=self.contest, problem=mirror, points=100, partial=True)
        self.dependencies({'2': [1]})
        with patch('judge.tasks.conservative.build_conservative.delay'):
            start_build(self.contest.pk)
        self.contest.refresh_from_db()
        pinned = self.contest.conservative_config[str(cp.pk)]
        self.assertEqual(pinned['dependencies']['2'], [1])
        self.assertEqual(pinned['problem_id'], mirror.pk)
        self.dependencies({'2': [3]})
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.conservative_config[str(cp.pk)]['dependencies']['2'], [1])
        with patch('judge.tasks.conservative.build_conservative.delay'):
            start_build(self.contest.pk)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.conservative_config[str(cp.pk)]['dependencies']['2'], [3])

    def test_mirror_of_manual_root_uses_root_layout_and_native_dependencies(self):
        self.problem.is_manually_managed = True
        self.problem.save()
        self.submit()
        self.dependencies({'2': [1]})
        mirror = create_problem(code='conservative_manual_mirror', mirror_of=self.problem)
        init = 'test_cases:\n- batched: []\n- batched: []\n- batched: []\n  dependencies: [1]\n'
        with patch('judge.models.problem_data_storage.open', return_value=StringIO(init)) as read:
            config = snapshot_problem(mirror)
        read.assert_called_once_with('%s/init.yml' % self.problem.code)
        self.assertEqual(config['native_dependencies']['3'], [1])
        self.assertEqual(config['dependencies']['2'], [1])
        self.assertEqual(config['problem_id'], mirror.pk)

    def test_min_then_dependencies_then_best_single_submission(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit(((1, 0), (1, 1), (1, 1)))
        result = calculate(sub, snapshot_problem(self.problem), 100, True)
        self.assertEqual(result['points'], 50)
        self.assertTrue(result['batches']['2']['limited_by_dependencies'])
        self.dependencies({'2': [1], '3': [2]})
        self.assertEqual(calculate(sub, snapshot_problem(self.problem), 100, True)['points'], 0)

    def test_cycles_self_and_missing_rejected(self):
        for graph in ({'1': [1]}, {'1': [4]}, {'1': [2], '2': [1]}):
            with self.subTest(graph=graph), self.assertRaises(ValidationError):
                validate_dependencies(graph, (1, 2, 3))

    def test_snapshot_pins_defaults_until_rebuild_and_old_generation_cannot_write(self):
        sub, cs = self.submit(((1, 0), (1, 1), (1, 1)))
        revision = self.build()
        self.assertEqual(self.contest.conservative_state, 'ON')
        self.dependencies({'2': [1]})
        refresh_one(cs.pk, revision)
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_points, 80)
        new = self.build()
        refresh_one(cs.pk, revision)
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_points, 50)
        self.assertEqual(cs.conservative_data['generation'], new)
        stop(self.contest.pk)
        self.assertIsNone(refresh_one(cs.pk, new))

    def test_min_best_differs_from_sum_best_and_does_not_merge_subtasks(self):
        a, ca = self.submit(((1, 1), (1, 1), (1, 0)))  # SUM75, MIN50
        b, cb = self.submit(((0.7, 0.7), (0.7, 0.7), (0.7, 0.7)))  # SUM70, MIN70
        c, cc = self.submit(((1, 0), (1, 0), (1, 1)))  # SUM75, MIN50; merging A/C would give 100
        revision = self.build()
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<td class="user-points">70</td>', html=False)
        self.assertContains(response, reverse('submission_status', args=[b.pk]))
        self.assertNotContains(response, '<td class="user-points">100</td>', html=False)
        self.assertEqual(Submission.objects.get(pk=a.pk).case_points, 75)

    def test_legacy_raw_results_reused_without_rejudge(self):
        sub, cs = self.submit(((1, 0), (1, 1), (1, 1)), legacy=True)
        with patch.object(Submission, 'judge') as judge:
            self.build()
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_points, 80)
        judge.assert_not_called()

    def test_skipped_tests_resolved_only_with_known_failure(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit(((1, 0), (0, 0), (1, 1)), statuses={3: 'SC', 4: 'SC'})
        self.assertEqual(calculate(sub, snapshot_problem(self.problem), 100, True)['points'], 50)
        self.dependencies({})
        with self.assertRaises(ValidationError):
            calculate(sub, snapshot_problem(self.problem), 100, True)

    def test_missing_results_fail_build_with_diagnostics(self):
        sub, cs = self.submit()
        sub.test_cases.filter(case=6).delete()
        self.build()
        self.assertEqual(self.contest.conservative_state, 'FAILED')
        self.assertIn('#%s' % sub.pk, self.contest.conservative_error)
        self.assertNotContains(self.client.get(reverse('contest_conservative', args=[self.contest.key])),
                               self.contest.conservative_error)

    def test_rejudge_decrease_and_ce_clear_stale_points(self):
        sub, cs = self.submit()
        revision = self.build()
        sub.test_cases.filter(case=1).update(points=0, status='WA')
        refresh_one(cs.pk, revision)
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_points, 80)
        sub.status = sub.result = 'CE'
        sub.save()
        refresh_one(cs.pk, revision)
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_points, 0)

    def test_partial_checker_scaling_and_partial_disabled(self):
        sub, cs = self.submit(((0.5, 1), (1, 1), (1, 1)))
        config = snapshot_problem(self.problem)
        self.assertEqual(calculate(sub, config, 200, True)['points'], 180)
        self.assertEqual(calculate(sub, config, 200, False)['points'], 0)

    def test_dependency_includes_partial_ratios_without_a_full_score_gate(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit(((0.6, 0.6), (0.8, 0.8), (1, 1)))
        result = calculate(sub, snapshot_problem(self.problem), 100, True)
        self.assertEqual(result['points'], 80)
        self.assertEqual(result['total'], 100)
        first, second = result['batches']['1'], result['batches']['2']
        self.assertEqual((first['points'], first['total']), (12, 20))
        self.assertEqual((second['raw_points'], second['points'], second['total']), (24, 18, 30))
        self.assertEqual(second['ratio'], 0.6)

    def test_transitive_and_diamond_dependencies_propagate_min_ratio_once(self):
        self.dependencies({'2': [1], '3': [1, 2]})
        sub, cs = self.submit(((0.6, 0.6), (0.8, 0.8), (0.9, 0.9)))
        result = calculate(sub, snapshot_problem(self.problem), 100, True)
        self.assertEqual(result['points'], 60)
        self.assertEqual([result['batches'][str(n)]['points'] for n in (1, 2, 3)], [12, 18, 30])
        self.assertEqual(result['total'], 100)

    def test_unequal_test_weights_and_batch_maxima_use_ratios_not_raw_points(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit()
        for case, total, points in ((1, 2, 1.2), (2, 18, 14.4), (3, 27, 24.3), (4, 3, 2.4)):
            sub.test_cases.filter(case=case).update(total=total, points=points)
        result = calculate(sub, snapshot_problem(self.problem), 100, True)
        self.assertEqual(result['points'], 80)
        self.assertEqual(result['batches']['2']['points'], 18)
        self.assertEqual(result['batches']['2']['total'], 30)

    def test_lower_own_ratio_is_preserved_with_partial_dependency(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit(((0.9, 0.9), (0.6, 0.6), (1, 1)))
        result = calculate(sub, snapshot_problem(self.problem), 100, True)
        self.assertEqual(result['points'], 86)
        self.assertEqual(result['batches']['2']['ratio'], 0.6)
        self.assertFalse(result['batches']['2']['limited_by_dependencies'])

    def test_partial_dependency_cannot_resolve_missing_own_tests_as_zero(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit(((0.6, 0.6), (0, 0), (1, 1)), statuses={3: 'SC', 4: 'SC'})
        with self.assertRaises(ValidationError):
            calculate(sub, snapshot_problem(self.problem), 100, True)

    def test_old_semantics_are_not_served_until_rebuild(self):
        self.submit()
        self.build()
        config = self.contest.conservative_config
        for item in config.values():
            item.pop('scoring_version')
        Contest.objects.filter(pk=self.contest.pk).update(conservative_config=config)
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw')
        self.assertNotContains(response, '<td class="user-points">100</td>')
        self.assertContains(response, 'needs to be rebuilt')

    def test_public_raw_never_reads_raw_testcases_and_uses_own_submission_scope(self):
        sub, cs = self.submit()
        self.build()
        url = reverse('contest_conservative', args=[self.contest.key]) + '?raw'
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(any('judge_submissiontestcase' in q['sql'] for q in queries))
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=True)
        cache.clear()
        self.assertNotContains(self.client.get(url), reverse('submission_status', args=[sub.pk]))

    def test_freeze_hidden_subtasks_and_visibility(self):
        sub, cs = self.submit()
        self.build()
        Contest.objects.filter(pk=self.contest.pk).update(has_hidden_subtasks=True)
        self.cp.hidden_subtasks = '2'
        with self.captureOnCommitCallbacks(execute=False):
            self.cp.save()
        cache.clear()
        url = reverse('contest_conservative', args=[self.contest.key])
        self.assertContains(self.client.get(url), '>70<', html=False)
        Contest.objects.filter(pk=self.contest.pk).update(frozen_last_minutes=30, end_time=timezone.now())
        cache.clear()
        self.assertNotContains(self.client.get(url), reverse('submission_status', args=[sub.pk]))

    def test_dependency_editor_separate_form_works_even_when_test_data_locked(self):
        self.client.force_login(self.users['superuser'])
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='ON')
        url = reverse('problem_data', args=[self.problem.code])
        response = self.client.get(url)
        self.assertContains(response, 'conservative-dependencies-form')
        values = QueryDict(mutable=True)
        values.update(action='save-conservative-dependencies',
                      **{'conservative-layout': dependency_context(self.problem)['conservative_layout']})
        values.setlist('conservative-dep-2', ['1'])
        with patch('judge.utils.problem_data.ProblemDataCompiler.generate') as compile:
            response = self.client.post(url, values)
        self.assertEqual(response.status_code, 302)
        compile.assert_not_called()
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.conservative_config['dependencies']['2'], [1])
        self.assertEqual(self.problem.subtask_score_state, 'ON')

    def test_controls_check_editor_permission_and_start_async(self):
        url = reverse('contest_conservative_controls', args=[self.contest.key])
        self.client.force_login(self.users['normal'])
        with patch('judge.tasks.conservative.build_conservative.delay') as task:
            self.client.post(url, {'action': 'enable'})
            task.assert_not_called()
        self.client.force_login(self.users['superuser'])
        with patch('judge.tasks.conservative.build_conservative.delay') as task, self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.post(url, {'action': 'enable'}).status_code, 302)
        task.assert_called_once()

    def test_testcase_count_changes_keep_compatible_dependency_defaults(self):
        self.dependencies({'2': [1]})
        self.problem.cases.filter(type='C').first().delete()
        self.assertEqual(snapshot_problem(self.problem)['dependencies']['2'], [1])
        self.assertFalse(dependency_context(self.problem)['conservative_dependency_stale'])

    def test_changed_subtask_maximum_requires_review(self):
        self.dependencies({'2': [1]})
        marker = self.problem.cases.filter(type='S').order_by('order').first()
        marker.points += 1
        marker.save()
        with self.assertRaises(ValidationError):
            snapshot_problem(self.problem)

    def test_historical_test_counts_ids_and_positions_can_differ(self):
        self.dependencies({'2': [1]})
        sub, cs = self.submit()
        sub.test_cases.filter(batch=1).delete()
        for number, ratio in zip((10, 20, 30), (0.5, 0.8, 1.0)):
            SubmissionTestCase.objects.create(submission=sub, case=number, batch=1,
                total=20/3, points=20/3*ratio, status='AC')
        sub.batch_scoring['full'][0]['case_id'] = 999999
        sub.batch_scoring['full'][0]['count'] = 99
        result = calculate(sub, snapshot_problem(self.problem), 100, True)
        self.assertEqual(result['points'], 75)
        self.assertEqual(result['batches']['1']['total'], 20)
        self.assertEqual(result['batches']['2']['total'], 30)

    def test_same_total_but_different_corresponding_subtask_maxima_is_rejected(self):
        sub, cs = self.submit()
        sub.test_cases.filter(case=1).update(total=11)
        sub.test_cases.filter(case=3).update(total=14, points=14)
        with self.assertRaises(ValidationError):
            calculate(sub, snapshot_problem(self.problem), 100, True)

    def test_different_subtask_count_is_rejected(self):
        sub, cs = self.submit()
        sub.test_cases.filter(case=6).update(batch=4)
        with self.assertRaises(ValidationError):
            calculate(sub, snapshot_problem(self.problem), 100, True)

    def test_standalone_test_is_rejected_even_when_it_has_zero_points(self):
        sub, cs = self.submit()
        extra = SubmissionTestCase.objects.create(submission=sub, case=100, total=0, points=0, status='AC')
        for batch in (None, 0):
            extra.batch = batch
            extra.save()
            with self.subTest(batch=batch), self.assertRaises(ValidationError):
                calculate(sub, snapshot_problem(self.problem), 100, True)

    def test_current_layout_rejects_standalone_zero_point_test(self):
        ProblemTestCase.objects.create(dataset=self.problem, order=100, type='C', points=0,
            input_file='standalone.in', output_file='standalone.out', is_pretest=False)
        self.assertEqual(dependency_context(self.problem)['conservative_rows'], [])
        with self.assertRaises(ValidationError):
            snapshot_problem(self.problem)

    def test_current_test_counts_can_change_during_active_ranking(self):
        sub, cs = self.submit()
        revision = self.build()
        self.problem.cases.filter(type='C').first().delete()
        self.assertEqual(refresh_one(cs.pk, revision), 'valid')
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_points, 100)

    def test_manually_managed_layout_ignores_interleaved_case_positions(self):
        sub, cs = self.submit()
        for case in sub.test_cases.all():
            case.case = 100 * (case.case % 2) + case.batch
            case.save()
        self.problem.is_manually_managed = True
        self.problem.save()
        config = snapshot_problem(self.problem)
        self.assertEqual([row['total'] for row in config['layout']], [20, 30, 50])
        self.assertEqual(calculate(sub, config, 100, True)['points'], 100)

    def test_previous_default_config_hash_does_not_require_case_identity(self):
        self.dependencies({'2': [1]})
        config = self.problem.conservative_config
        config.pop('subtasks')
        config.update(version=1, layout='previous-testcase-identity')
        Problem.objects.filter(pk=self.problem.pk).update(conservative_config=config)
        self.problem.refresh_from_db()
        self.assertEqual(snapshot_problem(self.problem)['dependencies']['2'], [1])

    def test_non_superuser_test_editor_neither_sees_nor_saves_dependencies(self):
        self.client.force_login(self.users['staff_problem_edit_all_with_rejudge'])
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='ON')
        url = reverse('problem_data', args=[self.problem.code])
        self.assertNotContains(self.client.get(url), 'conservative-dependencies-form')
        response = self.client.post(url, {'action': 'save-conservative-dependencies',
            'conservative-layout': dependency_context(self.problem)['conservative_layout'], 'conservative-dep-2': ['1']})
        self.assertEqual(response.status_code, 403)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.conservative_config, {})

    @override_settings(CSRF_FAILURE_VIEW='django.views.csrf.csrf_failure')
    def test_dependency_form_csrf_is_enforced(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.users['superuser'])
        response = client.post(reverse('problem_data', args=[self.problem.code]),
                               {'action': 'save-conservative-dependencies'})
        self.assertEqual(response.status_code, 403)

    def test_build_in_progress_and_ended_contest_still_poll(self):
        self.submit()
        self.build()
        Contest.objects.filter(pk=self.contest.pk).update(end_time=timezone.now()-timedelta(days=1),
                                                        conservative_state='BUILDING')
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key]))
        self.assertContains(response, 'conservative-output')
        self.assertContains(response, 'setTimeout(update_ranking, 10000)')
        self.assertNotContains(response, '<td class="user-points">100</td>')

    def test_delete_best_falls_back_to_second_best(self):
        first, cs = self.submit()
        second, cs2 = self.submit(((1, 0), (1, 1), (1, 1)))
        self.build()
        first.delete()
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw')
        self.assertContains(response, '<td class="user-points">80</td>')
        self.assertContains(response, reverse('submission_status', args=[second.pk]))

    def test_zero_point_prerequisite_does_not_automatically_pass(self):
        marker = self.problem.cases.filter(type='S').order_by('order').first()
        ProblemTestCase.objects.filter(pk=marker.pk).update(points=0)
        self.dependencies({'2': [1]})
        sub, cs = self.submit()
        sub.test_cases.filter(batch=1).update(total=0, points=0, status='WA')
        result = calculate(sub, snapshot_problem(self.problem), 80, True)
        self.assertEqual(result['points'], 50)

    def test_private_public_link_mode_and_expiry(self):
        sub, cs = self.submit()
        self.build()
        Contest.objects.filter(pk=self.contest.pk).update(is_private=True)
        link = ContestPublicRankingLink.create_for(self.contest)
        url = reverse('public_conservative_ranking', args=[link.token])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<td class="user-points">100</td>')
        self.assertContains(response, reverse('public_ranking', args=[link.token]))
        link.expires_at = timezone.now()-timedelta(seconds=1)
        link.save()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_pending_and_pretest_are_distinct_from_zero(self):
        sub, cs = self.submit(status='QU')
        self.assertEqual(calculate(sub, snapshot_problem(self.problem), 100, True), {'status': 'pending'})
        sub.status = 'D'
        sub.is_pretested = True
        sub.batch_scoring['pretests'] = [{'batch': 1, 'count': 1, 'total': 1}]
        with self.assertRaises(ValidationError):
            calculate(sub, snapshot_problem(self.problem), 100, True)

    def test_non_superuser_contest_editor_cannot_manage_conservative(self):
        user = self.users['staff_problem_edit_all_with_rejudge']
        self.contest.authors.add(user.profile)
        self.client.force_login(user)
        response = self.client.get(reverse('contest_edit', args=[self.contest.key]))
        self.assertNotContains(response, 'id="conservative-controls"')
        url = reverse('contest_conservative_controls', args=[self.contest.key])
        for action in ('enable', 'rebuild', 'disable', 'visibility'):
            self.assertEqual(self.client.post(url, {'action': action}).status_code, 403)

    def test_visible_ranking_preserves_own_only_and_full_contest_scopes(self):
        own, _ = self.submit(((1, 0), (1, 1), (1, 1)))
        other_user = create_user(username='other_conservative_viewer')
        other_part = create_contest_participation(contest=self.contest, user=other_user.profile)
        other, other_cs = self.submit()
        Submission.objects.filter(pk=other.pk).update(user=other_user.profile)
        ContestSubmission.objects.filter(pk=other_cs.pk).update(participation=other_part)
        self.build()
        Contest.objects.filter(pk=self.contest.pk).update(scoreboard_visibility=Contest.SCOREBOARD_HIDDEN)
        profile = self.users['normal'].profile
        profile.current_contest = self.part
        profile.save(update_fields=['current_contest'])
        self.client.force_login(self.users['normal'])
        for raw in ('', '?raw'):
            response = self.client.get(reverse('contest_conservative', args=[self.contest.key]) + raw)
            self.assertContains(response, reverse('submission_status', args=[own.pk]))
            self.assertNotContains(response, reverse('submission_status', args=[other.pk]))
        self.client.logout()
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw')
        self.assertNotContains(response, 'id="ranking-table"')
        Contest.objects.filter(pk=self.contest.pk).update(scoreboard_visibility=Contest.SCOREBOARD_VISIBLE)
        cache.clear()
        for user in (None, self.users['normal']):
            if user:
                self.client.force_login(user)
            response = self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw')
            self.assertContains(response, reverse('submission_status', args=[own.pk]))
            self.assertContains(response, reverse('submission_status', args=[other.pk]))

    def test_hidden_ranking_allows_only_authors_curators_and_superusers(self):
        self.submit()
        self.build()
        Contest.objects.filter(pk=self.contest.pk).update(show_conservative_ranking=False)
        author = create_user(username='conservative_author')
        curator = create_user(username='conservative_curator')
        editor = create_user(username='conservative_global_editor', user_permissions=('edit_all_contest',))
        viewer = create_user(username='conservative_scoreboard_viewer')
        tester = create_user(username='conservative_tester')
        self.contest.authors.add(author.profile)
        self.contest.curators.add(curator.profile)
        self.contest.testers.add(tester.profile)
        self.contest.view_contest_scoreboard.add(viewer.profile)
        url = reverse('contest_conservative', args=[self.contest.key])
        for user in (author, curator, self.users['superuser']):
            self.client.force_login(user)
            for raw in ('', '?raw'):
                self.assertContains(self.client.get(url+raw), 'id="ranking-table"')
            response = self.client.get(reverse('contest_view', args=[self.contest.key]))
            self.assertContains(response, url)
        for user in (None, self.users['normal'], viewer, tester, editor):
            with self.subTest(user=user):
                self.client.logout()
                if user:
                    self.client.force_login(user)
                for raw in ('', '?raw', '?raw&show_virtual=true'):
                    self.assertNotContains(self.client.get(url+raw), 'id="ranking-table"')
                self.assertNotContains(self.client.get(reverse('contest_view', args=[self.contest.key])), url)
                # Hiding Conservative does not hide the normal scoreboard.
                self.assertContains(self.client.get(reverse('contest_ranking', args=[self.contest.key])+'?raw'),
                                    'id="ranking-table"')

    def test_hidden_preview_is_available_to_authors_before_start_and_in_offline_mode(self):
        self.submit()
        self.build()
        author = self.users['normal']
        self.contest.authors.add(author.profile)
        Contest.objects.filter(pk=self.contest.pk).update(show_conservative_ranking=False,
            offline_mode=True, start_time=timezone.now()+timedelta(days=1))
        self.client.force_login(author)
        url = reverse('contest_conservative', args=[self.contest.key])
        self.assertContains(self.client.get(url+'?raw'), 'id="ranking-table"')
        self.assertContains(self.client.get(url), url)

    def test_visible_conservative_respects_offline_and_private_contest_access(self):
        self.submit()
        self.build()
        url = reverse('contest_conservative', args=[self.contest.key])
        Contest.objects.filter(pk=self.contest.pk).update(offline_mode=True)
        self.client.force_login(self.users['normal'])
        self.assertNotContains(self.client.get(url+'?raw'), 'id="ranking-table"')
        Contest.objects.filter(pk=self.contest.pk).update(offline_mode=False, is_private=True)
        self.client.logout()
        self.assertEqual(self.client.get(url+'?raw').status_code, 403)

    def test_visibility_change_revokes_warm_cached_pages_and_public_links_without_rebuild(self):
        self.submit()
        self.build()
        before = (self.contest.conservative_revision, self.contest.conservative_config)
        link = ContestPublicRankingLink.create_for(self.contest)
        normal_url = reverse('contest_conservative', args=[self.contest.key])
        public_url = reverse('public_conservative_ranking', args=[link.token])
        self.assertContains(self.client.get(normal_url+'?raw'), 'id="ranking-table"')
        self.assertContains(self.client.get(public_url+'?raw'), 'id="ranking-table"')
        self.client.force_login(self.users['superuser'])
        controls = reverse('contest_conservative_controls', args=[self.contest.key])
        for value in ('0', '1'):
            with patch('judge.tasks.conservative.build_conservative.delay') as build, \
                    self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(controls, {'action': 'visibility', 'show_conservative_ranking': value})
            self.assertEqual(response.status_code, 302)
            build.assert_not_called()
            self.contest.refresh_from_db()
            self.assertEqual(self.contest.show_conservative_ranking, value == '1')
            self.assertEqual((self.contest.conservative_revision, self.contest.conservative_config), before)
            self.assertEqual(self.contest.conservative_state, 'ON')
            # Authenticated organizers retain access, including through a token.
            self.assertContains(self.client.get(normal_url+'?raw'), 'id="ranking-table"')
            self.assertContains(self.client.get(public_url+'?raw'), 'id="ranking-table"')
            self.client.logout()
            if value == '0':
                self.assertNotContains(self.client.get(normal_url+'?raw'), 'id="ranking-table"')
                self.assertEqual(self.client.get(public_url+'?raw').status_code, 404)
                self.assertNotContains(self.client.get(reverse('public_ranking', args=[link.token])), public_url)
            else:
                self.assertContains(self.client.get(normal_url+'?raw'), 'id="ranking-table"')
                self.assertContains(self.client.get(public_url+'?raw'), 'id="ranking-table"')
            self.client.force_login(self.users['superuser'])

    def test_invalid_visibility_does_not_change_policy_and_off_still_hides_ranking(self):
        self.submit()
        self.build()
        self.client.force_login(self.users['superuser'])
        url = reverse('contest_conservative_controls', args=[self.contest.key])
        for value in ('invalid', ''):
            self.client.post(url, {'action': 'visibility', 'show_conservative_ranking': value})
            self.contest.refresh_from_db()
            self.assertTrue(self.contest.show_conservative_ranking)
        stop(self.contest.pk)
        self.assertNotContains(self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw'),
                              'id="ranking-table"')

    def test_visibility_control_renders_saved_setting_only_for_superusers(self):
        self.client.force_login(self.users['superuser'])
        url = reverse('contest_edit', args=[self.contest.key])
        response = self.client.get(url)
        self.assertContains(response, 'name="show_conservative_ranking"')
        self.assertContains(response, '<option value="1" selected>Yes</option>', html=True)
        Contest.objects.filter(pk=self.contest.pk).update(show_conservative_ranking=False)
        self.assertContains(self.client.get(url), '<option value="0" selected>No</option>', html=True)
        user = create_user(username='conservative_edit_own', user_permissions=('edit_own_contest',))
        self.contest.authors.add(user.profile)
        self.client.force_login(user)
        self.assertNotContains(self.client.get(url), 'name="show_conservative_ranking"')

    def test_normal_data_editor_has_two_independent_tables(self):
        self.client.force_login(self.users['superuser'])
        response = self.client.get(reverse('problem_data', args=[self.problem.code]))
        self.assertContains(response, 'id="case-table"')
        self.assertContains(response, 'id="conservative-dependencies-form"')
        # Optional inert preview for visual QA, generated only from the test DB.
        import os
        if os.environ.get('CONSERVATIVE_EDITOR_PREVIEW'):
            import re
            from pathlib import Path
            html = response.content.decode()
            html = re.sub(r'<input[^>]*name=[\'\"]csrfmiddlewaretoken[\'\"][^>]*>', '', html)
            html = re.sub(r'<form\b[^>]*>', '<form onsubmit="return false">', html)
            Path(os.environ['CONSERVATIVE_EDITOR_PREVIEW']).write_text(html)

    def test_public_link_never_caches_superuser_diagnostics(self):
        self.submit()
        self.build()
        link = ContestPublicRankingLink.create_for(self.contest)
        Contest.objects.filter(pk=self.contest.pk).update(conservative_state='FAILED',
                                                        conservative_error='PRIVATE-BUILD-DIAGNOSTIC')
        url = reverse('public_conservative_ranking', args=[link.token])+'?raw'
        self.client.force_login(self.users['superuser'])
        self.assertNotContains(self.client.get(url), 'PRIVATE-BUILD-DIAGNOSTIC')
        self.client.logout()
        self.assertNotContains(self.client.get(url), 'PRIVATE-BUILD-DIAGNOSTIC')

    def test_broker_failure_never_blocks_primary_judging_or_keeps_old_derived_score(self):
        sub, cs = self.submit()
        self.build()
        completed_primary_callback = []
        from django.db import transaction
        with patch('judge.tasks.conservative.refresh_conservative_submission.delay', side_effect=RuntimeError('offline')):
            with self.captureOnCommitCallbacks(execute=True):
                schedule_submission(sub.pk)
                transaction.on_commit(lambda: completed_primary_callback.append(True))
        self.assertEqual(completed_primary_callback, [True])
        cs.refresh_from_db()
        self.assertIsNone(cs.conservative_points)
        self.assertEqual(cs.conservative_data['status'], 'error')
        sub.refresh_from_db()
        self.assertEqual(sub.points, 100)

    def test_build_broker_failure_is_reported_instead_of_stuck_building(self):
        with patch('judge.tasks.conservative.build_conservative.delay', side_effect=RuntimeError('offline')):
            with self.captureOnCommitCallbacks(execute=True):
                start_build(self.contest.pk)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.conservative_state, 'FAILED')

    def test_submission_commit_after_new_build_enqueues_current_generation(self):
        sub, cs = self.submit()
        revision = self.build()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_submission(sub.pk)
        Contest.objects.filter(pk=self.contest.pk).update(conservative_revision=revision+1)
        with patch('judge.tasks.conservative.refresh_conservative_submission.delay') as task:
            for callback in callbacks:
                callback()
        task.assert_called_once_with(cs.pk, revision+1)
        cs.refresh_from_db()
        self.assertEqual(cs.conservative_data, {'generation': revision+1, 'status': 'pending'})

    def test_aborted_calculation_is_ignored_without_reading_test_results(self):
        sub, cs = self.submit(status='AB')
        with self.assertNumQueries(0):
            self.assertEqual(calculate(sub, None, 100, True), {'status': 'ignored', 'reason': 'aborted'})
        sub.status, sub.result = 'D', 'AB'
        with self.assertNumQueries(0):
            self.assertEqual(calculate(sub, None, 100, True), {'status': 'ignored', 'reason': 'aborted'})

    def test_abort_only_contest_builds_successfully_and_does_not_show_incomplete(self):
        sub, cs = self.submit(status='AB')
        self.build()
        self.assertEqual(self.contest.conservative_state, 'ON')
        self.assertEqual(self.contest.conservative_progress['errors'], 0)
        self.assertEqual(self.contest.conservative_progress['ignored'], 1)
        self.assertEqual(self.contest.conservative_progress['done'], self.contest.conservative_progress['total'])
        cs.refresh_from_db()
        self.assertIsNone(cs.conservative_points)
        self.assertEqual(cs.conservative_data['status'], 'ignored')
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw')
        self.assertNotContains(response, 'provisional')
        self.assertNotContains(response, reverse('submission_status', args=[sub.pk]))

    def test_aborted_rows_do_not_require_a_valid_dataset_snapshot(self):
        revision = self.build()
        sub, cs = self.submit(status='AB')
        Contest.objects.filter(pk=self.contest.pk).update(conservative_config={})
        with patch('judge.utils.conservative.problem_layout') as layout, patch('judge.utils.conservative.calculate') as score:
            self.assertEqual(refresh_one(cs.pk, revision), 'ignored')
        layout.assert_not_called()
        score.assert_not_called()

    def test_abort_is_excluded_even_with_stale_high_points_or_error_metadata(self):
        good, good_cs = self.submit(((1, 0), (1, 1), (1, 1)))
        aborted, aborted_cs = self.submit(status='AB')
        revision = self.build()
        Contest.objects.filter(pk=self.contest.pk).update(show_number_of_submission=True)
        ContestSubmission.objects.filter(pk=aborted_cs.pk).update(conservative_points=100,
            conservative_data={'generation': revision, 'scoring_version': 2, 'status': 'valid'})
        url = reverse('contest_conservative', args=[self.contest.key])+'?raw'
        response = self.client.get(url)
        self.assertContains(response, '<td class="user-points">80</td>')
        self.assertContains(response, '<td class="submission-count">1</td>', html=True)
        self.assertNotContains(response, reverse('submission_status', args=[aborted.pk]))
        ContestSubmission.objects.filter(pk=aborted_cs.pk).update(conservative_data={'status': 'error'})
        cache.clear()
        self.assertNotContains(self.client.get(url), 'provisional')
        # Imported histories can have D as status but AB as the result.
        Submission.objects.filter(pk=aborted.pk).update(status='D', result='AB')
        cache.clear()
        self.assertContains(self.client.get(url), '<td class="user-points">80</td>')

    def test_live_abort_clears_previous_best_without_celery(self):
        other, other_cs = self.submit(((1, 0), (1, 1), (1, 1)))
        best, best_cs = self.submit()
        revision = self.build()
        Submission.objects.filter(pk=best.pk).update(status='AB', result='AB')
        with patch('judge.tasks.conservative.refresh_conservative_submission.delay') as task:
            schedule_submission(best.pk)
        task.assert_not_called()
        best_cs.refresh_from_db()
        self.assertIsNone(best_cs.conservative_points)
        self.assertEqual(best_cs.conservative_data['status'], 'ignored')
        response = self.client.get(reverse('contest_conservative', args=[self.contest.key])+'?raw')
        self.assertContains(response, '<td class="user-points">80</td>')

    def test_resuming_old_abort_errors_reuses_completed_scores(self):
        good, good_cs = self.submit()
        aborted, aborted_cs = self.submit(status='AB')
        revision = self.build()
        good_cs.refresh_from_db()
        previous = good_cs.conservative_data
        ContestSubmission.objects.filter(pk=aborted_cs.pk).update(conservative_data={
            'generation': revision, 'status': 'error', 'error': 'Old abort error'})
        Contest.objects.filter(pk=self.contest.pk).update(conservative_state='BUILDING',
                                                       conservative_error='Old abort error')
        with patch('judge.utils.conservative.calculate') as score, patch.object(Submission, 'judge') as judge:
            build_conservative.run(self.contest.pk, revision)
        score.assert_not_called()
        judge.assert_not_called()
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.conservative_state, 'ON')
        self.assertEqual(self.contest.conservative_error, '')
        self.assertEqual(self.contest.conservative_progress['errors'], 0)
        self.assertEqual(self.contest.conservative_progress['ignored'], 1)
        good_cs.refresh_from_db()
        self.assertEqual(good_cs.conservative_data, previous)
        aborted_cs.refresh_from_db()
        self.assertEqual(aborted_cs.conservative_data['status'], 'ignored')

    def test_internal_error_is_still_reported_separately_from_abort(self):
        self.submit(status='IE')
        self.build()
        self.assertEqual(self.contest.conservative_state, 'FAILED')
        self.assertEqual(self.contest.conservative_progress['errors'], 1)
        self.assertEqual(self.contest.conservative_progress['ignored'], 0)
