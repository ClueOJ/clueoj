from django.test import TestCase
from django.urls import reverse

from judge.models import Language, ProblemData, ProblemTestCase, Submission, SubmissionSource, SubmissionTestCase
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.utils.batch_scoring import finalize_batch_results, scaled_score, scoring_snapshot
from judge.views.submission import group_test_cases


class CaseDisplayTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='caseweightdisplay', is_public=True, partial=True, points=100)
        cls.language = Language.get_python3()

    def add_batch(self, weights, maximum=30, mode='min', pretest=False):
        order = self.problem.cases.count()
        marker = ProblemTestCase.objects.create(dataset=self.problem, type='S', order=order,
                                                points=maximum, batch_type=mode, is_pretest=pretest)
        for offset, weight in enumerate(weights, 1):
            ProblemTestCase.objects.create(dataset=self.problem, type='C', order=order + offset,
                points=weight, input_file='%s.in' % offset, output_file='%s.out' % offset, is_pretest=pretest)
        ProblemTestCase.objects.create(dataset=self.problem, type='E', order=order + len(weights) + 1,
                                        is_pretest=pretest)
        return marker

    def submission(self, cases, pretested=False):
        sub = Submission.objects.create(problem=self.problem, language=self.language,
            user=self.users['normal'].profile, status='D', result='AC', is_pretested=pretested,
            time=0.06, memory=1024, batch_scoring=scoring_snapshot(self.problem))
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        for position, entry in enumerate(cases, 1):
            batch, points, total, *status = entry
            SubmissionTestCase.objects.create(submission=sub, case=position, batch=batch, points=points,
                total=total, status=status[0] if status else 'AC', time=0.01, memory=1024)
        sub.case_points, sub.case_total = finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        sub.points = scaled_score(sub.case_points, sub.case_total, self.problem.points)
        sub.save()
        return sub

    def assert_page_and_ajax(self, sub, expected, count=None):
        self.client.force_login(self.users['normal'])
        for response in (self.client.get(reverse('submission_status', args=[sub.pk]), HTTP_ACCEPT_LANGUAGE='en'),
                         self.client.get(reverse('submission_testcases_query'), {'id': sub.pk},
                                         HTTP_ACCEPT_LANGUAGE='en')):
            self.assertContains(response, expected, count=count)

    def test_min_normalizes_to_one_without_changing_scores(self):
        self.add_batch([1] * 6)
        sub = self.submission([(1, 5, 5)] * 6)
        self.assert_page_and_ajax(sub, '(1/1)', count=6)
        self.assertEqual(list(sub.test_cases.values_list('points', 'total')), [(5, 5)] * 6)
        self.assertEqual((sub.case_points, sub.case_total, sub.points), (30, 30, 100))
        self.assertEqual(list(sub.batch_results.values_list('points', 'total')), [(30, 30)])

    def test_mixed_min_sum_standalone_and_partial_cases(self):
        self.add_batch([1, 2, 3], mode='min')
        self.add_batch([1, 2, 3], mode='sum')
        ProblemTestCase.objects.create(dataset=self.problem, type='C', order=self.problem.cases.count(),
                                        points=7, is_pretest=False)
        sub = self.submission([(1, 5, 5), (1, 5, 10), (1, 15, 15),
                               (2, 5, 5), (2, 5, 10), (2, 15, 15), (None, 7, 7)])
        self.assert_page_and_ajax(sub, '(1/1)', count=2)
        for expected in ('(0.500/1)', '(5/5)', '(5/10)', '(15/15)', '(7/7)'):
            self.assert_page_and_ajax(sub, expected, count=1)
        self.assertEqual((sub.case_points, sub.case_total), (47, 67))

    def test_saved_mode_survives_later_test_edits(self):
        for mode, expected in (('min', '(1/1)'), ('sum', '(5/5)')):
            with self.subTest(mode=mode):
                self.problem.cases.all().delete()
                marker = self.add_batch([1] * 6, mode=mode)
                sub = self.submission([(1, 5, 5)] * 6)
                self.problem.cases.filter(pk=marker.pk).update(batch_type='sum' if mode == 'min' else 'min')
                self.problem.cases.filter(type='C').update(points=2)
                self.assert_page_and_ajax(sub, expected, count=6)

    def test_existing_results_need_no_weight_metadata_or_rejudge(self):
        self.add_batch([2] * 6)
        sub = self.submission([(1, 5, 5)] * 6)
        self.assertNotIn('case_weights', sub.batch_scoring)
        self.assert_page_and_ajax(sub, '(1/1)', count=6)
        sub.batch_scoring = None
        sub.save(update_fields=['batch_scoring'])
        ProblemData.objects.create(problem=self.problem, legacy_batch_type='min')
        self.assert_page_and_ajax(sub, '(1/1)', count=6)

    def test_manual_dataset_normalizes_without_weight_metadata(self):
        self.add_batch([2] * 6)
        self.problem.is_manually_managed = True
        self.problem.save()
        ProblemData.objects.create(problem=self.problem, legacy_batch_type='min')
        sub = self.submission([(1, 5, 5)] * 6)
        self.assert_page_and_ajax(sub, '(1/1)', count=6)

    def test_pretests_and_full_fallback_use_the_saved_mode(self):
        self.add_batch([1, 1], mode='sum')
        self.add_batch([1, 1], mode='min', pretest=True)
        sub = self.submission([(1, 15, 15)] * 2, pretested=True)
        self.assert_page_and_ajax(sub, '(1/1)', count=2)
        self.problem.cases.filter(is_pretest=True).delete()
        fallback = self.submission([(1, 15, 15)] * 2, pretested=True)
        self.assert_page_and_ajax(fallback, '(15/15)', count=2)

    def test_live_failed_and_skipped_cases_keep_status_and_batch_totals(self):
        self.add_batch([1, 1, 1])
        sub = self.submission([(1, 0, 10, 'TLE'), (1, 0, 10, 'SC'), (1, 0, 10, 'SC')])
        sub.status = 'G'
        sub.save(update_fields=['status'])
        self.assert_page_and_ajax(sub, '(0/1)', count=3)
        batch = group_test_cases(sub, set(), self.problem)[0][0]
        self.assertEqual((batch['points'], batch['total']), (0, 30))
        self.assertEqual([case.status for case in batch['cases']], ['TLE', 'SC', 'SC'])

    def test_zero_maximum_keeps_original_display(self):
        self.add_batch([0, 1])
        sub = self.submission([(1, 0, 0), (1, 30, 30)])
        self.assert_page_and_ajax(sub, '(0/0)', count=1)
        self.assert_page_and_ajax(sub, '(1/1)', count=1)
        self.problem.cases.all().delete()
        self.add_batch([1, 1], maximum=0)
        zero = self.submission([(1, 0, 0), (1, 0, 0)])
        self.assert_page_and_ajax(zero, '(0/0)', count=2)

    def test_batch_only_visibility_does_not_expose_case_rows(self):
        self.add_batch([1] * 6)
        sub = self.submission([(1, 5, 5)] * 6)
        self.problem.testcase_result_visibility_mode = 'B'
        self.problem.save(update_fields=['testcase_result_visibility_mode'])
        self.client.force_login(self.users['normal'])
        for response in (self.client.get(reverse('submission_status', args=[sub.pk])),
                         self.client.get(reverse('submission_testcases_query'), {'id': sub.pk})):
            self.assertNotContains(response, '(1/1)')
