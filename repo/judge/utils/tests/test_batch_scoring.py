from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.contest_format.ioi import IOIContestFormat
from judge.contest_format.ultimate import UltimateContestFormat
from judge.models import (ContestSubmission, Language, ProblemData, ProblemTestCase, Submission,
                          SubmissionBatchResult, SubmissionTestCase)
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_participation,
                                     create_contest_problem, create_problem)
from judge.utils.batch_scoring import (display_batch_score, editable_batch_policies, finalize_batch_results,
                                       save_batch_policies, score_cases, scoring_snapshot)
from judge.views.problem_data import ProblemCaseForm
from judge.views.submission import group_test_cases


class FormulaTests(SimpleTestCase):
    def cases(self, *pairs):
        return [SimpleNamespace(points=points, total=total) for points, total in pairs]

    def test_min_includes_first_failed_case(self):
        for pairs in (((0, 10), (10, 10)), ((10, 10), (0, 10)), ((0, 20),)):
            with self.subTest(pairs=pairs):
                self.assertEqual(score_cases(self.cases(*pairs), 'min'), (0, 20))

    def test_min_uses_ratios_with_unequal_weights(self):
        self.assertEqual(score_cases(self.cases((5, 10), (80, 80)), 'min'), (45, 90))

    def test_sum_keeps_partial_points(self):
        self.assertEqual(score_cases(self.cases((0, 10), (40, 80)), 'sum'), (40, 90))

    def test_zero_weight_cases_do_not_divide_by_zero(self):
        self.assertEqual(score_cases(self.cases((0, 0), (5, 10)), 'min'), (5, 10))
        self.assertEqual(score_cases(self.cases((0, 0)), 'min'), (0, 0))

    def test_testlib_decimal_percentages_keep_fractional_case_weights(self):
        cases = self.cases(*[(rate / 100 / 3, 1 / 3) for rate in (33.333333, 66.666667, 99.9999)])
        minimum, total = score_cases(cases, 'min')
        summed, _ = score_cases(cases, 'sum')
        self.assertAlmostEqual(minimum, 0.33333333, places=12)
        self.assertAlmostEqual(summed, 0.6666663333333334, places=12)
        self.assertEqual(total, 1)

    def test_tiny_nonzero_checker_points_are_not_rounded_in_a_batch(self):
        for mode in ('min', 'sum'):
            points, total = score_cases(self.cases((1e-8, 1), (1e-8, 1)), mode)
            self.assertEqual((points, total), (2e-8, 2))

    def test_exam_progress_uses_unrounded_ratio_for_full_credit(self):
        from judge.tasks.exams import _compute_progress_points as task_points
        from judge.views.exams import _compute_progress_points as view_points
        for calculate in (task_points, view_points):
            with self.subTest(calculate=calculate.__module__):
                self.assertEqual(calculate(99.9999, 100, 100, False), 0)
                self.assertEqual(calculate(1, 3, 100, True), 33.333)
                self.assertEqual(calculate(100, 100, 100, False), 100)


class BatchScoringTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='scorebatches', partial=True, points=100)
        cls.language = Language.get_python3()
        cls.contest = create_contest(key='scorebatches')
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.problem, points=200, partial=True)
        cls.participation = create_contest_participation(contest=cls.contest.key, user='normal')

    def add_batch(self, maximum=20, mode='min', pretest=False):
        order = self.problem.cases.count()
        marker = ProblemTestCase.objects.create(dataset=self.problem, order=order, type='S',
                                                points=maximum, batch_type=mode, is_pretest=pretest)
        for i in range(2):
            ProblemTestCase.objects.create(dataset=self.problem, order=order+i+1, type='C',
                input_file='%s.in' % (order+i), output_file='%s.out' % (order+i), is_pretest=pretest)
        ProblemTestCase.objects.create(dataset=self.problem, order=order+3, type='E', is_pretest=pretest)
        return marker

    def submission(self, batches, legacy=False, pretest=False, statuses=None):
        sub = Submission.objects.create(problem=self.problem, user=self.users['normal'].profile,
            language=self.language, batch_scoring=None if legacy else scoring_snapshot(self.problem),
            status='D', result='AC', is_pretested=pretest)
        ContestSubmission.objects.create(submission=sub, problem=self.cp, participation=self.participation)
        case = 0
        for batch, pairs in enumerate(batches, 1):
            for points, total in pairs:
                case += 1
                SubmissionTestCase.objects.create(submission=sub, case=case, batch=batch, points=points,
                    total=total, status=(statuses or {}).get(case, 'AC'), time=0.1, memory=1024)
        if not legacy:
            self.finalize(sub)
        else:
            sub.case_points = sum(case.points for case in sub.test_cases.all())
            sub.case_total = sum(case.total for case in sub.test_cases.all())
            sub.save()
        return sub

    def finalize(self, sub):
        sub.case_points, sub.case_total = finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        sub.save()

    def test_mixed_batches_ui_and_api_share_saved_results(self):
        self.add_batch(20, 'min')
        self.add_batch(80, 'sum')
        sub = self.submission([[(0, 10), (10, 10)], [(40, 40), (0, 40)]])
        self.assertEqual((sub.case_points, sub.case_total), (40, 100))
        batches = group_test_cases(sub, set(), self.problem)[0]
        self.assertEqual([(b['points'], b['total'], b['scoring_type']) for b in batches],
                         [(0, 20, 'min'), (40, 80, 'sum')])
        # Editing the problem does not reinterpret finished submissions.
        self.problem.cases.filter(type='S').update(batch_type='sum')
        self.assertEqual(group_test_cases(sub, set(), self.problem)[0][0]['points'], 0)
        from judge.views.api.api_v2 import APISubmissionDetail
        from django.test import RequestFactory
        view = APISubmissionDetail()
        view.request = RequestFactory().get('/')
        view.request.user = self.users['superuser']
        data = view.get_object_data(sub)
        self.assertEqual(data['cases'][0]['scoring_type'], 'min')
        self.assertEqual(data['cases'][0]['points'], 0)

    def test_ioi_does_not_combine_individual_tests(self):
        self.add_batch()
        self.submission([[(10, 10), (0, 10)]])
        self.submission([[(0, 10), (10, 10)]])
        format = IOIContestFormat(self.contest, {})
        rows = format.get_results_by_subtask(self.participation)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 0)

    def test_new_run_without_finalized_summary_never_uses_legacy_sum(self):
        self.add_batch()
        sub = self.submission([[(10, 10), (0, 10)]])
        sub.batch_results.all().delete()
        self.assertEqual(IOIContestFormat(self.contest, {}).get_results_by_subtask(self.participation), [])

    def test_ioi_best_each_batch_and_contest_scale(self):
        self.add_batch(20)
        self.add_batch(80)
        self.submission([[(10, 10), (10, 10)], [(0, 40), (40, 40)]])
        self.submission([[(0, 10), (10, 10)], [(40, 40), (40, 40)]])
        format = IOIContestFormat(self.contest, {})
        self.assertEqual([row[4] for row in format.get_results_by_subtask(self.participation)], [40, 160])
        format.update_participation(self.participation)
        self.assertEqual(self.participation.score, 200)

    def test_ioi_partial_credit_hidden_batch_and_earliest_tie(self):
        self.add_batch(20)
        self.add_batch(80)
        first = self.submission([[(5, 10), (10, 10)], [(20, 40), (40, 40)]])
        self.submission([[(5, 10), (10, 10)], [(20, 40), (40, 40)]])
        self.cp.hidden_subtasks = '2'
        self.cp.save()
        format = IOIContestFormat(self.contest, {})
        rows = format.get_results_by_subtask(self.participation)
        self.assertEqual([row[4] for row in rows], [20, 80])
        self.assertEqual(rows[0][2].replace(tzinfo=None), first.date.replace(tzinfo=None))
        format.update_participation(self.participation)
        self.assertEqual(self.participation.score, 20)

    def test_ioi_respects_no_partial_setting(self):
        self.add_batch()
        self.cp.partial = False
        self.cp.save()
        self.submission([[(5, 10), (10, 10)]])
        self.assertEqual(IOIContestFormat(self.contest, {}).get_results_by_subtask(self.participation)[0][4], 0)

    def test_near_full_decimal_is_not_full_credit_when_partial_is_disabled(self):
        self.add_batch()
        sub = self.submission([[(9.99999, 10), (9.99999, 10)]])
        self.cp.partial = False
        self.cp.save()
        sub.update_contest()
        sub.contest.refresh_from_db()
        self.assertEqual(sub.contest.points, 0)
        self.assertEqual(IOIContestFormat(self.contest, {}).get_results_by_subtask(self.participation)[0][4], 0)

    def test_float_representation_error_does_not_remove_full_credit(self):
        self.add_batch(1, 'sum')
        self.cp.partial = False
        self.cp.save()
        sub = self.submission([[(0.1, 0.1), (0.8999999999999999, 0.9)]])
        sub.update_contest()
        sub.contest.refresh_from_db()
        self.assertEqual(sub.contest.points, 200)
        self.assertAlmostEqual(IOIContestFormat(self.contest, {}).get_results_by_subtask(self.participation)[0][4], 200)

    def test_rescore_preserves_decimal_precision_and_batch_snapshot(self):
        from judge.tasks.submission import rescore_problem
        self.add_batch()
        sub = self.submission([[(10 / 3, 10), (10 / 3, 10)]])
        snapshot = sub.batch_scoring
        with patch('judge.tasks.submission.Progress'), patch('judge.models.Profile.calculate_points'):
            self.assertEqual(rescore_problem.run(self.problem.pk), 1)
        sub.refresh_from_db()
        self.assertEqual(sub.points, 33.333)
        self.assertEqual(sub.batch_scoring, snapshot)
        self.assertAlmostEqual(sub.batch_results.get().points, 20 / 3)

    def test_rescore_does_not_promote_near_full_credit(self):
        from judge.tasks.submission import rescore_problem
        self.add_batch()
        sub = self.submission([[(9.99999, 10), (9.99999, 10)]])
        self.problem.partial = False
        self.problem.save()
        with patch('judge.tasks.submission.Progress'), patch('judge.models.Profile.calculate_points'):
            rescore_problem.run(self.problem.pk)
        sub.refresh_from_db()
        self.assertEqual(sub.points, 0)

    def test_hidden_subtask_zero_is_a_sentinel_not_a_real_batch(self):
        self.assertEqual(IOIContestFormat(self.contest, {}).get_hidden_subtasks()[str(self.cp.pk)], set())

    def test_ultimate_uses_last_submission_batch_scores(self):
        self.add_batch()
        self.submission([[(10, 10), (10, 10)]])
        last = self.submission([[(5, 10), (10, 10)]])
        rows = UltimateContestFormat(self.contest, {}).get_results_by_subtask(self.participation)
        self.assertEqual((rows[0][3], rows[0][-1]), (100, last.pk))

    def test_legacy_ioi_and_display_are_not_recomputed(self):
        ProblemData.objects.create(problem=self.problem, legacy_batch_type='sum')
        self.add_batch(mode='min')
        sub = self.submission([[(10, 10), (0, 10)]], legacy=True)
        self.assertEqual(IOIContestFormat(self.contest, {}).get_results_by_subtask(self.participation)[0][4], 100)
        self.assertEqual(display_batch_score(sub, 1, list(sub.test_cases.all())), (10, 20, 'sum'))

    def test_legacy_and_new_scores_use_the_same_contest_units(self):
        self.add_batch()
        self.submission([[(10, 10), (10, 10)]], legacy=True)
        self.submission([[(5, 10), (10, 10)]])
        self.assertEqual(IOIContestFormat(self.contest, {}).get_results_by_subtask(self.participation)[0][4], 200)

    def test_standalone_cases_are_additive(self):
        ProblemTestCase.objects.create(dataset=self.problem, order=1, type='C', points=20, is_pretest=False)
        self.add_batch(80)
        sub = Submission.objects.create(problem=self.problem, user=self.users['normal'].profile,
            language=self.language, status='D', batch_scoring=scoring_snapshot(self.problem))
        SubmissionTestCase.objects.create(submission=sub, case=1, points=10, total=20, status='AC')
        for number in (2, 3):
            SubmissionTestCase.objects.create(submission=sub, case=number, batch=1, points=40, total=40, status='AC')
        self.finalize(sub)
        self.assertEqual((sub.case_points, sub.case_total), (90, 100))

    def resolver_data(self):
        from judge.views.resolver import Resolver
        self.contest.format_name = 'ioi16'
        self.contest.save()
        resolver = Resolver()
        resolver.contest = self.contest
        return resolver.get_contest_json()

    def test_resolver_rounds_after_combining_decimal_batches(self):
        for _ in range(3):
            self.add_batch(1)
        self.cp.points = 1
        self.cp.save()
        self.contest.points_precision = 3
        self.submission([[(0.5, 0.5), (0.5, 0.5)]] * 3)
        data = self.resolver_data()
        self.assertEqual(data['problems']['1'], {'1': 1})
        self.assertEqual(data['users']['1']['problems']['1']['points'], {'1': 1})

    def test_resolver_handles_standalone_cases_and_noncontiguous_problem_order(self):
        ProblemTestCase.objects.create(dataset=self.problem, order=1, type='C', points=20, is_pretest=False)
        sub = Submission.objects.create(problem=self.problem, user=self.users['normal'].profile,
            language=self.language, status='D', batch_scoring=scoring_snapshot(self.problem))
        SubmissionTestCase.objects.create(submission=sub, case=1, points=10, total=20, status='AC')
        ContestSubmission.objects.create(submission=sub, problem=self.cp, participation=self.participation)
        self.finalize(sub)
        self.cp.order = 10
        self.cp.save()
        data = self.resolver_data()
        self.assertEqual(data['problem_sub'], [1])
        self.assertEqual(data['users']['1']['problems']['1']['points'], {'1': 100})

    def test_resolver_uses_hidden_batches_by_problem_id(self):
        self.add_batch()
        self.submission([[(5, 10), (10, 10)]])
        self.cp.hidden_subtasks = '1'
        self.cp.order = 10
        self.cp.save()
        data = self.resolver_data()
        self.assertEqual(data['sub_frozen'], [[2]])
        self.assertEqual(data['users']['1']['problems']['1']['points'], {'1': 0, '2': 100})

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_test_editor_renders_and_saves_scoring_without_regenerating_files(self):
        marker = self.add_batch()
        self.client.force_login(self.users['superuser'])
        url = reverse('problem_data', args=[self.problem.code])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="cases-0-batch_type"')
        context = response.context_data
        values = {}
        def add_form(form):
            for field in form:
                from django.forms import FileField
                if isinstance(field.field, FileField):
                    continue
                value = field.value()
                if value is None and getattr(field.field, 'choices', None):
                    value = next(iter(field.field.choices))[0]
                values[field.html_name] = 'on' if value is True else '' if value is None or value is False else value
        for name in ('mirror_form', 'external_form', 'data_form'):
            add_form(context[name])
        formset = context['cases_formset']
        add_form(formset.management_form)
        for form in formset:
            add_form(form)
        values['cases-0-batch_type'] = 'sum'
        with patch('judge.utils.problem_data.ProblemDataCompiler.generate') as generate, patch(
                'judge.utils.storage_client.notify_problem_dirty') as dirty:
            response = self.client.post(url, values)
        self.assertEqual(response.status_code, 302, getattr(response, 'context_data', {}))
        marker.refresh_from_db()
        self.assertEqual(marker.batch_type, 'sum')
        generate.assert_not_called()
        dirty.assert_not_called()

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'], STORAGE_PLATFORM_ENABLED=True)
    def test_archived_editor_saves_policy_without_restore(self):
        from judge.models import StorageProblemUsage
        marker = self.add_batch()
        StorageProblemUsage.objects.create(problem=self.problem, code=self.problem.code,
            local_status='missing', r2_status='ready', catalog_state='present')
        self.client.force_login(self.users['superuser'])
        url = reverse('problem_data', args=[self.problem.code])
        self.assertContains(self.client.get(url), 'id="batch-scoring-form"')
        with patch('judge.utils.storage_client.ensure_problem_ready') as restore, patch(
                'judge.utils.problem_data.ProblemDataCompiler.generate') as generate:
            response = self.client.post(url, {'action': 'save-batch-scoring', 'batch-mode-full:1': 'sum'})
        self.assertEqual(response.status_code, 302)
        marker.refresh_from_db()
        self.assertEqual(marker.batch_type, 'sum')
        restore.assert_not_called()
        generate.assert_not_called()

    def test_pretest_and_full_have_independent_batch_numbers(self):
        self.add_batch(20, 'sum', pretest=True)
        self.add_batch(80, 'min')
        sub = self.submission([[(0, 10), (10, 10)]], pretest=True)
        self.assertEqual(sub.case_points, 10)
        sub = self.submission([[(0, 40), (40, 40)]])
        self.assertEqual(sub.case_points, 0)

    def test_old_judge_falls_back_to_full_tests_when_pretests_are_absent(self):
        self.add_batch(20, 'sum')
        sub = self.submission([[(0, 10), (10, 10)]], pretest=True)
        self.assertEqual(sub.case_points, 10)

    def test_structure_mismatch_does_not_save_partial_summary(self):
        self.add_batch()
        sub = self.submission([[(10, 10), (10, 10)]])
        sub.test_cases.filter(case=2).delete()
        with self.assertRaisesMessage(ValueError, 'sync test data'):
            self.finalize(sub)
        self.assertEqual(sub.batch_results.count(), 1)

    def test_rejudge_replaces_summary(self):
        marker = self.add_batch()
        sub = self.submission([[(10, 10), (0, 10)]])
        marker.batch_type = 'sum'
        marker.save()
        sub.batch_scoring = scoring_snapshot(self.problem)
        self.finalize(sub)
        self.assertEqual(sub.case_points, 10)
        self.assertEqual(sub.batch_results.count(), 1)

    def test_rejudge_can_archive_existing_raw_testcases(self):
        import reversion
        from reversion.models import Version
        self.add_batch()
        sub = self.submission([[(5, 10), (10, 10)]])
        with reversion.create_revision():
            reversion.add_to_revision(sub)
        self.assertEqual(Version.objects.get_for_object(sub.test_cases.first()).count(), 1)
        self.assertEqual(Version.objects.get_for_object(sub.batch_results.get()).count(), 1)

    def test_policy_is_fixed_while_a_run_is_in_progress(self):
        marker = self.add_batch()
        sub = self.submission([[(10, 10), (0, 10)]])
        sub.status = 'G'
        marker.batch_type = 'sum'
        marker.save()
        self.finalize(sub)
        self.assertEqual(sub.case_points, 0)

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_manual_editor_only_allows_scoring_changes(self):
        self.problem.is_manually_managed = True
        self.problem.save()
        self.submission([[(10, 10), (0, 10)]], legacy=True)
        self.client.force_login(self.users['superuser'])
        url = reverse('problem_data', args=[self.problem.code])
        self.assertContains(self.client.get(url), 'id="batch-scoring-form"')
        self.assertEqual(self.client.post(url, {'action': 'save-batch-scoring',
                                               'batch-mode-full:1': 'sum'}).status_code, 302)
        self.assertEqual(self.client.post(url, {'problem-data-grader': 'interactive'}).status_code, 400)

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_scoring_endpoint_requires_problem_edit_permission(self):
        marker = self.add_batch()
        self.client.force_login(self.users['normal'])
        response = self.client.post(reverse('problem_data', args=[self.problem.code]),
                                    {'action': 'save-batch-scoring', 'batch-mode-full:1': 'sum'})
        self.assertIn(response.status_code, (403, 404))
        marker.refresh_from_db()
        self.assertEqual(marker.batch_type, 'min')

    def test_archived_policies_save_without_storage(self):
        marker = self.add_batch()
        with patch('judge.utils.storage_client.notify_problem_dirty'), patch(
                'judge.utils.problem_data.ProblemDataCompiler.generate') as generate:
            save_batch_policies(self.problem, {'batch-mode-full:1': 'sum'})
        marker.refresh_from_db()
        self.assertEqual(marker.batch_type, 'sum')
        generate.assert_not_called()

    def test_policy_endpoint_and_form_preserve_selection(self):
        marker = self.add_batch(mode='sum')
        form = ProblemCaseForm(instance=marker)
        self.assertEqual(form['batch_type'].value(), 'sum')
        with self.assertRaises(ValidationError):
            save_batch_policies(self.problem, {'batch-mode-full:1': 'bogus'})
        with self.assertRaises(ValidationError):
            save_batch_policies(self.problem, {'batch-mode-full:2': 'sum'})
        from judge.forms import ProblemEditForm
        self.assertNotIn('batch_type', ProblemEditForm.base_fields)

    def test_mirror_uses_root_layout_and_independent_policy(self):
        self.add_batch()
        mirror = create_problem(code='scoremirror', mirror_of=self.problem, partial=True)
        ProblemData.objects.create(problem=mirror, legacy_batch_type='sum')
        self.assertEqual(scoring_snapshot(mirror)['full'][0]['mode'], 'sum')
        save_batch_policies(mirror, {'batch-mode-full:1': 'min'})
        mirror = type(mirror).objects.get(pk=mirror.pk)
        self.assertEqual(scoring_snapshot(mirror)['full'][0]['mode'], 'min')
        self.assertEqual(self.problem.cases.get(type='S').batch_type, 'min')

    def test_manual_structure_uses_legacy_default_and_can_override(self):
        self.problem.is_manually_managed = True
        self.problem.save()
        ProblemData.objects.create(problem=self.problem, legacy_batch_type='sum')
        self.submission([[(10, 10), (0, 10)]], legacy=True)
        self.assertEqual(editable_batch_policies(self.problem)[0]['mode'], 'sum')
        save_batch_policies(self.problem, {'batch-mode-full:1': 'min'})
        self.problem = type(self.problem).objects.get(pk=self.problem.pk)
        sub = self.submission([[(10, 10), (0, 10)]])
        self.assertEqual(sub.case_points, 0)

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_old_judge_packet_is_scored_by_web(self, finished, post):
        self.add_batch()
        sub = self.submission([[(0, 10), (10, 10)]])
        sub.status = 'G'
        sub.save()
        handler = object.__new__(JudgeHandler)
        handler.name = 'legacy-judge'
        handler._free_self = Mock()
        handler._make_json_log = Mock(return_value={})
        handler._post_update_submission = Mock()
        handler.on_grading_end({'name': 'grading-end', 'submission-id': sub.pk})
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.case_points, sub.points), ('D', 0, 0))
        self.assertEqual(sub.test_cases.count(), 2)
        self.assertEqual(sub.batch_results.get().points, 0)

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_bridge_does_not_round_partial_checker_points_to_full_credit(self, finished, post):
        self.add_batch()
        sub = self.submission([[(9.99999, 10), (9.99999, 10)]])
        self.problem.partial = False
        self.problem.save()
        handler = object.__new__(JudgeHandler)
        handler.name = 'decimal-judge'
        handler._free_self = Mock()
        handler._make_json_log = Mock(return_value={})
        handler._post_update_submission = Mock()
        handler.on_grading_end({'name': 'grading-end', 'submission-id': sub.pk})
        sub.refresh_from_db()
        self.assertEqual(sub.points, 0)
        self.assertEqual(sub.result_class, '_AC')

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_structure_mismatch_clears_old_scores_and_finishes_live_update(self, finished, post):
        self.add_batch()
        sub = self.submission([[(10, 10), (10, 10)]])
        sub.update_contest()
        sub.status = 'G'
        sub.save()
        sub.test_cases.filter(case=2).delete()
        handler = object.__new__(JudgeHandler)
        handler.name = 'stale-data-judge'
        handler._free_self = Mock()
        handler._make_json_log = Mock(return_value={})
        handler._post_update_submission = Mock()
        handler.on_grading_end({'name': 'grading-end', 'submission-id': sub.pk})
        sub.refresh_from_db()
        sub.contest.refresh_from_db()
        self.participation.refresh_from_db()
        self.assertEqual((sub.status, sub.result, sub.points, sub.case_points), ('IE', 'IE', 0, 0))
        self.assertEqual(sub.contest.points, 0)
        self.assertEqual(self.participation.score, 0)
        self.assertFalse(sub.batch_results.exists())
        handler._post_update_submission.assert_called_once_with(sub.pk, 'grading-end', done=True)
        post.assert_any_call('sub_%s' % sub.id_secret, {'type': 'internal-error'})
