import base64
import errno
import json
import os
import tempfile
import zipfile
from io import BytesIO
from unittest.mock import patch, MagicMock

from django.core.files.base import ContentFile
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import Problem, ProblemData, Submission, problem_data_storage
from judge.models.runtime import Language
from judge.models.tests.util import create_problem, create_organization, create_user
from judge.models.storage import StorageProblemUsage, StorageOrganizationUsage, StorageSystemStatus, \
    StorageSyncDeadLetter


class StorageProblemUsageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_user('storage_tester')
        cls.org = create_organization('Test Org', 'test-org')
        cls.problem = create_problem('storage_prob')

    def test_projection_creation(self):
        usage = StorageProblemUsage.objects.create(
            problem=self.problem,
            code='storage_prob',
            owner_organization_id=self.org.pk,
            logical_bytes=1024,
            allocated_bytes=4096,
            archive_bytes=512,
            auxiliary_bytes=512,
            file_count=3,
            quota_bytes=8192,
            local_status='present',
            r2_status='READY',
            snapshot_generation=1,
        )
        self.assertEqual(usage.problem, self.problem)
        self.assertEqual(usage.code, 'storage_prob')
        self.assertEqual(usage.logical_bytes, 1024)
        self.assertEqual(usage.r2_status, 'READY')
        self.assertTrue(usage.stale)

    def test_projection_defaults(self):
        usage = StorageProblemUsage.objects.create(problem=self.problem, code='storage_prob')
        self.assertEqual(usage.logical_bytes, 0)
        self.assertEqual(usage.file_count, 0)
        self.assertEqual(usage.local_status, 'present')
        self.assertEqual(usage.r2_status, 'none')
        self.assertTrue(usage.stale)

    def test_stale_flag(self):
        usage = StorageProblemUsage.objects.create(problem=self.problem, code='storage_prob', stale=False)
        self.assertFalse(usage.stale)
        usage.stale = True
        usage.save()
        usage.refresh_from_db()
        self.assertTrue(usage.stale)


class StorageOrganizationUsageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = create_organization('Org1', 'org1')
        cls.problem = create_problem('org_prob')

    def test_org_usage_creation(self):
        org_usage = StorageOrganizationUsage.objects.create(
            organization=self.org,
            total_logical_bytes=2048,
            total_allocated_bytes=8192,
            problem_count=2,
        )
        self.assertEqual(org_usage.organization, self.org)
        self.assertEqual(org_usage.total_logical_bytes, 2048)
        self.assertEqual(org_usage.problem_count, 2)
        self.assertTrue(org_usage.stale)


class StorageSystemStatusTestCase(TestCase):
    def test_singleton_id(self):
        status = StorageSystemStatus.objects.create(
            id=1,
            volume_total_bytes=1_000_000_000,
            volume_free_bytes=500_000_000,
        )
        self.assertEqual(status.id, 1)
        self.assertEqual(status.volume_total_bytes, 1_000_000_000)

    def test_save_forces_id_1(self):
        status = StorageSystemStatus(id=999)
        status.save()
        self.assertEqual(status.id, 1)

    def test_defaults(self):
        status = StorageSystemStatus.objects.get_or_create(id=1)[0]
        self.assertEqual(status.total_logical_bytes, 0)
        self.assertEqual(status.orphan_count, 0)
        self.assertTrue(status.stale)


class StorageClientTestCase(TestCase):
    @override_settings(STORAGE_CATALOG_SYNC_ENABLED=True, STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.post')
    def test_notify_problem_dirty_calls_api(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        from judge.utils.storage_client import notify_problem_dirty
        notify_problem_dirty('123', 'testcode', 'mut-1')
        mock_post.assert_called_once()
        url = mock_post.call_args[0][0]
        self.assertIn('/problems/123/dirty', url)
        headers = mock_post.call_args[1]['headers']
        self.assertEqual(headers['Authorization'], 'Bearer test-token')
        self.assertEqual(headers['Idempotency-Key'], 'mut-1')

    @override_settings(STORAGE_CATALOG_SYNC_ENABLED=False)
    @patch('judge.utils.storage_client.requests.post')
    def test_notify_problem_dirty_disabled(self, mock_post):
        from judge.utils.storage_client import notify_problem_dirty
        notify_problem_dirty('123', 'testcode')
        mock_post.assert_not_called()

    @override_settings(STORAGE_SERVICE_TOKEN='')
    @patch('judge.utils.storage_client.requests.post')
    def test_notify_problem_dirty_no_token(self, mock_post):
        from judge.utils.storage_client import notify_problem_dirty
        notify_problem_dirty('123', 'testcode')
        mock_post.assert_not_called()

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.post')
    def test_notify_problem_dirty_non_fatal(self, mock_post):
        mock_post.side_effect = Exception('Connection refused')
        from judge.utils.storage_client import notify_problem_dirty
        result = notify_problem_dirty('123', 'testcode')
        self.assertIsNone(result)

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.get')
    def test_get_sync_changes_success(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'changes': [{'external_id': '1'}], 'next_cursor': 'cur1', 'has_more': True},
        )
        from judge.utils.storage_client import get_sync_changes
        changes, cursor, has_more = get_sync_changes(cursor=None, limit=100)
        self.assertEqual(len(changes), 1)
        self.assertEqual(cursor, 'cur1')
        self.assertTrue(has_more)

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.get')
    def test_get_sync_changes_error(self, mock_get):
        mock_get.side_effect = Exception('Network error')
        from judge.utils.storage_client import get_sync_changes
        changes, cursor, has_more = get_sync_changes()
        self.assertIsNone(changes)
        self.assertFalse(has_more)

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.post')
    def test_request_download_url_success(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {'url': 'https://r2.example/file.zip', 'expires_at': '2024-01-01T00:03:00Z'},
        )
        from judge.utils.storage_client import request_download_url
        result = request_download_url('42')
        self.assertIsNotNone(result)
        self.assertEqual(result['url'], 'https://r2.example/file.zip')

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.post')
    def test_request_download_url_error(self, mock_post):
        mock_post.side_effect = Exception('Timeout')
        from judge.utils.storage_client import request_download_url
        result = request_download_url('42')
        self.assertIsNone(result)

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.get')
    def test_get_storage_volumes_unwraps_envelope(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                'items': [{'total_bytes': 100, 'free_bytes': 40, 'available_bytes': 30}],
                'next_cursor': None,
                'has_more': False,
            },
        )
        from judge.utils.storage_client import get_storage_volumes
        volumes = get_storage_volumes()
        self.assertEqual(volumes, [{'total_bytes': 100, 'free_bytes': 40, 'available_bytes': 30}])

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.get')
    def test_get_storage_volumes_accepts_legacy_single_volume(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {'total_bytes': 100, 'free_bytes': 40, 'available_bytes': 30},
        )
        from judge.utils.storage_client import get_storage_volumes
        self.assertEqual(get_storage_volumes()[0]['total_bytes'], 100)

    @override_settings(STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.post')
    def test_ensure_problem_ready_contract_states(self, mock_post):
        from judge.utils.storage_client import READY_STATE_NOT_READY, READY_STATE_READY, \
            READY_STATE_RESTORING, ensure_problem_ready

        mock_post.return_value = MagicMock(status_code=200, json=lambda: {'ready': True})
        self.assertEqual(ensure_problem_ready('42')['state'], READY_STATE_READY)

        mock_post.return_value = MagicMock(status_code=202, json=lambda: {'status': 'restoring', 'job_id': 'job-1'})
        restoring = ensure_problem_ready('42')
        self.assertEqual(restoring['state'], READY_STATE_RESTORING)
        self.assertEqual(restoring['job_id'], 'job-1')

        mock_post.return_value = MagicMock(status_code=409, json=lambda: {'status': 'not_ready'})
        self.assertEqual(ensure_problem_ready('42')['state'], READY_STATE_NOT_READY)

    @override_settings(
        STORAGE_SERVICE_TOKEN='',
        STORAGE_CLUEOJ_SERVICE_SECRET='secret-value',
        STORAGE_SERVICE_AUDIENCE='clueoj-storage',
        STORAGE_SERVICE_SCOPES='read,mutate,downloads:issue',
    )
    @patch('judge.utils.storage_client.requests.get')
    def test_service_token_is_minted_from_secret(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: {'items': []})
        from judge.utils import storage_client

        storage_client._service_token_cache['token'] = None
        storage_client._service_token_cache['refresh_at'] = 0
        storage_client.get_storage_volumes()

        auth = mock_get.call_args[1]['headers']['Authorization']
        self.assertTrue(auth.startswith('Bearer '))
        token = auth.split(' ', 1)[1]
        self.assertEqual(len(token.split('.')), 3)
        payload_part = token.split('.')[1]
        payload_part += '=' * (-len(payload_part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_part.encode('ascii')).decode('utf-8'))
        self.assertEqual(payload['kind'], 'service')
        self.assertEqual(payload['aud'], 'clueoj-storage')
        self.assertEqual(payload['scopes'], ['read', 'mutate', 'downloads:issue'])

    @override_settings(STORAGE_CATALOG_SYNC_ENABLED=True, STORAGE_SERVICE_TOKEN='test-token')
    @patch('judge.utils.storage_client.requests.post')
    def test_notify_problem_dirty_logs_non_2xx(self, mock_post):
        response = MagicMock(status_code=401)
        response.raise_for_status.side_effect = Exception('Unauthorized')
        mock_post.return_value = response
        from judge.utils.storage_client import notify_problem_dirty
        self.assertIsNone(notify_problem_dirty('123', 'testcode'))
        response.raise_for_status.assert_called_once()


class StorageSyncTaskTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = create_organization('SyncOrg', 'syncorg')
        cls.problem = create_problem('sync_prob')

    @patch('judge.utils.storage_client.get_storage_volumes')
    @patch('judge.utils.storage_client.get_sync_changes')
    def test_sync_catalog_processes_changes(self, mock_get_changes, mock_volumes):
        from judge.tasks.storage import storage_sync_catalog, _apply_sync_change

        mock_volumes.return_value = None
        mock_get_changes.return_value = (
            [{
                'external_id': str(self.problem.pk),
                'code': 'sync_prob',
                'owner_organization_id': self.org.pk,
                'logical_bytes': 2048,
                'allocated_bytes': 4096,
                'archive_bytes': 1024,
                'auxiliary_bytes': 1024,
                'file_count': 2,
                'quota_bytes': 8192,
                'local_status': 'present',
                'r2_status': 'ready',
                'snapshot_generation': 1,
                'downloadable': True,
                'catalog_state': 'present',
                'observed_at': '2024-01-01T00:00:00Z',
                'stale': False,
            }],
            'cur1',
            False,
        )

        storage_sync_catalog()

        usage = StorageProblemUsage.objects.get(problem=self.problem)
        self.assertEqual(usage.logical_bytes, 2048)
        self.assertEqual(usage.r2_status, 'READY')
        self.assertTrue(usage.downloadable)
        self.assertFalse(usage.stale)
        self.assertEqual(StorageSystemStatus.objects.get(id=1).sync_cursor, 'cur1')

    def test_sync_catalog_skips_if_locked(self):
        from judge.tasks.storage import storage_sync_catalog
        from judge.models.storage import StorageSyncLease
        from django.utils import timezone
        StorageSyncLease.objects.create(
            name='catalog',
            owner='other-worker',
            expires_at=timezone.now() + timezone.timedelta(minutes=5),
        )
        result = storage_sync_catalog()
        self.assertIsNone(result)

    def test_mark_stale(self):
        from judge.tasks.storage import storage_mark_stale
        usage = StorageProblemUsage.objects.create(
            problem=self.problem, code='sync_prob', stale=False
        )
        storage_mark_stale()
        usage.refresh_from_db()
        self.assertTrue(usage.stale)

    @patch('judge.utils.storage_client.get_storage_volumes')
    @patch('judge.utils.storage_client.get_sync_changes')
    def test_sync_catalog_idempotent(self, mock_get_changes, mock_volumes):
        from judge.tasks.storage import storage_sync_catalog

        mock_volumes.return_value = None
        mock_get_changes.return_value = (
            [{
                'external_id': str(self.problem.pk),
                'code': 'sync_prob',
                'logical_bytes': 1024,
                'catalog_state': 'present',
                'observed_at': '2024-01-01T00:00:00Z',
                'stale': False,
            }],
            None,
            False,
        )

        storage_sync_catalog()
        storage_sync_catalog()

        usage = StorageProblemUsage.objects.get(problem=self.problem)
        self.assertEqual(usage.logical_bytes, 1024)
        self.assertEqual(StorageProblemUsage.objects.count(), 1)

    @patch('judge.utils.storage_client.get_sync_changes')
    def test_sync_cursor_not_committed_when_missing_problem_retries(self, mock_get_changes):
        from judge.tasks.storage import storage_sync_catalog

        mock_get_changes.return_value = (
            [{'change_id': 'missing-1', 'external_id': '999999', 'code': 'missing'}],
            'cur-missing',
            False,
        )

        storage_sync_catalog()

        self.assertEqual(StorageSystemStatus.objects.get(id=1).sync_cursor, '')
        self.assertEqual(StorageSyncDeadLetter.objects.get(change_key='missing-1').retry_count, 1)

    @patch('judge.utils.storage_client.get_storage_volumes')
    @patch('judge.utils.storage_client.get_sync_changes')
    def test_sync_cursor_commits_after_deadletter_threshold(self, mock_get_changes, mock_volumes):
        from judge.tasks.storage import storage_sync_catalog

        mock_volumes.return_value = None
        mock_get_changes.return_value = (
            [{'change_id': 'missing-final', 'external_id': '999999', 'code': 'missing'}],
            'cur-final',
            False,
        )

        for _ in range(3):
            storage_sync_catalog()

        self.assertEqual(StorageSystemStatus.objects.get(id=1).sync_cursor, 'cur-final')
        self.assertEqual(StorageSyncDeadLetter.objects.get(change_key='missing-final').retry_count, 3)

    @patch('judge.utils.storage_client.get_storage_volumes')
    @patch('judge.utils.storage_client.get_sync_changes')
    def test_sync_aborts_if_lease_lost_before_commit(self, mock_get_changes, mock_volumes):
        from django.utils import timezone
        from judge.models.storage import StorageSyncLease
        from judge.tasks.storage import StorageSyncLeaseLost, storage_sync_catalog

        mock_volumes.return_value = None

        def steal_lease(*args, **kwargs):
            StorageSyncLease.objects.update(
                owner='other-worker',
                expires_at=timezone.now() + timezone.timedelta(minutes=5),
            )
            return (
                [{'external_id': str(self.problem.pk), 'code': 'sync_prob', 'logical_bytes': 99}],
                'cur-stolen',
                False,
            )

        mock_get_changes.side_effect = steal_lease

        with self.assertRaises(StorageSyncLeaseLost):
            storage_sync_catalog.run()

        self.assertFalse(StorageProblemUsage.objects.filter(problem=self.problem).exists())
        self.assertEqual(StorageSystemStatus.objects.get(id=1).sync_cursor, '')

    @patch('judge.utils.storage_client.get_sync_changes')
    def test_sync_malformed_change_does_not_advance_cursor(self, mock_get_changes):
        from judge.tasks.storage import StorageSyncMalformedChange, storage_sync_catalog

        mock_get_changes.return_value = ([{'code': 'broken'}], 'cur-broken', False)

        with self.assertRaises(StorageSyncMalformedChange):
            storage_sync_catalog.run()

        self.assertEqual(StorageSystemStatus.objects.get(id=1).sync_cursor, '')

    def test_apply_delete_event_kind_marks_tombstone(self):
        from judge.tasks.storage import _apply_sync_change

        StorageProblemUsage.objects.create(
            problem=self.problem,
            code='sync_prob',
            owner_organization_id=self.org.pk,
            r2_status='READY',
            downloadable=True,
            stale=True,
        )

        _apply_sync_change({
            'event_kind': 'delete',
            'external_id': str(self.problem.pk),
            'code': 'sync_prob',
            'owner_organization_id': self.org.pk,
            'deleted_at': '2024-01-01T00:00:00Z',
        })

        usage = StorageProblemUsage.objects.get(problem=self.problem)
        self.assertEqual(usage.catalog_state, 'deleted')
        self.assertFalse(usage.downloadable)
        self.assertFalse(usage.stale)
        self.assertIsNotNone(usage.deleted_at)

    @patch('judge.utils.storage_client.get_storage_volumes')
    def test_update_system_status_uses_unwrapped_volume_envelope(self, mock_volumes):
        from judge.tasks.storage import _update_system_status

        mock_volumes.return_value = [{'total_bytes': 100, 'free_bytes': 40, 'available_bytes': 30}]
        _update_system_status()

        status = StorageSystemStatus.objects.get(id=1)
        self.assertEqual(status.volume_total_bytes, 100)
        self.assertEqual(status.volume_free_bytes, 40)
        self.assertEqual(status.volume_available_bytes, 30)

    @patch('judge.utils.storage_client.get_organization_usage')
    def test_rebuild_organization_usage_uses_authoritative_quota(self, mock_org_usage):
        from judge.tasks.storage import _rebuild_organization_usage

        mock_org_usage.return_value = {
            'quota_bytes': 123456,
            'problem_count_quota': 9,
            'logical_bytes': 3072,
            'allocated_bytes': 4096,
            'archive_bytes': 2048,
            'auxiliary_bytes': 1024,
            'file_count': 6,
            'problem_count': 2,
            'orphan_bytes': 99,
            'referenced_bytes': 77,
        }
        StorageProblemUsage.objects.create(
            problem=self.problem,
            code='sync_prob',
            owner_organization_id=self.org.pk,
            allocated_bytes=2048,
            quota_bytes=0,
            orphan_bytes=1,
            referenced_bytes=2,
            catalog_state='present',
        )

        _rebuild_organization_usage()

        usage = StorageOrganizationUsage.objects.get(organization=self.org)
        self.assertEqual(usage.quota_bytes, 123456)
        self.assertEqual(usage.problem_quota, 9)
        self.assertEqual(usage.total_logical_bytes, 3072)
        self.assertEqual(usage.total_allocated_bytes, 4096)
        self.assertEqual(usage.total_archive_bytes, 2048)
        self.assertEqual(usage.total_auxiliary_bytes, 1024)
        self.assertEqual(usage.total_file_count, 6)
        self.assertEqual(usage.problem_count, 2)
        self.assertEqual(usage.orphan_bytes, 99)
        self.assertEqual(usage.referenced_bytes, 77)

    @patch('judge.models.problem.problem_data_storage.rename')
    def test_problem_rename_rolls_back_db_code_on_storage_failure(self, mock_rename):
        mock_rename.side_effect = OSError(errno.EIO, 'disk error')
        ProblemData.objects.filter(problem=self.problem).delete()
        original_code = self.problem.code
        self.problem.code = 'sync_prob_renamed'

        with self.assertRaises(OSError):
            self.problem.save()

        self.problem.refresh_from_db()
        self.assertEqual(self.problem.code, original_code)

    @patch('judge.utils.storage_client.get_organization_usage')
    def test_rebuild_organization_usage_refreshes_empty_org_quota(self, mock_org_usage):
        from judge.tasks.storage import _rebuild_organization_usage

        StorageOrganizationUsage.objects.create(organization=self.org, quota_bytes=5000, problem_quota=3)
        mock_org_usage.return_value = {
            'quota_bytes': 7000,
            'problem_count_quota': 4,
            'logical_bytes': 10,
            'allocated_bytes': 20,
            'archive_bytes': 7,
            'auxiliary_bytes': 3,
            'file_count': 2,
            'problem_count': 1,
        }

        _rebuild_organization_usage()

        usage = StorageOrganizationUsage.objects.get(organization=self.org)
        self.assertEqual(usage.quota_bytes, 7000)
        self.assertEqual(usage.problem_quota, 4)
        self.assertEqual(usage.total_logical_bytes, 10)
        self.assertEqual(usage.total_allocated_bytes, 20)
        self.assertEqual(usage.total_archive_bytes, 7)
        self.assertEqual(usage.total_auxiliary_bytes, 3)
        self.assertEqual(usage.total_file_count, 2)
        self.assertEqual(usage.problem_count, 1)


class ProblemStorageOwnerTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = create_organization('OwnerOrg', 'ownerorg')
        cls.problem = create_problem('owner_prob')

    def test_storage_owner_organization_field_exists(self):
        self.assertTrue(hasattr(self.problem, 'storage_owner_organization'))
        self.assertIsNone(self.problem.storage_owner_organization)

    def test_set_storage_owner(self):
        self.problem.storage_owner_organization = self.org
        self.problem.save()
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.storage_owner_organization, self.org)


class ProblemDataAtomicStorageTestCase(TestCase):
    def _zip_bytes(self, name='a.in', data=b'1'):
        out = BytesIO()
        with zipfile.ZipFile(out, 'w') as zf:
            zf.writestr(name, data)
        return out.getvalue()

    @override_settings(DMOJ_PROBLEM_DATA_ROOT=None)
    def test_same_filename_upload_keeps_new_file(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(DMOJ_PROBLEM_DATA_ROOT=root, STORAGE_CATALOG_SYNC_ENABLED=False):
                problem_data_storage.location = root
                problem = create_problem('samezip')
                data = ProblemData.objects.create(problem=problem)
                data.zipfile.save('samezip/tests.zip', ContentFile(self._zip_bytes(data=b'old')), save=True)
                data.refresh_from_db()
                data.zipfile.save('samezip/tests.zip', ContentFile(self._zip_bytes(data=b'new')), save=True)

                self.assertTrue(os.path.exists(os.path.join(root, 'samezip', 'tests.zip')))
                with zipfile.ZipFile(data.zipfile.path) as zf:
                    self.assertEqual(zf.read('a.in'), b'new')

    @override_settings(DMOJ_PROBLEM_DATA_ROOT=None)
    def test_invalid_zip_does_not_replace_old_archive(self):
        with tempfile.TemporaryDirectory() as root:
            with override_settings(DMOJ_PROBLEM_DATA_ROOT=root, STORAGE_CATALOG_SYNC_ENABLED=False):
                problem_data_storage.location = root
                problem = create_problem('badzip')
                data = ProblemData.objects.create(problem=problem)
                data.zipfile.save('badzip/tests.zip', ContentFile(self._zip_bytes(data=b'old')), save=True)
                old_content = open(data.zipfile.path, 'rb').read()

                with self.assertRaises(Exception):
                    data.zipfile.save('badzip/tests.zip', ContentFile(b'not a zip'), save=True)

                self.assertEqual(open(os.path.join(root, 'badzip', 'tests.zip'), 'rb').read(), old_content)


class StorageDownloadAndUiTestCase(TestCase):
    def setUp(self):
        self.user = create_user('download_admin', user_permissions=['edit_own_problem'])
        self.org = create_organization('Download Org', 'download-org', admins=['download_admin'])
        self.problem = create_problem('download_prob', authors=['download_admin'])
        self.problem.organizations.add(self.org)
        self.problem.storage_owner_organization = self.org
        self.problem.save()

    @override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True, STORAGE_SERVICE_TOKEN='token')
    @patch('judge.utils.storage_client.request_download_url')
    def test_direct_download_requires_ready_projection_and_redirects(self, mock_download):
        mock_download.return_value = {'url': 'https://r2.example/tests.zip'}
        StorageProblemUsage.objects.create(
            problem=self.problem,
            code=self.problem.code,
            owner_organization_id=self.org.pk,
            r2_status='ready',
            downloadable=True,
        )
        data = ProblemData.objects.create(problem=self.problem)
        data.zipfile.name = '%s/tests.zip' % self.problem.code
        data.save()
        self.client.force_login(self.user)

        response = self.client.get(reverse('problem_data_archive', args=[self.problem.code]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://r2.example/tests.zip')

    @override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True, STORAGE_SERVICE_TOKEN='token')
    @patch('judge.utils.storage_client.request_download_url')
    def test_direct_download_local_missing_is_404_when_r2_fails(self, mock_download):
        mock_download.return_value = None
        StorageProblemUsage.objects.create(
            problem=self.problem,
            code=self.problem.code,
            owner_organization_id=self.org.pk,
            r2_status='READY',
            downloadable=True,
        )
        data = ProblemData.objects.create(problem=self.problem)
        data.zipfile.name = '%s/tests.zip' % self.problem.code
        data.save()
        self.client.force_login(self.user)

        response = self.client.get(reverse('problem_data_archive', args=[self.problem.code]))

        self.assertEqual(response.status_code, 404)

    @override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True, STORAGE_SERVICE_TOKEN='token')
    @patch('judge.utils.storage_client.request_download_url')
    def test_direct_download_missing_local_archive_still_redirects_when_r2_ready(self, mock_download):
        mock_download.return_value = {'url': 'https://r2.example/latest.zip'}
        StorageProblemUsage.objects.create(
            problem=self.problem,
            code=self.problem.code,
            owner_organization_id=self.org.pk,
            r2_status='READY',
            downloadable=True,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('problem_data_archive', args=[self.problem.code]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], 'https://r2.example/latest.zip')

    def test_owner_accounting_and_ui(self):
        from judge.tasks.storage import _rebuild_organization_usage

        StorageProblemUsage.objects.create(
            problem=self.problem,
            code=self.problem.code,
            owner_organization_id=self.org.pk,
            allocated_bytes=4096,
            archive_bytes=3072,
            auxiliary_bytes=1024,
            quota_bytes=8192,
            referenced_bytes=512,
            r2_status='ready',
            downloadable=True,
            stale=False,
        )
        _rebuild_organization_usage()
        self.client.force_login(self.user)

        response = self.client.get(reverse('organization_storage', args=[self.org.slug]))

        self.assertContains(response, 'Storage overview')
        self.assertContains(response, self.problem.code)
        self.assertContains(response, '4.0 KB')
        self.assertContains(response, '8.0 KB')


class StorageEnsureReadySubmissionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_user('ready_user')
        cls.problem = create_problem('ready_prob')
        cls.language = Language.objects.create(
            key='PY3',
            name='Python 3',
            short_name='PY3',
            common_name='Python',
            ace='python',
            pygments='python',
            extension='py',
        )

    def setUp(self):
        cache.clear()

    def _submission(self):
        return Submission.objects.create(
            user=self.user.profile,
            problem=self.problem,
            language=self.language,
        )

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    @patch('judge.tasks.storage.storage_retry_judge_submission.apply_async')
    @patch('judge.utils.storage_client.ensure_problem_ready')
    def test_restoring_keeps_submission_queued_and_enqueues_retry(self, mock_ready, mock_retry):
        mock_ready.return_value = {'ready': False, 'state': 'restoring', 'job_id': 'job-1'}
        submission = self._submission()

        with self.captureOnCommitCallbacks(execute=True):
            submission.judge()

        submission.refresh_from_db()
        self.assertEqual(submission.status, 'QU')
        self.assertIsNone(submission.judged_date)
        mock_retry.assert_called_once()
        self.assertEqual(mock_retry.call_args[1]['args'], [submission.pk])

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    @patch('judge.models.submission.judge_submission')
    @patch('judge.utils.storage_client.ensure_problem_ready')
    def test_ready_dispatches_judge(self, mock_ready, mock_dispatch):
        mock_ready.return_value = {'ready': True, 'state': 'ready'}
        mock_dispatch.return_value = True
        submission = self._submission()

        submission.judge()

        mock_dispatch.assert_called_once()
        submission.refresh_from_db()
        self.assertEqual(submission.status, 'P')

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True, STORAGE_ENSURE_READY_MAX_ATTEMPTS=1)
    @patch('judge.utils.storage_client.ensure_problem_ready')
    def test_restore_timeout_is_terminal_after_policy(self, mock_ready):
        mock_ready.return_value = {'ready': False, 'state': 'restoring'}
        submission = self._submission()

        submission.judge(ensure_ready_attempt=1)

        submission.refresh_from_db()
        self.assertEqual(submission.status, 'IE')
        self.assertIn('restore did not complete', submission.error)

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True, STORAGE_ENSURE_READY_DEGRADED_DISPATCH=True)
    @patch('judge.models.submission.judge_submission')
    @patch('judge.models.submission._problem_data_local_usable')
    @patch('judge.utils.storage_client.ensure_problem_ready')
    def test_unavailable_can_degrade_to_dispatch_when_local_data_exists(self, mock_ready, mock_local, mock_dispatch):
        mock_ready.return_value = {'ready': False, 'state': 'unavailable'}
        mock_local.return_value = True
        mock_dispatch.return_value = True
        submission = self._submission()

        submission.judge()

        mock_dispatch.assert_called_once()

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True, STORAGE_ENSURE_READY_DEGRADED_DISPATCH=True)
    @patch('judge.tasks.storage.storage_retry_judge_submission.apply_async')
    @patch('judge.models.submission._problem_data_local_usable')
    @patch('judge.utils.storage_client.ensure_problem_ready')
    def test_unavailable_without_local_data_retries_instead_of_dispatch(self, mock_ready, mock_local, mock_retry):
        mock_ready.return_value = {'ready': False, 'state': 'unavailable'}
        mock_local.return_value = False
        submission = self._submission()

        with self.captureOnCommitCallbacks(execute=True):
            submission.judge()

        submission.refresh_from_db()
        self.assertEqual(submission.status, 'QU')
        mock_retry.assert_called_once()

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    @patch('judge.models.submission.judge_submission')
    @patch('judge.utils.storage_client.ensure_problem_ready')
    def test_duplicate_retry_does_not_dispatch_already_processing_submission(self, mock_ready, mock_dispatch):
        mock_ready.return_value = {'ready': True, 'state': 'ready'}
        submission = self._submission()
        submission.status = 'P'
        submission.save(update_fields=['status'])

        submission.judge(force_judge=True, ensure_ready_attempt=2)

        mock_ready.assert_not_called()
        mock_dispatch.assert_not_called()
