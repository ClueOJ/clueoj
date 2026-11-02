from unittest.mock import Mock, patch

from django.contrib import admin
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings

from judge.admin.runtime import JudgeAdmin
from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import DeferredSubmission, JudgeList
from judge.models import Judge, Language, Problem, ProblemDataManifest, Submission
from judge.models.tests.util import create_problem, create_user
from judge.utils.remote_data import CAPABILITY, manifest_digest


@override_settings(REMOTE_JUDGING_ENABLED=True, REMOTE_JUDGE_DATA_URL='https://oj.example/judge/remote-data/')
class RemoteJudgeRoutingTests(TestCase):
    def setUp(self):
        self.user = create_user(username='remoteauthor')
        self.language = Language.get_python3()
        self.root = create_problem(code='remoteroot')
        self.mirror = create_problem(code='remotemirror', mirror_of=self.root)
        self.local = Judge.objects.create(name='local-test', auth_key='local-key')
        self.remote = Judge.objects.create(name='remote-test', auth_key='remote-key', is_remote=True)
        manifest = {'version': 1, 'problem_id': self.root.pk, 'files': []}
        digest = manifest_digest(manifest)
        ProblemDataManifest.objects.create(problem=self.root, manifest=manifest, sha256=digest, ready_sha256=digest)

    def handler(self, judge):
        handler = object.__new__(JudgeHandler)
        handler.name = judge.name
        handler.capabilities = {CAPABILITY}
        handler.is_disabled = False
        handler.problems = {self.root.code: 0, self.mirror.code: 0}
        handler.executors = {self.language.key: {}}
        handler._working = False
        handler.load = 0
        handler.send = Mock()
        handler.request = Mock()
        handler.judges = JudgeList()
        return handler

    def submission(self, problem):
        return Submission.objects.create(problem=problem, user=self.user.profile, language=self.language)

    def test_existing_default_is_local_and_local_can_judge_manual_root_and_mirror(self):
        self.assertFalse(self.local.is_remote)
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        handler = self.handler(self.local)
        for problem in (self.root, self.mirror):
            with self.subTest(problem=problem.code):
                self.assertTrue(handler.can_judge(problem.code, self.language.key))

    def test_remote_can_judge_managed_root_and_mirror(self):
        handler = self.handler(self.remote)
        for problem in (self.root, self.mirror):
            with self.subTest(problem=problem.code):
                submission = self.submission(problem)
                self.assertTrue(handler.can_judge(self.root.code, self.language.key, submission_id=submission.pk))

    def test_remote_blocks_manual_root_and_mirror_even_when_explicitly_selected(self):
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        handler = self.handler(self.remote)
        for problem in (self.root, self.mirror):
            for selected in (None, self.remote.name):
                with self.subTest(problem=problem.code, selected=selected):
                    submission = self.submission(problem)
                    self.assertFalse(handler.can_judge(problem.code, self.language.key, selected))
                    self.assertFalse(handler.can_judge(self.root.code, self.language.key, selected,
                                                       submission_id=submission.pk))

    def test_remote_checks_submitted_problem_even_when_queue_contains_managed_root_code(self):
        Problem.objects.filter(pk=self.mirror.pk).update(is_manually_managed=True)
        submission = self.submission(self.mirror)
        self.assertFalse(self.handler(self.remote).can_judge(
            self.root.code, self.language.key, submission_id=submission.pk))

    def test_remote_reads_current_problem_flags_instead_of_caching_them(self):
        handler = self.handler(self.remote)
        self.assertTrue(handler.can_judge(self.root.code, self.language.key))
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        self.assertFalse(handler.can_judge(self.root.code, self.language.key))

    def test_direct_dispatch_cannot_bypass_remote_restriction(self):
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        submission = self.submission(self.mirror)
        handler = self.handler(self.remote)
        handler.get_related_submission_data = Mock()
        with self.assertRaises(DeferredSubmission):
            handler.submit(submission.pk, self.root.code, self.language.key, 'print(1)')
        handler.get_related_submission_data.assert_not_called()
        handler.send.assert_not_called()

    def test_automatic_scheduling_chooses_local_for_manual_mirror(self):
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        submission = self.submission(self.mirror)
        local, remote = self.handler(self.local), self.handler(self.remote)
        local.submit, remote.submit = Mock(), Mock()
        judges = JudgeList()
        judges.judges.update((local, remote))
        judges.judge(submission.pk, self.root.code, self.language.key, 'source', None, 0)
        local.submit.assert_called_once()
        remote.submit.assert_not_called()

    def test_forced_remote_stays_queued_and_cannot_bypass_restriction(self):
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        submission = self.submission(self.mirror)
        local, remote = self.handler(self.local), self.handler(self.remote)
        local.submit, remote.submit = Mock(), Mock()
        judges = JudgeList()
        judges.judges.update((local, remote))
        judges.judge(submission.pk, self.root.code, self.language.key, 'source', self.remote.name, 0)
        self.assertIn(submission.pk, judges.node_map)
        local.submit.assert_not_called()
        remote.submit.assert_not_called()

    def test_queued_job_rechecks_manual_flag_when_remote_becomes_free(self):
        submission = self.submission(self.mirror)
        remote = self.handler(self.remote)
        remote.submit = Mock()
        remote._working = 999
        judges = JudgeList()
        judges.judges.add(remote)
        judges.judge(submission.pk, self.root.code, self.language.key, 'source', None, 0)
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        remote._working = False
        judges._handle_free_judge(remote)
        self.assertIn(submission.pk, judges.node_map)
        remote.submit.assert_not_called()
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=False)
        publication = ProblemDataManifest.objects.get(problem=self.root)
        digest = manifest_digest(publication.manifest)
        ProblemDataManifest.objects.filter(pk=publication.pk).update(sha256=digest, ready_sha256=digest)
        judges._handle_free_judge(remote)
        remote.submit.assert_called_once()
        self.assertNotIn(submission.pk, judges.node_map)

    @patch('judge.bridge.judge_handler.threading.Thread')
    def test_old_handshake_checks_remote_policy_before_first_queued_dispatch(self, thread):
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        submission = self.submission(self.mirror)
        judges = JudgeList()
        judges.judge(submission.pk, self.root.code, self.language.key, 'source', None, 0)
        handler = self.handler(self.remote)
        handler.judges = judges
        handler.client_address = ('127.0.0.1', 9999)
        handler._connected = Mock()
        handler.submit = Mock()
        # Existing judges send no location, manifest, or version capability.
        handler.on_handshake({'id': self.remote.name, 'key': self.remote.auth_key,
                              'problems': [(self.root.code, 0)], 'executors': {self.language.key: {}}})
        handler.send.assert_called_once_with({'name': 'handshake-success'})
        handler.submit.assert_not_called()
        self.assertIn(submission.pk, judges.node_map)


class RemoteJudgeAdminTests(TestCase):
    def setUp(self):
        self.model_admin = JudgeAdmin(Judge, admin.site)
        self.model_admin.message_user = Mock()
        self.request = RequestFactory().post('/admin/judge/judge/')
        self.request.user = create_user(username='judgeadmin', is_staff=True, is_superuser=True)

    def test_admin_exposes_type_and_bulk_actions(self):
        self.assertIn('is_remote', self.model_admin.get_form(self.request).base_fields)
        self.assertIn('is_remote', self.model_admin.list_display)
        self.assertEqual(set(self.model_admin.actions), {'mark_local', 'mark_remote'})

    def test_type_is_readonly_for_online_judge(self):
        judge = Judge.objects.create(name='online', auth_key='key', online=True)
        self.assertIn('is_remote', self.model_admin.get_readonly_fields(self.request, judge))
        self.assertNotIn('is_remote', self.model_admin.get_form(self.request, judge).base_fields)

    def test_bulk_changes_only_selected_offline_judges_in_both_directions(self):
        local = Judge.objects.create(name='local', auth_key='key')
        remote = Judge.objects.create(name='remote', auth_key='key', is_remote=True)
        online = Judge.objects.create(name='online', auth_key='key', online=True)
        untouched = Judge.objects.create(name='untouched', auth_key='key')
        selected = Judge.objects.filter(pk__in=[local.pk, remote.pk, online.pk])
        self.model_admin.mark_remote(self.request, selected)
        for judge, expected in ((local, True), (remote, True), (online, False), (untouched, False)):
            judge.refresh_from_db()
            self.assertEqual(judge.is_remote, expected)
        self.assertEqual(self.model_admin.message_user.call_count, 2)
        self.model_admin.mark_local(self.request, selected)
        self.assertFalse(Judge.objects.filter(is_remote=True).exists())


class RemoteJudgeMigrationTests(TransactionTestCase):
    def test_upgrade_marks_existing_judges_local_and_preserves_authentication(self):
        before = [('judge', '0258_invocation_source_unique_filename')]
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        executor.migrate(before)
        try:
            old_judge = executor.loader.project_state(before).apps.get_model('judge', 'Judge')
            old_judge.objects.create(name='existing-judge', auth_key='unchanged-key', online=True)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(latest)
        judge = Judge.objects.get(name='existing-judge')
        self.assertFalse(judge.is_remote)
        self.assertEqual(judge.auth_key, 'unchanged-key')
        self.assertTrue(judge.online)
