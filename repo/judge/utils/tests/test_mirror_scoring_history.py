from copy import deepcopy
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase

from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import JudgeList
from judge.contest_format.ioi import IOIContestFormat
from judge.models import (ContestSubmission, Language, ProblemData, ProblemTestCase, Submission,
                          SubmissionBatchResult, SubmissionSource, SubmissionTestCase)
from judge.models.tests.util import (CommonDataMixin, create_contest, create_contest_participation,
                                     create_contest_problem, create_problem)
from judge.tasks.submission import rescore_problem
from judge.utils.batch_scoring import display_batch_score, scoring_snapshot


class MirrorScoringHistoryTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.root = create_problem(code='historyroot', partial=True, points=100)
        for batch, mode in enumerate(('min', 'sum')):
            order = batch * 4
            ProblemTestCase.objects.create(dataset=cls.root, order=order, type='S', points=50,
                                          batch_type=mode, is_pretest=False)
            for offset in (1, 2):
                ProblemTestCase.objects.create(dataset=cls.root, order=order + offset, type='C',
                    input_file='%s.in' % (order + offset), output_file='%s.out' % (order + offset), is_pretest=False)
            ProblemTestCase.objects.create(dataset=cls.root, order=order + 3, type='E', is_pretest=False)
        cls.mirror = create_problem(code='historymirror', mirror_of=cls.root, partial=True, points=100)
        ProblemData.objects.create(problem=cls.mirror, legacy_batch_type='sum',
                                   batch_scoring_overrides={'full:1': 'sum'})
        cls.language = Language.get_python3()
        cls.contest = create_contest(key='mirrorhistory')
        cls.cp = create_contest_problem(contest=cls.contest, problem=cls.mirror, partial=True, points=200)
        cls.part = create_contest_participation(contest=cls.contest, user='normal')

    def setUp(self):
        cache.clear()

    def historical_submission(self, legacy=True):
        snapshot = None
        if not legacy:
            snapshot = scoring_snapshot(self.mirror)
            snapshot['full'][0]['mode'] = 'sum'
            snapshot['overrides'] = {'full:1': 'sum'}
            snapshot['dispatch_id'] = 'historical-run'
        sub = Submission.objects.create(problem=self.mirror, user=self.users['normal'].profile,
            language=self.language, status='D', result='WA', points=75, case_points=75, case_total=100,
            batch_scoring=snapshot)
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        cs = ContestSubmission.objects.create(submission=sub, problem=self.cp, participation=self.part, points=150)
        self.raw_cases(sub)
        if not legacy:
            for batch, points in ((1, 25), (2, 50)):
                SubmissionBatchResult.objects.create(submission=sub, batch=batch, points=points,
                                                     total=50, scoring_type='sum')
        return sub, cs

    def raw_cases(self, sub):
        for n, points in ((1, 25), (2, 0), (3, 25), (4, 25)):
            SubmissionTestCase.objects.create(submission=sub, case=n, batch=1 if n <= 2 else 2,
                points=points, total=25, status='AC' if points else 'WA', time=0.1, memory=1024)

    def enable_pretests(self):
        for order, kind in enumerate(('S', 'C', 'C', 'E'), 8):
            ProblemTestCase.objects.create(dataset=self.root, order=order, type=kind,
                input_file='%s.in' % order if kind == 'C' else '',
                output_file='%s.out' % order if kind == 'C' else '',
                points=20 if kind == 'S' else None, batch_type='min', is_pretest=True)
        self.contest.run_pretests_only = True
        self.contest.save(update_fields=['run_pretests_only'])
        self.cp.is_pretested = True
        self.cp.save(update_fields=['is_pretested'])
        ProblemData.objects.filter(problem=self.mirror).update(batch_scoring_overrides={'pretests:1': 'sum'})

    def requeue(self, sub, batch=False, force_full=False):
        def acknowledge(packet):
            return {'name': 'submission-received', 'submission-id': packet['submission-id']}

        with patch('judge.judgeapi.judge_request', side_effect=acknowledge) as request, self.captureOnCommitCallbacks(execute=True):
            sub.judge(rejudge=True, batch_rejudge=batch, force_full=force_full,
                      rejudge_user=self.users['superuser'])
        sub.refresh_from_db()
        self.assertEqual(request.call_args.args[0]['problem-id'], self.root.code)
        self.assertEqual(sub.status, 'QU')
        self.assertIsNotNone(sub.rejudged_date)
        self.assertNotIn('full', sub.batch_scoring)
        self.assertFalse(sub.test_cases.exists())
        self.assertFalse(sub.batch_results.exists())
        return sub.batch_scoring['dispatch_id']

    def dispatch(self, sub):
        handler = object.__new__(JudgeHandler)
        handler.name = 'mirror-history-judge'
        handler.judges = JudgeList()
        handler.send = Mock()
        handler._free_self = Mock()
        handler._post_update_submission = Mock()
        handler._make_json_log = Mock(return_value='{}')
        with patch('judge.bridge.judge_handler._ensure_connection'), patch('judge.bridge.judge_handler.threading.Timer'):
            handler.submit(sub.pk, self.root.code, self.language.key, sub.source.source)
        sub.refresh_from_db()
        packet = handler.send.call_args.args[0]
        self.assertEqual(packet['problem-id'], self.root.code)
        self.assertEqual(packet['meta']['dispatch-id'], sub.batch_scoring['dispatch_id'])
        self.assertEqual([row['mode'] for row in packet['meta']['batch-scoring']['full']], ['min', 'sum'])
        self.assertEqual(sub.batch_scoring['overrides'], {})
        return handler

    def complete(self, sub, handler):
        token = sub.batch_scoring['dispatch_id']
        handler.on_grading_begin({'submission-id': sub.pk, 'dispatch-id': token, 'pretested': False})
        self.raw_cases(sub)
        with patch('judge.bridge.judge_handler.finished_submission'), self.captureOnCommitCallbacks(execute=True):
            handler.on_grading_end({'submission-id': sub.pk, 'dispatch-id': token})
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.result, sub.points, sub.case_points), ('D', 'WA', 50, 50))
        self.assertEqual(list(sub.batch_results.order_by('batch').values_list('points', 'scoring_type')),
                         [(0, 'min'), (50, 'sum')])

    def test_legacy_read_and_rescore_do_not_adopt_current_root_modes(self):
        sub, cs = self.historical_submission()
        cases = list(sub.test_cases.filter(batch=1))
        self.assertEqual(display_batch_score(sub, 1, cases), (25, 50, 'sum'))
        self.root.cases.filter(type='S').update(batch_type='min')
        self.assertEqual(display_batch_score(sub, 1, cases), (25, 50, 'sum'))
        self.assertEqual([row[4] for row in IOIContestFormat(self.contest, {}).get_results_by_subtask(self.part)], [50, 100])
        with patch('judge.tasks.submission.Progress'), patch('judge.models.Profile.calculate_points'):
            rescore_problem.run(self.mirror.pk)
        sub.refresh_from_db()
        cs.refresh_from_db()
        self.assertEqual((sub.points, cs.points), (75, 150))
        self.assertIsNone(sub.batch_scoring)
        self.assertFalse(sub.batch_results.exists())

    def test_saved_old_mirror_policy_and_results_stay_unchanged(self):
        sub, _ = self.historical_submission(legacy=False)
        snapshot = deepcopy(sub.batch_scoring)
        self.root.cases.filter(type='S').update(batch_type='min')
        self.assertEqual(display_batch_score(sub, 1, list(sub.test_cases.filter(batch=1))), (25, 50, 'sum'))
        with patch('judge.tasks.submission.Progress'), patch('judge.models.Profile.calculate_points'):
            rescore_problem.run(self.mirror.pk)
        sub.refresh_from_db()
        self.assertEqual((sub.points, sub.batch_scoring), (75, snapshot))
        self.assertEqual(list(sub.batch_results.order_by('batch').values_list('points', 'scoring_type')),
                         [(25, 'sum'), (50, 'sum')])

    def test_legacy_and_snapshotted_rejudge_replace_old_policy_with_root(self):
        from reversion.models import Version

        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                sub, cs = self.historical_submission(legacy=legacy)
                token = self.requeue(sub, batch=not legacy)
                self.assertNotEqual(token, 'historical-run')
                history = Version.objects.get_for_object(sub).first().field_dict
                self.assertEqual(history['points'], 75)
                self.assertEqual(history['batch_scoring'] is None, legacy)
                handler = self.dispatch(sub)
                self.complete(sub, handler)
                cs.refresh_from_db()
                self.assertEqual(cs.points, 100)
                data = ProblemData.objects.get(problem=self.mirror)
                self.assertEqual((data.legacy_batch_type, data.batch_scoring_overrides), ('sum', {'full:1': 'sum'}))

    def test_root_mode_is_fixed_for_dispatched_rejudge(self):
        sub, _ = self.historical_submission(legacy=False)
        self.requeue(sub)
        handler = self.dispatch(sub)
        self.root.cases.filter(type='S').update(batch_type='sum')
        self.complete(sub, handler)
        self.mirror.refresh_from_db()
        self.assertEqual(scoring_snapshot(self.mirror)['full'][0]['mode'], 'sum')
        self.assertEqual(sub.batch_scoring['full'][0]['mode'], 'min')

    def test_old_run_callback_cannot_finish_rejudge(self):
        sub, _ = self.historical_submission(legacy=False)
        self.requeue(sub)
        handler = self.dispatch(sub)
        handler.on_grading_end({'submission-id': sub.pk, 'dispatch-id': 'historical-run'})
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')
        self.assertFalse(sub.batch_results.exists())
        self.complete(sub, handler)

    def test_root_unlock_rejects_rejudge_without_erasing_history(self):
        sub, cs = self.historical_submission(legacy=False)
        snapshot = deepcopy(sub.batch_scoring)
        type(self.root).objects.filter(pk=self.root.pk).update(is_locked=False)
        with patch('judge.judgeapi.judge_request') as request, self.assertRaises(ValidationError):
            sub.judge(rejudge=True)
        request.assert_not_called()
        sub.refresh_from_db()
        cs.refresh_from_db()
        self.assertEqual((sub.status, sub.points, cs.points), ('D', 75, 150))
        self.assertEqual(sub.batch_scoring, snapshot)
        self.assertEqual((sub.test_cases.count(), sub.batch_results.count()), (4, 2))

    def test_pretest_rejudge_inherits_root_pretest_mode(self):
        self.enable_pretests()
        sub, _ = self.historical_submission()
        self.requeue(sub)
        self.assertTrue(sub.is_pretested)
        handler = self.dispatch(sub)
        self.assertEqual(sub.batch_scoring['pretests'][0]['mode'], 'min')
        token = sub.batch_scoring['dispatch_id']
        handler.on_grading_begin({'submission-id': sub.pk, 'dispatch-id': token, 'pretested': True})
        for n, points in ((1, 10), (2, 0)):
            SubmissionTestCase.objects.create(submission=sub, case=n, batch=1, points=points, total=10,
                status='AC' if points else 'WA', time=0.1, memory=1024)
        with patch('judge.bridge.judge_handler.finished_submission'), self.captureOnCommitCallbacks(execute=True):
            handler.on_grading_end({'submission-id': sub.pk, 'dispatch-id': token})
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.points, sub.is_pretested), ('D', 0, True))
        self.assertEqual(sub.batch_results.get().scoring_type, 'min')

    def test_force_full_rejudge_uses_root_full_policy_in_pretest_contest(self):
        self.enable_pretests()
        sub, _ = self.historical_submission()
        self.requeue(sub, force_full=True)
        self.assertFalse(sub.is_pretested)
        handler = self.dispatch(sub)
        self.complete(sub, handler)
