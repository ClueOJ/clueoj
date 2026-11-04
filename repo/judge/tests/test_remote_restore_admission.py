from unittest.mock import patch
from types import SimpleNamespace

from django.test import TestCase, override_settings

from judge.judgeapi import judge_submission
from judge.models import (Judge, Language, Problem, ProblemDataManifest, StorageProblemUsage,
                          Submission, SubmissionSource)
from judge.models.tests.util import create_problem, create_user
from judge.utils.remote_data import manifest_digest
from judge.models.submission import _problem_data_local_usable


class LocalDatasetPresenceTests(TestCase):
    def test_archive_and_init_are_both_required_for_archive_data(self):
        problem = SimpleNamespace(code='presence', data_files=SimpleNamespace(
            zipfile=SimpleNamespace(name='presence/tests.zip')))
        for init, archive in [(False, True), (True, False), (False, False), (True, True)]:
            with self.subTest(init=init, archive=archive), patch(
                    'judge.models.problem_data.problem_data_storage.exists',
                    side_effect=lambda path: {'presence/init.yml': init, 'presence/tests.zip': archive}[path]):
                self.assertEqual(_problem_data_local_usable(problem), init and archive)

    def test_non_archive_dataset_keeps_init_presence_rule(self):
        problem = SimpleNamespace(code='presence', data_files=SimpleNamespace(zipfile=None))
        with patch('judge.models.problem_data.problem_data_storage.exists', return_value=True):
            self.assertTrue(_problem_data_local_usable(problem))


@override_settings(STORAGE_ENSURE_READY_ENABLED=True, REMOTE_JUDGING_ENABLED=True,
                   REMOTE_JUDGE_DATA_URL='https://oj.example/judge/remote-data/')
class RemoteRestoreAdmissionTests(TestCase):
    def setUp(self):
        self.user = create_user(username='archived-author')
        self.root = create_problem(code='archivedroot')
        self.mirror = create_problem(code='archivedmirror', mirror_of=self.root)
        self.local = Judge.objects.create(name='archive-local', auth_key='local')
        self.remote = Judge.objects.create(name='archive-remote', auth_key='remote', is_remote=True)
        self.language = Language.get_python3()
        self.usage = StorageProblemUsage.objects.create(
            problem=self.root, code=self.root.code, local_status='missing',
            r2_status='READY', catalog_state='present', stale=False)
        self.local_check = patch('judge.models.submission._problem_data_local_usable', return_value=False).start()
        self.addCleanup(patch.stopall)

    def submission(self, problem=None, status='QU'):
        sub = Submission.objects.create(problem=problem or self.root, user=self.user.profile,
                                        language=self.language, status=status)
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        return sub

    def publish(self):
        manifest = {'version': 1, 'problem_id': self.root.pk, 'files': []}
        sha256 = manifest_digest(manifest)
        ProblemDataManifest.objects.create(problem=self.root, manifest=manifest,
                                            sha256=sha256, ready_sha256=sha256)

    def assert_no_restore(self, submission, **kwargs):
        with patch('judge.utils.storage_client.ensure_problem_ready') as restore, \
                patch('judge.models.submission.judge_submission', wraps=judge_submission) as dispatch, \
                patch('judge.tasks.storage.storage_retry_judge_submission.apply_async') as retry:
            submission.judge(**kwargs)
        restore.assert_not_called()
        retry.assert_not_called()
        dispatch.assert_called_once()
        self.assertFalse(dispatch.call_args.kwargs['storage_claimed'])
        submission.refresh_from_db()
        self.assertEqual(submission.status, 'QU')
        self.assertTrue(submission.batch_scoring['dispatch_id'])

    def test_storage_ready_without_manifest_queues_for_bootstrap(self):
        self.assert_no_restore(self.submission())

    def test_manifest_ready_without_storage_projection_queues_for_remote(self):
        self.publish()
        self.usage.delete()
        self.assert_no_restore(self.submission())

    def test_mirror_uses_root_ready_state_without_restoring(self):
        self.assert_no_restore(self.submission(self.mirror))

    def test_no_online_remote_is_not_a_reason_to_restore(self):
        self.assertFalse(Judge.objects.filter(is_remote=True, online=True).exists())
        self.assert_no_restore(self.submission())

    def test_disabled_remote_is_not_a_reason_to_restore(self):
        Judge.objects.filter(pk=self.remote.pk).update(is_disabled=True)
        self.assert_no_restore(self.submission())

    def test_forced_remote_does_not_restore(self):
        self.assert_no_restore(self.submission(), judge_id=self.remote.name)

    def test_old_restore_retry_rechecks_and_stops_restoring(self):
        self.assert_no_restore(self.submission(), force_judge=True, ensure_ready_attempt=4)

    def test_explicit_rejudge_uses_remote_admission(self):
        self.assert_no_restore(self.submission(status='D'), rejudge=True)

    def test_batch_rejudge_uses_remote_admission(self):
        self.assert_no_restore(self.submission(status='D'), batch_rejudge=True)

    def test_historical_ab_is_not_resurrected(self):
        submission = self.submission(status='AB')
        with patch('judge.utils.storage_client.ensure_problem_ready') as restore, \
                patch('judge.models.submission.judge_submission') as dispatch:
            submission.judge(force_judge=True)
        restore.assert_not_called()
        dispatch.assert_not_called()
        submission.refresh_from_db()
        self.assertEqual(submission.status, 'AB')

    def assert_legacy_restore(self, **kwargs):
        with patch('judge.utils.storage_client.ensure_problem_ready', return_value={'ready': True}) as restore, \
                patch('judge.models.submission.judge_submission') as dispatch:
            self.submission().judge(**kwargs)
        restore.assert_called_once()
        dispatch.assert_called_once()
        self.assertTrue(dispatch.call_args.kwargs['storage_claimed'])

    def test_explicit_local_keeps_restore_behavior(self):
        self.assert_legacy_restore(judge_id=self.local.name)

    @override_settings(REMOTE_JUDGING_ENABLED=False)
    def test_remote_disabled_keeps_restore_behavior(self):
        self.assert_legacy_restore()

    @override_settings(REMOTE_JUDGE_DATA_URL='')
    def test_no_remote_endpoint_keeps_restore_behavior(self):
        self.assert_legacy_restore()

    def test_unavailable_storage_without_manifest_keeps_restore_behavior(self):
        self.usage.r2_status = 'none'
        self.usage.save(update_fields=['r2_status'])
        self.assert_legacy_restore()

    def test_stale_storage_without_manifest_keeps_restore_behavior(self):
        self.usage.stale = True
        self.usage.save(update_fields=['stale'])
        self.assert_legacy_restore()

    def test_manually_managed_root_keeps_local_flow(self):
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        self.root.refresh_from_db()
        self.assert_legacy_restore()

    def test_local_files_keep_existing_local_dispatch(self):
        self.local_check.return_value = True
        with patch('judge.utils.storage_client.ensure_problem_ready') as restore, \
                patch('judge.models.submission.judge_submission') as dispatch:
            self.submission().judge()
        restore.assert_not_called()
        self.assertTrue(dispatch.call_args.kwargs['storage_claimed'])
