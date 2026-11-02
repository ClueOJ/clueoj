import fcntl
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import InvalidSubmission, JudgeList
from judge.models import Language, Problem, ProblemData, Submission, SubmissionSource
from judge.models.tests.util import create_problem, create_user
from judge.utils.problem_data import problem_data_lock, problem_data_lock_path
from judge.utils.test_data_lock import locked_problem, require_judging_allowed, unlock_tests


class TestDataLockTests(TestCase):
    def setUp(self):
        self.author = create_user(username='lockauthor', is_superuser=True)
        self.problem = create_problem(code='locktest', is_public=True)
        self.language = Language.get_python3()
        self.url = reverse('problem_data', args=[self.problem.code])
        self.client.force_login(self.author)

    def submission(self, problem=None, status='QU', token='run-one'):
        sub = Submission.objects.create(problem=problem or self.problem, user=self.author.profile,
                                        language=self.language, status=status,
                                        batch_scoring={'dispatch_id': token})
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        return sub

    def unlocked(self):
        Problem.objects.filter(pk=self.problem.pk).update(is_locked=False)

    def payload(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as data:
            data.writestr('1.in', '1\n')
            data.writestr('1.out', '1\n')
        return {
            'mirror-test_source': 'local', 'mirror-mirror_of': '',
            'problem-data-checker': 'standard', 'problem-data-checker_args': '',
            'problem-data-checker_type': 'default', 'problem-data-grader': 'standard',
            'problem-data-grader_args': '', 'problem-data-io_method': 'standard',
            'problem-data-zipfile': SimpleUploadedFile('tests.zip', archive.getvalue()),
            'cases-TOTAL_FORMS': '1', 'cases-INITIAL_FORMS': '0',
            'cases-MIN_NUM_FORMS': '0', 'cases-MAX_NUM_FORMS': '1000',
            'cases-0-type': 'C', 'cases-0-order': '0', 'cases-0-input_file': '1.in',
            'cases-0-output_file': '1.out', 'cases-0-points': '1', 'cases-0-batch_type': 'sum',
        }

    def handler(self, sub):
        handler = object.__new__(JudgeHandler)
        handler._working = sub.pk
        handler._dispatch_id = (sub.batch_scoring or {}).get('dispatch_id')
        handler._pending_usage = None
        handler._no_response_job = None
        handler.judges = JudgeList()
        handler.judges.submission_map[sub.pk] = handler
        handler.judges.judges.add(handler)
        handler.is_disabled = True
        handler.name = 'lock-judge'
        handler.problems = {}
        handler.executors = {}
        handler.load = 0
        handler.send = Mock()
        return handler

    def test_get_is_readonly_and_does_not_create_problem_data(self):
        sub = self.submission()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'unlock-tests-form')
        self.assertFalse(ProblemData.objects.filter(problem=self.problem).exists())
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')

    def test_locked_post_cannot_upload_or_change_tests(self):
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ProblemData.objects.filter(problem=self.problem).exists())

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_unlock_aborts_active_root_and_mirrors_but_not_finished_or_other_problem(self, abort):
        mirror = create_problem(code='lockmirror', mirror_of=self.problem)
        other = create_problem(code='lockother')
        active = [self.submission(status=status) for status in ('QU', 'P', 'G')]
        active.append(self.submission(problem=mirror))
        finished = self.submission(status='D')
        unaffected = self.submission(problem=other)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {'action': 'unlock-tests'})
        self.assertEqual(response.status_code, 302)
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)
        self.assertEqual(set(Submission.objects.filter(pk__in=[s.pk for s in active]).values_list('status', flat=True)), {'AB'})
        finished.refresh_from_db()
        unaffected.refresh_from_db()
        self.assertEqual(finished.status, 'D')
        self.assertEqual(unaffected.status, 'QU')
        abort.assert_called_once_with(self.problem.pk)

    def test_unlocked_root_blocks_mirror_admission(self):
        mirror = create_problem(code='lockmirror', mirror_of=self.problem)
        self.unlocked()
        with self.assertRaises(ValidationError):
            self.submission(problem=mirror)
        self.assertFalse(Submission.objects.exists())

    def test_stale_problem_save_cannot_open_gate(self):
        self.unlocked()
        self.problem.name = 'Changed statement title'
        self.problem.save()
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)

    def test_late_dispatch_of_aborted_run_is_dropped(self):
        sub = self.submission(status='AB')
        handler = self.handler(sub)
        with self.assertRaises(InvalidSubmission):
            handler.submit(sub.pk, self.problem.code, 'PY3', 'print(1)')
        handler.send.assert_not_called()

    def test_late_callback_cannot_write_and_still_frees_judge(self):
        sub = self.submission(status='AB')
        handler = self.handler(sub)
        callback = Mock()
        with self.captureOnCommitCallbacks(execute=True):
            handler._handle_submission_packet(callback, {'name': 'grading-end', 'submission-id': sub.pk,
                                                        'dispatch-id': 'run-one'})
        callback.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')
        self.assertFalse(handler.working)
        self.assertNotIn(sub.pk, handler.judges.submission_map)

    def test_old_callback_cannot_free_new_attempt_of_same_submission(self):
        sub = self.submission(token='new-run')
        handler = self.handler(sub)
        callback = Mock()
        handler._handle_submission_packet(callback, {'name': 'grading-end', 'submission-id': sub.pk,
                                                    'dispatch-id': 'old-run'})
        callback.assert_not_called()
        self.assertTrue(handler.working)

    def test_old_network_handoff_cannot_attach_to_a_new_run(self):
        from judge.judgeapi import _send_submission
        sub = self.submission(token='new-run')
        with patch('judge.judgeapi.judge_request') as send:
            accepted = _send_submission(sub, False, False, None, 1, dispatch_id='old-run')
        self.assertFalse(accepted)
        send.assert_not_called()

    def test_manual_rejudge_waits_for_cancelled_assignment(self):
        sub = self.submission(status='AB')
        handler = self.handler(sub)
        judges = handler.judges
        judges.dispatch_ids[sub.pk] = 'run-one'
        Submission.objects.filter(pk=sub.pk).update(status='QU', batch_scoring={'dispatch_id': 'new-run'})
        judges.judge(sub.pk, self.problem.code, 'PY3', 'print(1)', None, 1, dispatch_id='new-run')
        self.assertIn(sub.pk, judges.node_map)
        with self.captureOnCommitCallbacks(execute=True):
            handler._handle_submission_packet(Mock(), {'name': 'grading-end', 'submission-id': sub.pk,
                                                      'dispatch-id': 'run-one'})
        self.assertEqual(judges.dispatch_ids[sub.pk], 'new-run')

    @patch('judge.views.problem_data.ProblemDataCompiler.generate')
    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_compile_feedback_cannot_relock_tests(self, abort, compile_tests):
        self.unlocked()

        def invalid(problem, data, cases, files):
            data.feedback = 'Invalid checker configuration'
            data.save()

        compile_tests.side_effect = invalid
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)

    def test_all_late_nonterminal_packets_are_ignored(self):
        sub = self.submission(status='AB')
        handler = self.handler(sub)
        for name in ('test-case-status', 'grading-begin', 'batch-begin', 'compile-message'):
            callback = Mock()
            handler._handle_submission_packet(callback, {'name': name, 'submission-id': sub.pk})
            callback.assert_not_called()

    def test_restore_retry_does_not_resurrect_ab(self):
        from judge.tasks.storage import storage_retry_judge_submission
        sub = self.submission(status='AB')
        with patch.object(Submission, 'judge') as dispatch:
            storage_retry_judge_submission.run(sub.pk)
        dispatch.assert_not_called()

    def test_late_virtual_judge_error_does_not_overwrite_ab(self):
        from judge.external_judge import set_external_submission_error
        sub = self.submission(status='AB')
        set_external_submission_error(sub, 'Late network error')
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.result), ('AB', None))

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_apply_success_relocks_and_keeps_aborted_submissions(self, abort):
        sub = self.submission(status='AB')
        self.unlocked()
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 302, response.content[:1500])
        self.problem.refresh_from_db()
        self.assertTrue(self.problem.is_locked)
        self.assertTrue(self.problem.data_files.has_yml())
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_invalid_apply_stays_unlocked(self, abort):
        self.unlocked()
        payload = self.payload()
        payload['cases-0-input_file'] = 'missing.in'
        response = self.client.post(self.url, payload)
        self.assertIn(response.status_code, (200, 409))
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_apply_cannot_write_while_worker_holds_read_lock(self, abort):
        self.unlocked()
        lock_path = problem_data_lock_path('problem:%s' % self.problem.pk)
        with open(lock_path, 'a+') as reader:
            fcntl.flock(reader, fcntl.LOCK_SH)
            response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.assertFalse(ProblemData.objects.filter(problem=self.problem).exists())
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)

    def test_unauthorized_unlock_cannot_abort_submissions(self):
        sub = self.submission()
        self.client.force_login(create_user(username='lockvisitor'))
        response = self.client.post(self.url, {'action': 'unlock-tests'})
        self.assertEqual(response.status_code, 404)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')


@skipUnlessDBFeature('has_select_for_update')
class TestDataLockConcurrencyTests(TransactionTestCase):
    def test_admission_waiting_behind_unlock_is_rejected(self):
        author = create_user(username='lockrace', is_superuser=True)
        problem = create_problem(code='lockrace')
        language = Language.get_python3()
        started = Event()

        def submit():
            close_old_connections()
            try:
                started.set()
                try:
                    Submission.objects.create(problem_id=problem.pk, user_id=author.profile.pk,
                                              language_id=language.pk)
                except ValidationError:
                    return 'blocked'
                return 'accepted'
            finally:
                connection.close()

        with patch('judge.utils.test_data_lock.abort_test_submissions'):
            with ThreadPoolExecutor(max_workers=1) as pool:
                with locked_problem(problem.pk):
                    future = pool.submit(submit)
                    self.assertTrue(started.wait(5))
                    unlock_tests(problem.pk)
                self.assertEqual(future.result(timeout=10), 'blocked')
        self.assertFalse(Submission.objects.exists())
