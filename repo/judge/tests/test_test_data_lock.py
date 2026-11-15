import fcntl
import io
import json
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import InvalidSubmission, JudgeList
from judge.models import (Language, Problem, ProblemData, ProblemDataManifest, ProblemTestCase, StorageProblemUsage,
                          Submission, SubmissionSource, problem_data_storage)
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
        self.data_folder = Path(problem_data_storage.path(self.problem.code))
        self.addCleanup(shutil.rmtree, self.data_folder, ignore_errors=True)

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

    def saved_cases_payload(self, cases, *, deleted=()):
        cases = list(cases)
        payload = {key: value for key, value in self.payload().items()
                   if not key.startswith('cases-0-') and key != 'problem-data-zipfile'}
        payload['cases-TOTAL_FORMS'] = payload['cases-INITIAL_FORMS'] = str(len(cases))
        for index, case in enumerate(cases):
            for field in ('id', 'type', 'order', 'input_file', 'output_file', 'points', 'batch_type'):
                value = getattr(case, field)
                payload[f'cases-{index}-{field}'] = '' if value is None else str(value)
            if case.pk in deleted:
                payload[f'cases-{index}-DELETE'] = 'on'
        return payload

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

    def test_locked_shared_root_cannot_apply_without_confirmation(self):
        mirror = create_problem(code='lockmirror', mirror_of=self.problem)
        response = self.client.post(self.url, self.payload())
        self.assertEqual(response.status_code, 409)
        self.problem.refresh_from_db()
        mirror.refresh_from_db()
        self.assertTrue(self.problem.is_locked)
        self.assertEqual(mirror.mirror_of_id, self.problem.pk)
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
    def test_can_apply_empty_to_a_new_problem_and_retry(self, abort):
        self.unlocked()
        payload = self.saved_cases_payload([])
        for _ in range(2):
            self.assertEqual(self.client.post(self.url, payload).status_code, 302)
            self.problem.refresh_from_db()
            self.assertTrue(self.problem.data_files.tests_applied_empty)
            self.assertFalse(self.problem.is_locked)
            self.assertTrue(self.data_folder.is_dir())
            self.assertFalse((self.data_folder / 'init.yml').exists())
            self.assertFalse(ProblemDataManifest.objects.filter(problem=self.problem).exists())

    @override_settings(STORAGE_PLATFORM_ENABLED=True, STORAGE_DIRECT_DOWNLOAD_ENABLED=True)
    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_delete_last_keeps_archive_and_blocks_old_snapshot(self, abort):
        self.unlocked()
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 302)
        archive = self.problem.data_files.zipfile
        original_archive = Path(archive.path).read_bytes()
        mirror = create_problem(code='emptytestmirror', mirror_of=self.problem)
        old_manifest = ProblemDataManifest.objects.get(problem=self.problem)
        ProblemDataManifest.objects.filter(pk=old_manifest.pk).update(ready_sha256=old_manifest.sha256)
        self.assertEqual(self.client.post(self.url, {'action': 'unlock-tests'}).status_code, 302)
        cases = self.problem.cases.all()
        payload = self.saved_cases_payload(cases, deleted=list(cases.values_list('pk', flat=True)))
        with patch('judge.utils.storage_client.notify_problem_dirty') as dirty:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.client.post(self.url, payload).status_code, 302)
            dirty.assert_called_once()

        self.problem.refresh_from_db()
        old_manifest.refresh_from_db()
        self.assertFalse(self.problem.is_locked)
        self.assertFalse(self.problem.cases.exists())
        self.assertTrue(self.problem.data_files.tests_applied_empty)
        self.assertFalse((self.data_folder / 'init.yml').exists())
        self.assertEqual(Path(archive.path).read_bytes(), original_archive)
        self.assertEqual((old_manifest.sha256, old_manifest.ready_sha256), ('', ''))
        for problem in (self.problem, mirror):
            with self.assertRaises(ValidationError):
                self.submission(problem=problem)

        StorageProblemUsage.objects.create(problem=self.problem, code=self.problem.code, catalog_state='present',
                                           local_status='missing', r2_status='ready', downloadable=True, stale=False)
        response = self.client.get(self.url)
        self.assertContains(response, 'An empty test set has been applied.')
        self.assertContains(response, 'var testsAppliedEmpty = true;')
        self.assertContains(response, 'name="cases-TOTAL_FORMS" value="0"')
        self.assertNotContains(response, 'id="storage-archived-banner"')
        with patch('judge.utils.storage_client.requests.post') as http, patch(
                'judge.utils.storage_client.request_download_url') as download:
            from judge.utils import storage_client
            self.assertEqual(storage_client.ensure_problem_ready(self.problem.pk)['state'],
                             storage_client.READY_STATE_NOT_READY)
            self.assertEqual(self.client.post(self.url, {'action': 'restore-storage'}).status_code, 409)
            for problem in (self.problem, mirror):
                init_url = reverse('problem_data_init', args=[problem.code])
                data_url = reverse('problem_data_file', args=[problem.code, 'init.yml'])
                self.assertEqual(self.client.get(init_url).status_code, 404)
                self.assertEqual(self.client.get(data_url).status_code, 404)
            archive_url = reverse('problem_data_archive', args=[self.problem.code])
            self.assertEqual(self.client.get(archive_url).status_code, 200)
            http.assert_not_called()
            download.assert_not_called()

        with patch('judge.views.storage_admin.StorageAdminOverview._restore_lock_map', return_value={}), \
                patch('judge.utils.storage_client.requests.post') as http, \
                patch('judge.views.storage_admin.storage_sync_catalog'):
            admin_url = reverse('status_storage')
            response = self.client.get(admin_url, {'section': 'problems'})
            self.assertNotContains(response, 'name="action" value="restore"')
            response = self.client.post(admin_url, {'action': 'restore', 'section': 'problems',
                                                    'problem_id': self.problem.pk})
            self.assertEqual(response.url, admin_url + '?section=problems&done=restore_unavailable')
            http.assert_not_called()

        payload = self.payload()
        del payload['problem-data-zipfile']
        self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        self.problem.refresh_from_db()
        self.assertTrue(self.problem.is_locked)
        self.assertFalse(self.problem.data_files.tests_applied_empty)
        self.assertTrue((self.data_folder / 'init.yml').is_file())
        self.assertTrue(self.problem.cases.exists())
        self.submission(problem=mirror)

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_partial_batch_deletion_and_then_delete_all(self, abort):
        self.unlocked()
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'unlock-tests'}).status_code, 302)
        case = self.problem.cases.get()
        case.order = 1
        case.save(update_fields=['order'])
        ProblemTestCase.objects.create(dataset=self.problem, type='S', order=0, points=10, is_pretest=False)
        extra = ProblemTestCase.objects.create(dataset=self.problem, type='C', order=2, points=1,
                                               input_file='1.in', output_file='1.out', is_pretest=False)
        ProblemTestCase.objects.create(dataset=self.problem, type='E', order=3, is_pretest=False)
        cases = self.problem.cases.order_by('order')
        payload = self.saved_cases_payload(cases, deleted=[extra.pk])
        self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        self.assertEqual(list(cases.values_list('type', flat=True)), ['S', 'C', 'E'])
        self.problem.refresh_from_db()
        self.assertTrue(self.problem.is_locked)
        self.assertFalse(self.problem.data_files.tests_applied_empty)

        self.assertEqual(self.client.post(self.url, {'action': 'unlock-tests'}).status_code, 302)
        payload = self.saved_cases_payload(cases.all(), deleted=list(cases.values_list('pk', flat=True)))
        payload['problem-data-zipfile-clear'] = 'on'
        self.assertEqual(self.client.post(self.url, payload).status_code, 302)
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_locked)
        self.assertTrue(self.problem.data_files.tests_applied_empty)
        self.assertFalse(self.problem.data_files.zipfile)
        self.assertFalse(cases.all().exists())

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_empty_apply_failure_restores_init_and_case_rows(self, abort):
        self.unlocked()
        self.assertEqual(self.client.post(self.url, self.payload()).status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'unlock-tests'}).status_code, 302)
        original = ProblemDataManifest.objects.get(problem=self.problem)
        init = self.data_folder / 'init.yml'
        old_yaml = init.read_bytes()
        cases = self.problem.cases.all()
        payload = self.saved_cases_payload(cases, deleted=list(cases.values_list('pk', flat=True)))

        def fail_after_removing_yaml(problem):
            self.assertFalse(init.exists())
            raise ValidationError('Failed after removing init.yml')

        with patch('judge.utils.storage_client.notify_problem_dirty_on_commit', side_effect=fail_after_removing_yaml):
            self.assertEqual(self.client.post(self.url, payload).status_code, 409)
        self.problem.refresh_from_db()
        self.assertEqual(init.read_bytes(), old_yaml)
        self.assertEqual(cases.count(), 1)
        self.assertFalse(self.problem.data_files.tests_applied_empty)
        self.assertFalse(self.problem.is_locked)
        self.assertEqual(ProblemDataManifest.objects.get(problem=self.problem).sha256, original.sha256)

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_invalid_new_archive_cannot_be_applied_as_empty(self, abort):
        self.unlocked()
        payload = self.saved_cases_payload([])
        payload['problem-data-zipfile'] = SimpleUploadedFile('invalid.zip', b'invalid')
        response = self.client.post(self.url, payload)
        self.assertContains(response, 'Your zip file is invalid!')
        self.assertFalse(ProblemData.objects.filter(problem=self.problem).exists())

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

    @patch('judge.utils.test_data_lock.abort_test_submissions')
    def test_empty_apply_rolls_back_if_database_cannot_commit(self, abort):
        author = create_user(username='commitfailauthor', is_superuser=True)
        problem = create_problem(code='commitfailtest', is_locked=False)
        folder = Path(problem_data_storage.path(problem.code))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        self.client.force_login(author)
        url = reverse('problem_data', args=[problem.code])
        payload = TestDataLockTests.payload(self)
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        self.assertEqual(self.client.post(url, {'action': 'unlock-tests'}).status_code, 302)
        case_id = problem.cases.get().pk
        manifest = ProblemDataManifest.objects.get(problem=problem)
        old_yaml = (folder / 'init.yml').read_bytes()
        del payload['problem-data-zipfile']
        payload.update({'cases-INITIAL_FORMS': '1', 'cases-0-id': case_id, 'cases-0-DELETE': 'on'})

        def fail_commit():
            self.assertFalse((folder / 'init.yml').exists())
            raise OperationalError('test commit failure')

        with patch.object(connection, 'commit', side_effect=fail_commit), patch(
                'judge.utils.storage_client.notify_problem_dirty') as dirty:
            with self.assertRaises(OperationalError):
                self.client.post(url, payload)
            dirty.assert_not_called()
        self.assertEqual((folder / 'init.yml').read_bytes(), old_yaml)
        self.assertTrue(problem.cases.filter(pk=case_id).exists())
        problem.refresh_from_db()
        self.assertFalse(problem.is_locked)
        self.assertFalse(problem.data_files.tests_applied_empty)
        self.assertEqual(ProblemDataManifest.objects.get(problem=problem).sha256, manifest.sha256)

        def check_commit(problem_id, *args, **kwargs):
            self.assertEqual(problem_id, problem.pk)
            self.assertFalse((folder / 'init.yml').exists())
            self.assertTrue(ProblemData.objects.get(problem=problem).tests_applied_empty)
            with open(problem_data_lock_path('problem:%s' % problem.pk), 'a+') as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        with patch('judge.utils.storage_client.notify_problem_dirty', side_effect=check_commit) as dirty:
            self.assertEqual(self.client.post(url, payload).status_code, 302)
            dirty.assert_called_once()
