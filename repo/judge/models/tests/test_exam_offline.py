from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import RequestFactory, TestCase
from django.urls import resolve
from django.utils import timezone

from judge.models import (ExamOfflineAttempt, ExamOfflineSubmission, ExamTag, ExamTagProblemPoint,
                          Language, Profile, Submission, SubmissionSource)
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, create_problem
from judge.utils.exam_offline import (admit_submission, finish_attempt, link_submission,
                                     locked_contest_transition, result_rows, reveal_attempt, start_attempt)
from judge.views.exam_offline import guard


class OfflineExamTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.profile = cls.users['normal'].profile
        cls.language = Language.objects.filter(key__startswith='CPP').first()
        cls.problem = create_problem(code='offlineproblem', is_public=True, partial=True)
        cls.exam = ExamTag.objects.create(slug='offline-exam', name='Offline exam', duration_minutes=180,
                                         virtual_offline_enabled=True, day_count=1)
        cls.problem.exam_tags.add(cls.exam)
        ExamTagProblemPoint.objects.filter(exam_tag=cls.exam, problem=cls.problem).update(points=10)

    def start(self):
        return start_attempt(self.profile.pk, self.exam.pk)

    def submit(self, attempt, status='D', result='AC', points=10):
        with transaction.atomic():
            profile = Profile.objects.select_for_update().get(pk=self.profile.pk)
            row = admit_submission(profile, self.problem, attempt.id)
            sub = Submission.objects.create(user=profile, problem=self.problem, language=self.language,
                                            status=status, result=result, case_points=points,
                                            case_total=10, points=points, offline_hidden=True)
            SubmissionSource.objects.create(submission=sub, source='int main() {}')
            link_submission(row, sub)
        return sub

    def test_config_validation(self):
        self.exam.duration_minutes = None
        with self.assertRaises(ValidationError):
            self.exam.full_clean()

    def test_snapshot_and_only_one_active(self):
        attempt = self.start()
        with self.assertRaises(ValidationError):
            self.start()
        self.exam.duration_minutes = 1
        self.exam.save()
        ExamTagProblemPoint.objects.filter(exam_tag=self.exam).update(points=99)
        self.assertEqual(attempt.deadline - attempt.started_at, timedelta(minutes=180))
        self.assertEqual(attempt.problems.get().points, 10)

    def test_must_leave_contest(self):
        contest = create_contest(key='offline_conflict', is_visible=True)
        participation = create_contest_participation(contest=contest.key, user='normal')
        Profile.objects.filter(pk=self.profile.pk).update(current_contest=participation)
        with self.assertRaises(ValidationError):
            self.start()

    def test_contest_admission_uses_shared_guard(self):
        self.start()
        request = RequestFactory().post('/contest/test/join')
        request.profile = self.profile
        request.user = self.users['normal']
        called = []

        @locked_contest_transition
        def join(view, request):
            called.append(True)

        with patch('judge.utils.views.generic_message', return_value='blocked'):
            self.assertEqual(join(None, request), 'blocked')
        self.assertFalse(called)

    def test_last_ce_counts_and_retake_is_independent(self):
        attempt = self.start()
        first = self.submit(attempt)
        last = self.submit(attempt, status='CE', result='CE', points=0)
        self.assertFalse(Submission.visible.filter(pk__in=[first.pk, last.pk]).exists())
        self.assertFalse(last.can_see_detail(self.users['normal']))
        attempt = finish_attempt(self.profile.pk, attempt.pk, early=True)
        attempt = reveal_attempt(self.profile.pk, attempt.pk)
        rows, score, pending = result_rows(attempt)
        self.assertEqual(rows[0].final_submission_id, last.id)
        self.assertEqual(score, 0)
        self.assertFalse(pending)
        self.assertEqual(Submission.visible.filter(pk__in=[first.pk, last.pk]).count(), 2)
        again = self.start()
        self.assertNotEqual(attempt.pk, again.pk)
        self.assertIsNone(again.problems.get().final_submission_id)

    def test_pending_final_does_not_fall_back(self):
        attempt = self.start()
        self.submit(attempt)
        final = self.submit(attempt, status='QU', result=None, points=0)
        attempt = finish_attempt(self.profile.pk, attempt.pk, early=True)
        attempt = reveal_attempt(self.profile.pk, attempt.pk)
        self.assertTrue(result_rows(attempt)[2])
        final.status = 'D'
        final.result = 'WA'
        final.case_points = 3
        final.save()
        final.refresh_from_db()
        self.assertFalse(final.offline_hidden, 'Stale judge save must not undo publication')
        self.assertEqual(result_rows(attempt)[1], 3)

    def test_expiry_and_stale_form(self):
        attempt = self.start()
        ExamOfflineAttempt.objects.filter(pk=attempt.pk).update(deadline=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            admit_submission(self.profile, self.problem, attempt.id)
        attempt = finish_attempt(self.profile.pk, attempt.pk)
        self.assertEqual(attempt.ended_at, attempt.deadline)
        self.assertEqual(attempt.end_reason, 'timeout')
        with self.assertRaises(ValidationError):
            admit_submission(self.profile, self.problem, attempt.id)
        repeated = finish_attempt(self.profile.pk, attempt.pk, early=True)
        self.assertEqual(repeated.ended_at, attempt.ended_at)

    def test_acceptance_at_deadline_rolls_back(self):
        attempt = self.start()
        with self.assertRaises(ValidationError), transaction.atomic():
            row = attempt.problems.get()
            sub = Submission.objects.create(user=self.profile, problem=self.problem, language=self.language)
            sub.date = attempt.deadline
            link_submission(row, sub)
        self.assertFalse(ExamOfflineSubmission.objects.exists())

    def test_blind_routes_and_own_source(self):
        attempt = self.start()
        sub = self.submit(attempt)
        for path in ['/problem/offlineproblem/rank/', '/problem/offlineproblem/editorial',
                     '/api/v2/submissions', '/exams/offline-exam/',
                     '/user/normal']:
            request = RequestFactory().get(path)
            request.user = self.users['normal']
            request.profile = self.profile
            request.offline_attempt = attempt
            with patch('judge.views.exam_offline.generic_message', return_value='blocked'):
                self.assertEqual(guard(request, resolve(path)), 'blocked', path)
        request.path = '/exam-offline/source/%s' % sub.id
        self.assertIsNone(guard(request, resolve(request.path)))

    def test_realtime_does_not_publish_blind_results(self):
        from judge import event_poster
        attempt = self.start()
        sub = self.submit(attempt)
        with patch('judge.event_poster._transport_post') as transport:
            event_poster.post('sub_' + sub.id_secret, {'type': 'compile-message'})
            event_poster.post('submissions', {'id': sub.id, 'status': 'D'})
            transport.assert_not_called()
            finish_attempt(self.profile.pk, attempt.pk, early=True)
            reveal_attempt(self.profile.pk, attempt.pk)
            event_poster.post('sub_' + sub.id_secret, {'type': 'grading-end'})
            transport.assert_called_once()

    def test_progress_excludes_hidden_submission(self):
        from judge.tasks.exams import _sync_user_exam_progress
        from judge.models import ExamUserProgress
        attempt = self.start()
        self.submit(attempt)
        _sync_user_exam_progress(self.profile.pk, self.exam.pk)
        self.assertEqual(ExamUserProgress.objects.get(user=self.profile, exam_tag=self.exam).earned_points, 0)
        finish_attempt(self.profile.pk, attempt.pk, early=True)
        reveal_attempt(self.profile.pk, attempt.pk)
        _sync_user_exam_progress(self.profile.pk, self.exam.pk)
        self.assertEqual(ExamUserProgress.objects.get(user=self.profile, exam_tag=self.exam).earned_points, 10)

    def test_actual_pages_and_other_user_cannot_read_hidden_submission(self):
        from django.test import override_settings
        with override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
            self.client.force_login(self.users['normal'])
            attempt = self.start()
            sub = self.submit(attempt)
            response = self.client.get('/exam-offline/%s/' % attempt.id)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'Lần nộp được tính')
            self.assertNotContains(response, 'Accepted')
            problem_response = self.client.get('/problem/offlineproblem')
            self.assertEqual(problem_response.status_code, 200)
            self.assertTemplateUsed(problem_response, 'problem/problem.html')
            self.assertEqual(self.client.get('/problem/offlineproblem/submit').status_code, 200)
            self.assertEqual(self.client.get('/problem/offlineproblem/rank/').status_code, 403)
            self.assertEqual(self.client.get('/exam-offline/source/%s' % sub.id).status_code, 200)
            self.client.force_login(self.users['superuser'])
            self.assertEqual(self.client.get('/submission/%s' % sub.id).status_code, 200)
            self.assertEqual(self.client.get('/api/v2/submission/%s' % sub.id).status_code, 200)
            self.assertEqual(self.client.get('/exam-offline/source/%s' % sub.id).status_code, 404)
            self.client.force_login(self.users['normal'])
            response = self.client.post('/exam-offline/%s/finish' % attempt.id)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.client.get('/exam-offline/%s/' % attempt.id).status_code, 200)
            self.assertEqual(self.client.get('/exams/offline-exam/offline/history').status_code, 200)


    def test_finished_unrevealed_ac_does_not_mark_problem_solved(self):
        from django.core.cache import cache
        from judge.utils.problems import user_completed_ids
        attempt = self.start()
        sub = self.submit(attempt)
        cache.delete('user_complete:%d' % self.profile.pk)
        self.assertNotIn(self.problem.pk, user_completed_ids(self.profile))
        attempt = finish_attempt(self.profile.pk, attempt.pk, early=True)
        sub.refresh_from_db()
        self.assertTrue(sub.offline_hidden)
        self.assertIsNone(attempt.active_user_id)
        self.assertNotIn(self.problem.pk, user_completed_ids(self.profile))
        self.assertIsNone(result_rows(attempt)[0][0].earned)
        with self.captureOnCommitCallbacks(execute=True), patch('judge.tasks.exam_offline.refresh_offline_progress.delay'):
            attempt = reveal_attempt(self.profile.pk, attempt.pk)
        self.assertIn(self.problem.pk, user_completed_ids(self.profile))
        self.assertEqual(reveal_attempt(self.profile.pk, attempt.pk).revealed_at, attempt.revealed_at)

    def test_practice_ac_is_visible_without_revealing_original(self):
        from django.core.cache import cache
        from judge.utils.problems import user_completed_ids
        attempt = self.start()
        original = self.submit(attempt)
        finish_attempt(self.profile.pk, attempt.pk, early=True)
        Submission.objects.create(user=self.profile, problem=self.problem, language=self.language,
                                  status='D', result='AC', case_points=10, case_total=10)
        cache.delete('user_complete:%d' % self.profile.pk)
        self.assertIn(self.problem.pk, user_completed_ids(self.profile))
        original.refresh_from_db()
        self.assertTrue(original.offline_hidden)

    def test_reveal_requires_finished_owned_session(self):
        attempt = self.start()
        with self.assertRaises(ValidationError):
            reveal_attempt(self.profile.pk, attempt.pk)
        with self.assertRaises(ExamOfflineAttempt.DoesNotExist):
            reveal_attempt(self.users['superuser'].profile.pk, attempt.pk)

    def test_days_are_independent_attempts_and_reveals(self):
        self.exam.day_count = 2
        self.exam.save()
        second_problem = create_problem(code='offlinedaytwo', is_public=True, partial=True)
        second_problem.exam_tags.add(self.exam)
        ExamTagProblemPoint.objects.filter(exam_tag=self.exam, problem=second_problem).update(day_number=2)
        first = start_attempt(self.profile.pk, self.exam.pk, 1)
        original = self.submit(first)
        finish_attempt(self.profile.pk, first.pk, early=True)
        second = start_attempt(self.profile.pk, self.exam.pk, 2)
        self.assertEqual(list(second.problems.values_list('problem_id', flat=True)), [second_problem.id])
        self.assertEqual(second.day_number, 2)
        self.assertEqual(list(first.problems.values_list('problem_id', flat=True)), [self.problem.id])
        finish_attempt(self.profile.pk, second.pk, early=True)
        reveal_attempt(self.profile.pk, second.pk)
        original.refresh_from_db()
        self.assertTrue(original.offline_hidden)
        first.refresh_from_db()
        self.assertIsNone(first.revealed_at)
        self.assertEqual(start_attempt(self.profile.pk, self.exam.pk, 2).day_number, 2)

    def test_invalid_and_empty_day_rejected(self):
        for day in [0, -1, 2, 'bad', None]:
            with self.assertRaises(ValidationError):
                start_attempt(self.profile.pk, self.exam.pk, day)
        self.exam.day_count = 2
        self.exam.save()
        with self.assertRaises(ValidationError):
            start_attempt(self.profile.pk, self.exam.pk, 2)
        row = self.exam.problem_points.get()
        row.day_number = 3
        with self.assertRaises(ValidationError):
            row.full_clean()


    def test_zero_days_includes_all_problems_and_hides_day_controls(self):
        from django.test import override_settings
        self.exam.day_count = 0
        self.exam.save()
        other = create_problem(code='unsplitother', is_public=True)
        other.exam_tags.add(self.exam)
        ExamTagProblemPoint.objects.filter(exam_tag=self.exam, problem=other).update(day_number=2)
        attempt = start_attempt(self.profile.pk, self.exam.pk, 99)
        self.assertEqual(attempt.day_number, 0)
        self.assertEqual(attempt.problems.count(), 2)
        with override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
            self.client.force_login(self.users['normal'])
            response = self.client.get('/exam-offline/%s/' % attempt.id)
            self.assertNotContains(response, 'Ngày 0')
            finish_attempt(self.profile.pk, attempt.pk, early=True)
            response = self.client.get('/exams/offline-exam/offline/history')
            self.assertNotContains(response, '<th>Ngày thi</th>')
        self.assertEqual(ExamTag().day_count, 0)


    def test_staff_can_access_admin_during_offline_attempt(self):
        request = RequestFactory().get('/admin/')
        request.offline_attempt = self.start()
        request.user = self.users['staff_problem_edit_own']
        self.assertIsNone(guard(request, resolve('/admin/')))
        self.assertIsNone(guard(request, resolve('/admin/judge/examtag/')))
        with patch('judge.views.exam_offline.generic_message', return_value='blocked'):
            self.assertEqual(guard(request, resolve('/problem/offlineproblem/rank/')), 'blocked')
            request.user = self.users['normal']
            self.assertEqual(guard(request, resolve('/admin/')), 'blocked')


    def test_hidden_status_page_is_readable_before_and_after_finish(self):
        from django.test import override_settings
        with override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
            self.client.force_login(self.users['normal'])
            attempt = self.start()
            sub = self.submit(attempt)
            for ended in (False, True):
                if ended:
                    finish_attempt(self.profile.pk, attempt.pk, early=True)
                response = self.client.get('/submission/%s' % sub.id)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Kết quả đang ẩn')
                self.assertNotContains(response, 'Accepted')
                self.assertTemplateUsed(response, 'submission/offline-hidden.html')


    def test_hidden_submission_in_list_has_no_result(self):
        from django.test import override_settings
        with override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
            self.client.force_login(self.users['normal'])
            attempt = self.start()
            sub = self.submit(attempt)
            response = self.client.get('/submissions/')
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, '/submission/%s' % sub.id)
            self.assertContains(response, '---')
            self.assertNotContains(response, 'title="Accepted"')
            finish_attempt(self.profile.pk, attempt.pk, early=True)
            response = self.client.get('/submissions/')
            self.assertContains(response, '/submission/%s' % sub.id)
            self.assertNotContains(response, 'title="Accepted"')


    def test_other_viewer_sees_result_while_owner_does_not(self):
        from django.test import override_settings
        with override_settings(VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
            attempt = self.start()
            sub = self.submit(attempt)
            finish_attempt(self.profile.pk, attempt.pk, early=True)
            self.client.force_login(self.users['normal'])
            self.assertContains(self.client.get('/submission/%s' % sub.pk), 'Kết quả đang ẩn')
            self.client.force_login(self.users['superuser'])
            response = self.client.get('/submission/%s' % sub.pk)
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'submission/status.html')
            response = self.client.get('/submissions/')
            self.assertContains(response, '10 / 10')
