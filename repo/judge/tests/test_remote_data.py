import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import Mock, patch

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import DeferredSubmission, JudgeList
from judge.models import (Judge, Language, Problem, ProblemDataManifest, ProblemTestCase, Submission,
                          problem_data_storage)
from judge.models.tests.util import create_problem, create_user
from judge.tasks.remote_data import check_remote_data, sync_remote_data
from judge.tests import test_test_data_lock
from judge.utils.remote_data import CAPABILITY, TICKET_SALT, build_manifest, manifest_digest, publish_manifest
from judge.utils.test_data_lock import locked_problem, unlock_tests


class RemoteFixtures:
    def make_data(self):
        self.author = create_user(username='remote-data-author', is_superuser=True)
        self.root = create_problem(code='remotedataset', is_public=True)
        self.folder = Path(problem_data_storage.path(self.root.code))
        shutil.rmtree(self.folder, ignore_errors=True)
        self.folder.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / 'init.yml').write_text('archive: tests.zip\n')
        (self.folder / 'tests.zip').write_bytes(b'zip-bytes')
        (self.folder / 'checker.cpp').write_text('checker-v1')
        with locked_problem(self.root.pk):
            publish_manifest(self.root)
        self.data = ProblemDataManifest.objects.get(problem=self.root)

    def ready(self):
        ProblemDataManifest.objects.filter(pk=self.data.pk).update(ready_sha256=self.data.sha256)

    def page(self, items=None, snapshot='snapshot-one', more=False, cursor=None):
        files = self.data.manifest['files'] if items is None else items
        return {
            'snapshot_id': snapshot, 'snapshot_state': 'ready',
            'file_count': len(self.data.manifest['files']), 'has_more': more, 'next_cursor': cursor,
            'items': [{'snapshot_id': snapshot, 'rel_path': item['path'], 'sha256': item['sha256'],
                       'size_bytes': item['size'], 'uploaded': True, 'verified': True} for item in files],
        }

    def submission(self, problem=None, status='QU', judge=None, token='dispatch-one'):
        return Submission.objects.create(
            problem=problem or self.root, user=self.author.profile, language=Language.get_python3(),
            status=status, judged_on=judge, batch_scoring={'dispatch_id': token})

    def handler(self, judge):
        handler = object.__new__(JudgeHandler)
        handler.name, handler.judge = judge.name, judge
        handler.is_disabled = False
        handler.problems, handler.executors = {}, {Language.get_python3().key: {}}
        handler.capabilities = {CAPABILITY}
        handler._working = False
        handler._dispatch_id = None
        handler._remote_run = None
        handler._remote_preparing = False
        handler._pending_usage = None
        handler._no_response_job = None
        handler.load = 0
        handler.send = Mock()
        handler.judges = JudgeList()
        return handler


class ManifestTests(RemoteFixtures, TestCase):
    def setUp(self):
        self.make_data()

    def test_manifest_hashes_zip_and_every_auxiliary_file(self):
        files = {item['path']: item for item in self.data.manifest['files']}
        self.assertEqual(set(files), {'init.yml', 'tests.zip', 'checker.cpp'})
        self.assertEqual(files['tests.zip']['sha256'], hashlib.sha256(b'zip-bytes').hexdigest())
        self.assertEqual(manifest_digest(build_manifest(self.root)), self.data.sha256)

    def test_only_checker_changes_zip_hash_stays_identical(self):
        (self.folder / 'checker.cpp').write_text('checker-v2')
        newer = build_manifest(self.root)
        old_files = {item['path']: item['sha256'] for item in self.data.manifest['files']}
        new_files = {item['path']: item['sha256'] for item in newer['files']}
        self.assertNotEqual(manifest_digest(newer), self.data.sha256)
        self.assertEqual(old_files['tests.zip'], new_files['tests.zip'])
        self.assertNotEqual(old_files['checker.cpp'], new_files['checker.cpp'])

    def test_conservative_dependencies_keep_locked_manifest_and_active_run(self):
        from judge.utils.conservative import dependency_context

        for order, kind in enumerate(('S', 'C', 'E', 'S', 'C', 'E')):
            ProblemTestCase.objects.create(
                dataset=self.root, order=order, type=kind,
                points=50 if kind == 'S' else None, is_pretest=False)
        self.ready()
        active = self.submission(status='G')
        manifest, sha256 = self.data.manifest, self.data.sha256
        self.client.force_login(self.author)
        with patch('judge.utils.problem_data.ProblemDataCompiler.generate') as compile:
            response = self.client.post(reverse('problem_data', args=[self.root.code]), {
                'action': 'save-conservative-dependencies',
                'conservative-layout': dependency_context(self.root)['conservative_layout'],
                'conservative-dep-2': ['1'],
            })
        self.assertEqual(response.status_code, 302)
        compile.assert_not_called()
        self.root.refresh_from_db()
        self.data.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(self.root.conservative_config['dependencies']['2'], [1])
        self.assertTrue(self.root.is_locked)
        self.assertEqual(self.data.manifest, manifest)
        self.assertEqual(self.data.sha256, sha256)
        self.assertEqual(self.data.ready_sha256, sha256)
        self.assertEqual(manifest_digest(build_manifest(self.root)), sha256)
        self.assertEqual(active.status, 'G')
        self.assertEqual(active.batch_scoring['dispatch_id'], 'dispatch-one')

    def test_removal_rename_and_mode_change_change_manifest(self):
        original = self.data.sha256
        path = self.folder / 'checker.cpp'
        path.chmod(0o755)
        self.assertNotEqual(manifest_digest(build_manifest(self.root)), original)
        path.rename(self.folder / 'other.cpp')
        self.assertNotEqual(manifest_digest(build_manifest(self.root)), original)

    def test_external_symlink_cannot_be_published(self):
        (self.folder / 'escape').symlink_to('/etc/passwd')
        with self.assertRaises(ValidationError):
            build_manifest(self.root)

    def test_unlock_clears_ready_and_keeps_finished_results(self):
        self.ready()
        done = self.submission(status='D')
        active = self.submission()
        unlock_tests(self.root.pk)
        self.data.refresh_from_db()
        done.refresh_from_db()
        active.refresh_from_db()
        self.assertEqual(self.data.ready_sha256, '')
        self.assertEqual(done.status, 'D')
        self.assertEqual(active.status, 'AB')

    def test_file_storage_mutation_invalidates_publication(self):
        from django.core.files.base import ContentFile
        self.ready()
        problem_data_storage.save(self.root.code + '/checker.cpp', ContentFile(b'changed'))
        self.data.refresh_from_db()
        self.assertEqual(self.data.sha256, '')
        self.assertEqual(self.data.ready_sha256, '')

    def test_manual_roundtrip_requires_new_publication_even_for_bulk_updates(self):
        self.ready()
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=True)
        (self.folder / 'checker.cpp').write_text('changed while manually managed')
        Problem.objects.filter(pk=self.root.pk).update(is_manually_managed=False)
        self.data.refresh_from_db()
        self.assertEqual(self.data.sha256, '')
        self.assertEqual(self.data.ready_sha256, '')

    def test_deferred_statement_save_does_not_invalidate_unchanged_manifest(self):
        self.ready()
        problem = Problem.objects.only('name').get(pk=self.root.pk)
        problem.name = 'Updated statement title'
        problem.save(update_fields=['name'])
        self.data.refresh_from_db()
        self.assertEqual(self.data.sha256, self.data.ready_sha256)

    def test_apply_publishes_manifest_before_locking(self):
        Problem.objects.filter(pk=self.root.pk).update(is_locked=False)
        self.client.force_login(self.author)
        response = self.client.post(reverse('problem_data', args=[self.root.code]),
                                    test_test_data_lock.TestDataLockTests.payload(self))
        self.assertEqual(response.status_code, 302)
        self.root.refresh_from_db()
        self.data.refresh_from_db()
        self.assertTrue(self.root.is_locked)
        self.assertEqual(self.data.sha256, manifest_digest(build_manifest(self.root)))
        self.assertEqual(self.data.ready_sha256, '')

    @patch('judge.utils.remote_data.build_manifest', side_effect=ValidationError('hash failed'))
    def test_failed_manifest_keeps_apply_unlocked(self, build):
        Problem.objects.filter(pk=self.root.pk).update(is_locked=False)
        self.client.force_login(self.author)
        response = self.client.post(reverse('problem_data', args=[self.root.code]),
                                    test_test_data_lock.TestDataLockTests.payload(self))
        self.assertEqual(response.status_code, 409)
        self.root.refresh_from_db()
        self.assertFalse(self.root.is_locked)


class StorageReadinessTests(RemoteFixtures, TestCase):
    def setUp(self):
        self.make_data()

    @patch('judge.tasks.remote_data.storage_client.get_problem_files')
    def test_only_complete_verified_matching_snapshot_becomes_ready(self, get_files):
        page = self.page()
        get_files.return_value = page
        self.assertTrue(check_remote_data(self.root.pk))
        self.data.refresh_from_db()
        self.assertEqual(self.data.ready_sha256, self.data.sha256)
        for change in ('hash', 'verified', 'missing', 'extra', 'snapshot'):
            ProblemDataManifest.objects.filter(pk=self.data.pk).update(ready_sha256='')
            bad = json.loads(json.dumps(page))
            if change == 'hash':
                bad['items'][0]['sha256'] = '0' * 64
            elif change == 'verified':
                bad['items'][0]['verified'] = False
            elif change == 'missing':
                bad['items'].pop()
            elif change == 'extra':
                bad['items'].append(dict(bad['items'][0], rel_path='extra.txt'))
            else:
                bad['items'][0]['snapshot_id'] = 'another'
            get_files.return_value = bad
            self.assertFalse(check_remote_data(self.root.pk), change)

    @patch('judge.tasks.remote_data.storage_client.get_problem_files')
    def test_generation_switch_between_pages_is_rejected(self, get_files):
        files = self.data.manifest['files']
        get_files.side_effect = [self.page(files[:1], more=True, cursor='one'),
                                 self.page(files[1:], snapshot='snapshot-two')]
        self.assertFalse(check_remote_data(self.root.pk))

    @patch('judge.tasks.remote_data.storage_client.get_problem_files')
    def test_incomplete_pagination_and_duplicate_cursor_are_rejected(self, get_files):
        get_files.return_value = self.page([], more=True, cursor='same')
        self.assertFalse(check_remote_data(self.root.pk))
        self.assertEqual(get_files.call_count, 2)

    @override_settings(REMOTE_JUDGING_ENABLED=True)
    @patch('judge.judgeapi.judge_request')
    @patch('judge.tasks.remote_data.storage_client.get_problem_files')
    def test_periodic_sync_wakes_idle_judges(self, get_files, wake):
        get_files.return_value = self.page()
        sync_remote_data()
        wake.assert_called_once_with({'name': 'retry-queued-submissions'})


@override_settings(REMOTE_JUDGING_ENABLED=True, REMOTE_JUDGE_DATA_URL='https://oj.example/judge/remote-data/')
class RemoteRoutingAndEndpointTests(RemoteFixtures, TestCase):
    def setUp(self):
        self.make_data()
        self.ready()
        self.remote = Judge.objects.create(name='remote-data-node', auth_key='test-key', is_remote=True, online=True)
        self.handler_instance = self.handler(self.remote)

    def ticket(self, sub, manifest=None):
        return signing.dumps({'submission': sub.pk, 'dispatch': sub.batch_scoring['dispatch_id'],
                              'judge': self.remote.pk, 'root': self.root.pk,
                              'manifest': manifest or self.data.sha256}, salt=TICKET_SALT)

    def fetch(self, ticket, paths=None):
        return self.client.post(reverse('remote_judge_data'),
                                json.dumps({'ticket': ticket, 'paths': paths or []}),
                                content_type='application/json')

    def test_cold_remote_receives_managed_ready_problem(self):
        self.assertTrue(self.handler_instance.can_judge(self.root.code, Language.get_python3().key))
        self.handler_instance.capabilities = set()
        self.assertFalse(self.handler_instance.can_judge(self.root.code, Language.get_python3().key))

    def test_old_ready_hash_and_unlocked_target_are_not_eligible(self):
        ProblemDataManifest.objects.filter(pk=self.data.pk).update(ready_sha256='older')
        self.assertFalse(self.handler_instance.can_judge(self.root.code, Language.get_python3().key))
        self.ready()
        mirror = create_problem(code='remote-data-mirror', mirror_of=self.root)
        sub = self.submission(problem=mirror)
        Problem.objects.filter(pk=mirror.pk).update(is_locked=False)
        self.assertFalse(self.handler_instance.can_judge(
            self.root.code, Language.get_python3().key, submission_id=sub.pk,
        ))

    def test_valid_ticket_gets_manifest_but_not_other_paths(self):
        sub = self.submission(status='P', judge=self.remote)
        response = self.fetch(self.ticket(sub))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['manifest'], self.data.manifest)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(self.fetch(self.ticket(sub), ['../secret']).status_code, 403)
        self.assertEqual(self.fetch(self.ticket(sub), ['not-in-manifest']).status_code, 403)
        self.assertEqual(self.fetch('forged-ticket').status_code, 403)

    def test_expired_ticket_and_deleted_root_are_rejected(self):
        sub = self.submission(status='P', judge=self.remote)
        with patch('django.core.signing.time.time', return_value=0):
            expired = self.ticket(sub)
        self.assertEqual(self.fetch(expired).status_code, 403)
        ticket = self.ticket(sub)
        self.root.delete()
        self.assertEqual(self.fetch(ticket).status_code, 409)

    @override_settings(REMOTE_JUDGING_ENABLED=False)
    def test_feature_off_blocks_remote_even_with_cached_data_and_capability(self):
        self.assertFalse(self.handler_instance.can_judge(self.root.code, Language.get_python3().key))
        sub = self.submission(status='P', judge=self.remote)
        self.assertEqual(self.fetch(self.ticket(sub)).status_code, 503)

    @patch('judge.bridge.judge_handler.threading.Timer')
    def test_dispatch_pins_manifest_and_ticket_to_current_run(self, timer):
        sub = self.submission()
        handler = self.handler_instance
        handler.submit(sub.pk, self.root.code, sub.language.key, 'source')
        packet = handler.send.call_args[0][0]
        metadata = packet['meta']['remote-data']
        self.assertEqual(metadata['sha256'], self.data.sha256)
        ticket = signing.loads(metadata['ticket'], salt=TICKET_SALT)
        self.assertEqual(ticket, {'submission': sub.pk, 'dispatch': 'dispatch-one',
                                  'judge': self.remote.pk, 'root': self.root.pk, 'manifest': self.data.sha256})
        sub.refresh_from_db()
        self.assertEqual(sub.batch_scoring['data_manifest_sha256'], self.data.sha256)
        self.assertEqual(sub.batch_scoring['dispatch_id'], 'dispatch-one')
        self.assertIn('streak', sub.batch_scoring)
        self.assertIn('cumulative_revision', sub.batch_scoring)
        self.assertEqual(sub.judged_on_id, self.remote.pk)
        self.assertEqual(self.fetch(metadata['ticket']).status_code, 200)

    @patch('judge.judgeapi._send_submission', return_value=True)
    def test_explicit_rejudge_uses_new_manifest_without_apply_rejudging_old_results(self, send):
        from judge.judgeapi import judge_submission

        sub = self.submission(status='D')
        Submission.objects.filter(pk=sub.pk).update(points=57, result='WA')
        unlock_tests(self.root.pk)
        (self.folder / 'checker.cpp').write_text('checker-v2')
        with locked_problem(self.root.pk):
            publish_manifest(self.root)
            Problem.objects.filter(pk=self.root.pk).update(is_locked=True)
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.points, sub.result), ('D', 57, 'WA'))
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(judge_submission(sub, rejudge=True))
        current = ProblemDataManifest.objects.get(problem=self.root)
        self.handler_instance.get_related_submission_data(sub.pk)
        sub.refresh_from_db()
        self.assertEqual(sub.batch_scoring['data_manifest_sha256'], current.sha256)
        self.assertNotEqual(current.sha256, self.data.sha256)
        send.assert_called_once()

    def test_ticket_cannot_survive_unlock_rejudge_or_other_assignment(self):
        sub = self.submission(status='P', judge=self.remote)
        ticket = self.ticket(sub)
        for field, value in (('status', 'AB'), ('batch_scoring', {'dispatch_id': 'new'}), ('judged_on_id', None)):
            Submission.objects.filter(pk=sub.pk).update(**{field: value})
            self.assertEqual(self.fetch(ticket).status_code, 409)
            Submission.objects.filter(pk=sub.pk).update(
                status='P', batch_scoring=sub.batch_scoring, judged_on=self.remote,
            )

    @patch('judge.views.remote_data.storage_client.request_download_url')
    def test_unlock_during_url_request_prevents_response(self, download):
        sub = self.submission(status='P', judge=self.remote)

        def during_request(*args, **kwargs):
            unlock_tests(self.root.pk)
            return {'url': 'https://r2.example/object', 'expires_at': 'later'}

        download.side_effect = during_request
        self.assertEqual(self.fetch(self.ticket(sub), ['checker.cpp']).status_code, 409)

    def assigned(self):
        sub = self.submission(status='P', judge=self.remote)
        handler = self.handler_instance
        handler._working, handler._dispatch_id = sub.pk, 'dispatch-one'
        handler._remote_run, handler._remote_preparing = {'root': self.root.pk, 'sha256': self.data.sha256}, True
        handler._retry_request = (sub.pk, self.root.code, sub.language.key, 'source', None, 1, [])
        handler.judges.judges.add(handler)
        handler.judges.submission_map[sub.pk] = handler
        return sub, handler

    @patch('judge.bridge.judge_handler.threading.Timer')
    def test_preparation_failure_returns_qu_rotates_token_and_does_not_rejudge(self, timer):
        sub, handler = self.assigned()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertTrue(handler._retry_remote_submission())
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')
        self.assertNotEqual(sub.batch_scoring['dispatch_id'], 'dispatch-one')
        self.assertIsNone(sub.rejudged_date)
        self.assertIn(sub.pk, handler.judges.node_map)
        self.assertFalse(handler._current_score_run({'submission-id': sub.pk, 'dispatch-id': 'dispatch-one'}))

    def test_aborted_preparation_is_not_requeued(self):
        sub, handler = self.assigned()
        Submission.objects.filter(pk=sub.pk).update(status='AB', result='AB')
        self.assertFalse(handler._retry_remote_submission())
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')

    def test_late_failure_packet_after_unlock_releases_slot_without_resurrecting_run(self):
        sub, handler = self.assigned()
        unlock_tests(self.root.pk)
        packet = {'name': 'problem-data-unavailable', 'submission-id': sub.pk,
                  'dispatch-id': 'dispatch-one', 'sha256': self.data.sha256}
        with self.captureOnCommitCallbacks(execute=True):
            handler._handle_submission_packet(handler.on_problem_data_unavailable, packet)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')
        self.assertNotIn(sub.pk, handler.judges.node_map)
        self.assertFalse(handler.working)

    def test_remote_readiness_race_keeps_valid_queued_submission(self):
        handler = self.handler_instance
        handler.can_judge = Mock(return_value=True)
        handler.submit = Mock(side_effect=DeferredSubmission())
        handler.judges.judges.add(handler)
        handler.judges.judge(42, self.root.code, 'PY3', 'source', None, 1)
        self.assertIn(42, handler.judges.node_map)
        self.assertNotIn(42, handler.judges.submission_map)


class RemotePublicationRaceTests(RemoteFixtures, TransactionTestCase):
    def setUp(self):
        self.make_data()

    def test_delayed_r2_confirmation_cannot_acknowledge_new_apply(self):
        started, release = Event(), Event()
        old_page = self.page()

        def get_files(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise AssertionError('test synchronization timed out')
            return old_page

        def poll():
            close_old_connections()
            try:
                return check_remote_data(self.root.pk)
            finally:
                close_old_connections()

        with patch('judge.tasks.remote_data.storage_client.get_problem_files', side_effect=get_files):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(poll)
                self.assertTrue(started.wait(10))
                try:
                    unlock_tests(self.root.pk)
                    (self.folder / 'checker.cpp').write_text('checker-v2')
                    with locked_problem(self.root.pk):
                        publish_manifest(self.root)
                        Problem.objects.filter(pk=self.root.pk).update(is_locked=True)
                finally:
                    release.set()
                self.assertFalse(future.result(timeout=10))
        current = ProblemDataManifest.objects.get(problem=self.root)
        self.assertNotEqual(current.sha256, self.data.sha256)
        self.assertEqual(current.ready_sha256, '')


class ManifestMigrationTests(RemoteFixtures, TransactionTestCase):
    def test_metadata_migration_preserves_existing_submission_scores(self):
        self.make_data()
        sub = self.submission(status='D')
        Submission.objects.filter(pk=sub.pk).update(points=57, result='WA', time=0.25, memory=4096)
        fields = ('status', 'result', 'points', 'time', 'memory', 'batch_scoring')
        before = Submission.objects.filter(pk=sub.pk).values(*fields).get()
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate([('judge', '0259_judge_is_remote')])
        finally:
            MigrationExecutor(connection).migrate(latest)
        self.assertEqual(Submission.objects.filter(pk=sub.pk).values(*fields).get(), before)
        self.assertFalse(ProblemDataManifest.objects.exists())
