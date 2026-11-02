import json
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.translation import gettext, override

from judge.bridge.judge_handler import JudgeHandler
from judge.models import Language, ProblemData, ProblemTestCase, Submission
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.utils.batch_scoring import scoring_snapshot
from judge.utils.problem_data import ProblemDataCompiler, ProblemDataError
from judge.views.problem_data import ProblemCaseForm, ProblemCaseFormSet


class WeightedBatchTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='weightedbatch', partial=True, points=90)
        cls.language = Language.get_python3()

    def batch(self, weights, maximum=90, mode='sum'):
        start = ProblemTestCase.objects.create(dataset=self.problem, order=1, type='S', points=maximum,
                                                batch_type=mode, is_pretest=False)
        for index, weight in enumerate(weights, 2):
            ProblemTestCase.objects.create(dataset=self.problem, order=index, type='C', points=weight,
                input_file='%s.in' % index, output_file='%s.out' % index, is_pretest=False)
        ProblemTestCase.objects.create(dataset=self.problem, order=len(weights)+2, type='E', is_pretest=False)
        return start

    def compile(self, percentage=None):
        args = {} if percentage is None else {'type': 'testlib', 'files': 'checker.cpp', 'lang': 'CPP17',
                                               'treat_checker_points_as_percentage': percentage}
        data = ProblemData(problem=self.problem, checker='standard' if percentage is None else 'bridged',
                           custom_checker='weightedbatch/checker.cpp', checker_args=json.dumps(args))
        files = [name for case in self.problem.cases.filter(type='C') for name in (case.input_file, case.output_file)]
        return ProblemDataCompiler(self.problem, data, self.problem.cases.order_by('order'), files).make_init()

    def form_values(self):
        formset = ProblemCaseFormSet(queryset=self.problem.cases.order_by('order'), prefix='cases')
        values = {}
        for form in [formset.management_form, *formset]:
            for field in form:
                value = field.value()
                values[field.html_name] = 'on' if value is True else '' if value is None or value is False else value
        return values

    def bound_forms(self, values):
        return ProblemCaseFormSet(values, queryset=self.problem.cases.order_by('order'), prefix='cases')

    def test_new_batches_default_to_sum_in_model_and_form(self):
        self.assertEqual(ProblemTestCase(type='S').batch_type, 'sum')
        self.assertEqual(ProblemCaseForm()['batch_type'].value(), 'sum')
        existing = self.batch([None], mode='min')
        self.assertEqual(ProblemCaseForm(instance=existing)['batch_type'].value(), 'min')

    def test_blank_weights_default_to_one_and_equal_shares(self):
        self.batch([None] * 20)
        forms = self.bound_forms(self.form_values())
        self.assertTrue(forms.is_valid(), forms.errors)
        self.assertEqual([form.cleaned_data['points'] for form in forms if form.cleaned_data['type'] == 'C'], [1] * 20)
        batch = self.compile()['test_cases'][0]
        weights = [case['points'] for case in batch['batched']]
        self.assertEqual(weights, [1] * 20)
        self.assertEqual(batch['points'] / sum(weights), 4.5)

    def test_weight_edits_in_sum_batches_survive_save_and_compile(self):
        self.batch([1, 1])
        values = self.form_values()
        values['cases-1-points'] = '2'
        values['cases-2-points'] = '3'
        forms = self.bound_forms(values)
        self.assertTrue(forms.is_valid(), forms.errors)
        forms.save()
        self.assertEqual([case['points'] for case in self.compile()['test_cases'][0]['batched']], [2, 3])
        self.assertEqual(self.problem.cases.get(type='S').points, 90)

    def test_weight_edits_are_rejected_in_min_batches(self):
        self.batch([None, None], mode='min')
        values = self.form_values()
        values['cases-1-points'] = '2'
        forms = self.bound_forms(values)
        self.assertFalse(forms.is_valid())
        self.assertIn('points', forms.forms[1].errors)

    def test_existing_weights_are_preserved_when_switching_to_min(self):
        self.batch([1, 2])
        values = self.form_values()
        values['cases-0-batch_type'] = 'min'
        forms = self.bound_forms(values)
        self.assertTrue(forms.is_valid(), forms.errors)
        forms.save()
        self.assertEqual(self.problem.cases.get(type='S').batch_type, 'min')
        self.assertEqual([case['points'] for case in self.compile()['test_cases'][0]['batched']], [1, 2])

    def test_negative_weights_and_zero_total_are_rejected(self):
        self.batch([1, 1])
        for weights in ((-1, 1), (0, 0)):
            with self.subTest(weights=weights):
                values = self.form_values()
                for index, weight in enumerate(weights, 1):
                    values['cases-%s-points' % index] = str(weight)
                self.assertFalse(self.bound_forms(values).is_valid())
        self.problem.cases.filter(type='C').update(points=0)
        with self.assertRaises(ProblemDataError):
            self.compile()

    def test_zero_weight_total_is_rejected_even_when_batch_maximum_is_zero(self):
        start = self.batch([0, 0], maximum=0)
        for mode in ('sum', 'min'):
            with self.subTest(mode=mode):
                start.batch_type = mode
                start.save(update_fields=['batch_type'])
                forms = self.bound_forms(self.form_values())
                self.assertFalse(forms.is_valid())
                self.assertIn('points', forms.forms[0].errors)
                with self.assertRaisesMessage(ProblemDataError, 'at least one positive test weight'):
                    self.compile()

    def test_zero_weights_are_allowed_when_another_test_has_positive_weight(self):
        start = self.batch([0, 1], maximum=0)
        for mode in ('sum', 'min'):
            with self.subTest(mode=mode):
                start.batch_type = mode
                start.save(update_fields=['batch_type'])
                forms = self.bound_forms(self.form_values())
                self.assertTrue(forms.is_valid(), forms.errors)
                self.assertEqual([case['points'] for case in self.compile()['test_cases'][0]['batched']], [0, 1])

    @patch('judge.bridge.judge_handler.finished_submission')
    @patch('judge.bridge.judge_handler.event.post')
    def test_user_example_scores_42_through_bridge(self, post, finished):
        self.batch([1] * 5 + [2] * 5)
        sub = self.score_packets([1, 0, 0, 0, 0, 1, 1, 1, 0, 0])
        self.assertEqual((sub.case_points, sub.case_total, sub.points), (42, 90, 42))
        self.assertEqual(sub.batch_results.get().scoring_type, 'sum')

    @patch('judge.bridge.judge_handler.finished_submission')
    @patch('judge.bridge.judge_handler.event.post')
    def test_both_production_checker_modes_use_the_same_weighted_ratio(self, post, finished):
        self.batch([1] * 5 + [2] * 5)
        for percentage in (True, False):
            with self.subTest(percentage=percentage):
                self.assertIs(self.compile(percentage)['checker']['args']['treat_checker_points_as_percentage'], percentage)
                # Production-judge contract: it converts 50% or 3/6 points to
                # the same earned points before sending the existing packet.
                sub = self.score_packets([0.5, 0, 0, 0, 0, 1, 1, 1, 0, 0])
                self.assertEqual((sub.case_points, sub.case_total, sub.points), (39, 90, 39))
                self.assertEqual(sub.test_cases.get(case=1).points, 3)

    @patch('judge.bridge.judge_handler.finished_submission')
    @patch('judge.bridge.judge_handler.event.post')
    def test_partial_checker_ratios_work_in_both_batch_modes(self, post, finished):
        self.batch([1] * 5 + [2] * 5)
        for mode in ('sum', 'min'):
            self.problem.cases.filter(type='S').update(batch_type=mode)
            for percentage in (True, False):
                with self.subTest(mode=mode, percentage=percentage):
                    self.assertIs(self.compile(percentage)['checker']['args']['treat_checker_points_as_percentage'], percentage)
                    sub = self.score_packets([0.5] * 10)
                    self.assertEqual((sub.case_points, sub.case_total, sub.points), (45, 90, 45))
                    self.assertEqual(sub.batch_results.get().scoring_type, mode)

    def score_packets(self, ratios):
        sub = Submission.objects.create(problem=self.problem, user=self.users['normal'].profile,
            language=self.language, status='G', batch_scoring=scoring_snapshot(self.problem))
        batch = self.compile()['test_cases'][0]
        weights = [case['points'] for case in batch['batched']]
        cases = []
        for index, (weight, ratio) in enumerate(zip(weights, ratios), 1):
            maximum = batch['points'] * weight / sum(weights)
            cases.append({'position': index, 'status': 0 if ratio else 1,
                          'points': maximum * ratio, 'total-points': maximum,
                          'time': 0.01, 'memory': 1024, 'output': ''})
        handler = object.__new__(JudgeHandler)
        handler.name = 'production-contract'
        handler._working = sub.pk
        handler._pending_usage = None
        handler.in_batch = True
        handler.batch_id = 1
        handler.update_counter = {}
        handler._free_self = Mock()
        handler._make_json_log = Mock(return_value={})
        handler._post_update_submission = Mock()
        handler.on_test_case({'submission-id': sub.pk, 'cases': cases})
        handler.on_grading_end({'submission-id': sub.pk})
        sub.refresh_from_db()
        return sub

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_editor_renders_weight_controls_and_sum_template(self):
        self.batch([None, 2])
        self.client.force_login(self.users['superuser'])
        self.client.cookies['django_language'] = 'vi'
        with override('vi'):
            response = self.client.get(reverse('problem_data', args=[self.problem.code]), HTTP_ACCEPT_LANGUAGE='vi')
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'batch-weight-label')
            self.assertContains(response, 'value="sum" selected')
            self.assertContains(response, gettext('Points / Weight'))

    def editor_values(self):
        self.client.force_login(self.users['superuser'])
        url = reverse('problem_data', args=[self.problem.code])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        context = response.context_data
        values = self.form_values()
        from django.forms import FileField
        for name in ('mirror_form', 'external_form', 'data_form'):
            for field in context[name]:
                if isinstance(field.field, FileField):
                    continue
                value = field.value()
                if value is None and getattr(field.field, 'choices', None):
                    value = next(iter(field.field.choices))[0]
                values[field.html_name] = 'on' if value is True else '' if value is None or value is False else value
        return url, values

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_http_weight_edit_regenerates_the_dataset(self):
        self.batch([1, 1])
        url, values = self.editor_values()
        values['cases-1-points'] = '2'
        with patch('judge.utils.problem_data.ProblemDataCompiler.generate') as generate, patch(
                'judge.models.problem_data.ProblemData.has_yml', return_value=True):
            type(self.problem).objects.filter(pk=self.problem.pk).update(is_locked=False)
            response = self.client.post(url, values)
        self.assertEqual(response.status_code, 302, getattr(response, 'context_data', {}))
        generate.assert_called_once()
        self.assertEqual(list(self.problem.cases.filter(type='C').order_by('order').values_list('points', flat=True)), [2, 1])

    @override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver'])
    def test_http_mode_edit_with_default_weights_remains_db_only(self):
        self.batch([None, None], mode='min')
        url, values = self.editor_values()
        values['cases-0-batch_type'] = 'sum'
        values['cases-1-points'] = values['cases-2-points'] = '1'
        with patch('judge.utils.problem_data.ProblemDataCompiler.generate') as generate:
            type(self.problem).objects.filter(pk=self.problem.pk).update(is_locked=False)
            response = self.client.post(url, values)
        self.assertEqual(response.status_code, 302, getattr(response, 'context_data', {}))
        generate.assert_not_called()
        self.assertEqual(self.problem.cases.get(type='S').batch_type, 'sum')
