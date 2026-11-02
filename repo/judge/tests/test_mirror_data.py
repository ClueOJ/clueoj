import io
import os
import shutil
import zipfile
from datetime import date
from unittest.mock import Mock, patch

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from judge.models import ExternalProblem, Language, Problem, ProblemData, ProblemTestCase, Submission, SubmissionSource, problem_data_storage
from judge.models.tests.util import create_organization, create_problem, create_user
from judge.utils.batch_scoring import save_batch_policies
from judge.utils.polygon_import import import_polygon_package
from judge.utils.problem_data import ProblemDataCompiler, get_problem_testcases_data, problem_data_lock
from judge.utils.problem_mirror import test_data_source, validate_mirror_source_for_target
from judge.utils.test_data_lock import require_judging_allowed, unlock_tests


class MirrorDataTests(TestCase):
    fixtures = ['language_all.json']

    def setUp(self):
        for code in ('mirror_root', 'mirror_other', 'mirror_child'):
            shutil.rmtree(problem_data_storage.path(code), ignore_errors=True)

        self.author = create_user(username='mirroradmin', is_superuser=True, is_staff=True)
        self.client.force_login(self.author)
        self.root = self.dataset('mirror_root', '1')
        self.other = self.dataset('mirror_other', '2')
        self.mirror = create_problem(code='mirror_child', mirror_of=self.root, is_public=True,
                                     authors=('mirroradmin',), points=100)
        self.url = reverse('problem_data', args=[self.mirror.code])

    def dataset(self, code, output):
        problem = create_problem(code=code, is_public=True, points=100, authors=('mirroradmin',))
        data = ProblemData.objects.create(problem=problem, archive_source_problem=problem)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('1.in', '1\n')
            z.writestr('1.out', output + '\n')
        data.zipfile.save('tests.zip', ContentFile(archive.getvalue()))
        ProblemTestCase.objects.create(dataset=problem, order=1, type='C', input_file='1.in',
                                       output_file='1.out', points=100, is_pretest=False)
        ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order'), ['1.in', '1.out'])
        return problem

    def local_payload(self, output='3'):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('1.in', '1\n')
            z.writestr('1.out', output + '\n')
        return {
            'mirror-test_source': 'local', 'mirror-mirror_of': '',
            'problem-data-checker': 'standard', 'problem-data-checker_type': 'default',
            'problem-data-grader': 'standard', 'problem-data-io_method': 'standard',
            'problem-data-zipfile': SimpleUploadedFile('own.zip', archive.getvalue()),
            'cases-TOTAL_FORMS': '1', 'cases-INITIAL_FORMS': '0',
            'cases-MIN_NUM_FORMS': '0', 'cases-MAX_NUM_FORMS': '1000',
            'cases-0-type': 'C', 'cases-0-order': '1', 'cases-0-input_file': '1.in',
            'cases-0-output_file': '1.out', 'cases-0-points': '100',
        }

    def unlock(self, problem=None):
        Problem.objects.filter(pk=(problem or self.mirror).pk).update(is_locked=False)

    def test_new_mirror_has_no_copied_files_or_rows(self):
        self.assertFalse(self.mirror.cases.exists())
        self.assertFalse(ProblemData.objects.filter(problem=self.mirror).exists())
        self.assertFalse(os.path.exists(problem_data_storage.path(self.mirror.code)))
        self.assertEqual(get_problem_testcases_data(self.mirror)[1]['answer'], '1\n')

    def test_get_does_not_create_or_heal_legacy_data(self):
        data = ProblemData.objects.create(problem=self.mirror, zipfile='mirror_child/old.zip',
                                          archive_source_problem=self.root, checker='bridged')
        case = ProblemTestCase.objects.create(dataset=self.mirror, order=1, type='C', points=7,
                                              input_file='missing.in', is_pretest=False)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(data.zipfile.name, 'mirror_child/old.zip')
        self.assertEqual(case.input_file, 'missing.in')
        self.assertEqual(response.context['cases_formset'].total_form_count(), 0)

    def test_archived_root_does_not_hide_the_new_local_dataset_table(self):
        self.unlock()
        with patch('judge.views.problem_data.ProblemDataView._archived_usage', return_value=Mock(problem_id=self.root.pk)):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="case-table"')
        self.assertContains(response, 'id="id_problem-data-zipfile"')

    def test_hidden_checker_fields_do_not_change_mirror_policy_or_files(self):
        data = ProblemData.objects.create(problem=self.mirror, legacy_batch_type='min',
                                          batch_scoring_overrides={'full:1': 'sum'}, feedback='old error')
        self.unlock()
        with patch('judge.views.problem_data.ProblemDataCompiler.generate') as generate:
            response = self.client.post(self.url, {
                'mirror-test_source': 'mirror', 'mirror-mirror_of': self.root.pk,
                'problem-data-checker': 'invalid', 'cases-TOTAL_FORMS': 'invalid',
            })
        self.assertEqual(response.status_code, 302)
        generate.assert_not_called()
        data.refresh_from_db()
        self.assertEqual(data.legacy_batch_type, 'min')
        self.assertEqual(data.batch_scoring_overrides, {'full:1': 'sum'})

    def test_mirror_policy_edit_is_rejected_without_writing_root_files(self):
        ProblemTestCase.objects.create(dataset=self.root, order=0, type='S', points=100, is_pretest=False)
        ProblemTestCase.objects.create(dataset=self.root, order=2, type='E', is_pretest=False)
        with patch('judge.models.problem_data.problem_data_lock') as lock:
            with self.assertRaises(ValidationError):
                save_batch_policies(self.mirror, {'batch-mode-full:1': 'sum'})
        lock.assert_not_called()
        self.assertFalse(ProblemData.objects.filter(problem=self.mirror).exists())
        self.assertEqual(self.root.cases.get(type='S').batch_type, 'sum')

    def test_superuser_cannot_edit_mirror_batch_policies(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'id="batch-scoring-form"')
        self.assertEqual(page.context['batch_policy_rows'], [])
        self.unlock()
        with patch('judge.utils.test_data_lock.abort_test_submissions'):
            response = self.client.post(self.url, {'action': 'save-batch-scoring', 'batch-mode-full:1': 'sum'})
        self.assertEqual(response.status_code, 400)
        self.mirror.refresh_from_db()
        self.assertFalse(ProblemData.objects.filter(problem=self.mirror).exists())

    def test_switch_root_leaves_sibling_and_both_root_files_unchanged(self):
        child = create_problem(code='mirror_sibling', mirror_of=self.root)
        self.unlock()
        with problem_data_lock('problem:%s' % self.root.pk), patch(
                'judge.views.problem_data.ProblemDataCompiler.generate') as generate:
            response = self.client.post(self.url, {
                'mirror-test_source': 'mirror', 'mirror-mirror_of': self.other.pk,
            })
        self.assertEqual(response.status_code, 302)
        generate.assert_not_called()
        child.refresh_from_db()
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.mirror_root_id, self.other.pk)
        self.assertEqual(child.mirror_root_id, self.root.pk)
        self.assertEqual(get_problem_testcases_data(self.mirror)[1]['answer'], '2\n')
        self.assertEqual(get_problem_testcases_data(child)[1]['answer'], '1\n')
        self.assertEqual(get_problem_testcases_data(self.root)[1]['answer'], '1\n')

    def test_switch_source_for_same_organization_private_problems(self):
        org = create_organization(name='mirror_private_org', slug='mirror-private-org', paid_until=date(2100, 1, 1))
        org.admins.add(self.author.profile)
        for problem in (self.root, self.other, self.mirror):
            Problem.objects.filter(pk=problem.pk).update(is_public=False, is_organization_private=True)
            problem.organizations.add(org)
        self.unlock()
        response = self.client.post(self.url, {
            'mirror-test_source': 'mirror', 'mirror-mirror_of': self.other.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.mirror_root_id, self.other.pk)

    def test_locked_problem_rejects_source_edit_in_model_and_form(self):
        self.mirror.mirror_of = self.other
        with self.assertRaises(ValidationError):
            self.mirror.save()
        response = self.client.post(self.url, {
            'mirror-test_source': 'mirror', 'mirror-mirror_of': self.other.pk,
        })
        self.assertEqual(response.status_code, 409)

    def test_partial_model_save_keeps_cached_root_in_sync(self):
        self.unlock()
        self.mirror.mirror_of = self.other
        self.mirror.save(update_fields=['mirror_of'])
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.mirror_root_id, self.other.pk)

    def test_local_to_mirror_only_saves_source(self):
        self.unlock(self.other)
        response = self.client.post(reverse('problem_data', args=[self.other.code]), {
            'mirror-test_source': 'mirror', 'mirror-mirror_of': self.root.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.other.refresh_from_db()
        self.assertEqual(test_data_source(self.other).pk, self.root.pk)
        self.assertEqual(get_problem_testcases_data(self.other)[1]['answer'], '1\n')

    def test_switch_to_local_replaces_inactive_legacy_rows(self):
        ProblemTestCase.objects.create(dataset=self.mirror, order=9, points=999, is_pretest=False)
        ProblemData.objects.create(problem=self.mirror, zipfile='mirror_child/old.zip',
                                   archive_source_problem=self.root, checker='bridged')
        self.unlock()
        response = self.client.post(self.url, self.local_payload())
        self.assertEqual(response.status_code, 302)
        self.mirror.refresh_from_db()
        self.assertIsNone(self.mirror.mirror_of_id)
        self.assertTrue(self.mirror.is_locked)
        self.assertEqual(list(self.mirror.cases.values_list('order', flat=True)), [1])
        self.assertEqual(get_problem_testcases_data(self.mirror)[1]['answer'], '3\n')
        self.assertEqual(get_problem_testcases_data(self.root)[1]['answer'], '1\n')

    def test_failed_compile_keeps_mirror_relation_unlocked(self):
        self.unlock()
        with patch('judge.views.problem_data.ProblemDataCompiler.generate', side_effect=ValidationError('bad tests')):
            response = self.client.post(self.url, self.local_payload())
        self.assertEqual(response.status_code, 409)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.mirror_of_id, self.root.pk)
        self.assertFalse(self.mirror.is_locked)
        self.assertFalse(ProblemData.objects.filter(problem=self.mirror).exists())
        self.assertEqual(get_problem_testcases_data(self.root)[1]['answer'], '1\n')

    def test_detached_foreign_dataset_can_be_replaced_with_valid_local_data(self):
        ProblemData.objects.create(problem=self.mirror, zipfile='mirror_child/old.zip',
                                   archive_source_problem=self.root)
        ProblemTestCase.objects.create(dataset=self.mirror, order=9, points=999, is_pretest=False)
        self.unlock()
        self.mirror.mirror_of = None
        self.mirror.save()
        response = self.client.post(self.url, self.local_payload())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.mirror.cases.values_list('order', flat=True)), [1])
        self.mirror.refresh_from_db()
        self.assertEqual(get_problem_testcases_data(self.mirror)[1]['answer'], '3\n')

    def test_indirect_virtual_judge_source_is_rejected(self):
        from judge.views.problem_data import ProblemMirrorForm
        ExternalProblem.objects.create(problem=self.root, config=None, oj='vjudge',
                                        external_problem_id='external-root', is_active=True)
        form = ProblemMirrorForm(instance=self.other, user=self.author)
        self.assertNotIn(self.mirror.pk, form.fields['mirror_of'].queryset.values_list('pk', flat=True))
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(self.author, self.mirror, target_problem=self.other)

    def test_mirror_cannot_make_private_root_answers_public(self):
        reader = create_user(username='mirrorreader')
        Problem.objects.filter(pk=self.root.pk).update(testcase_visibility_mode='O')
        Problem.objects.filter(pk=self.mirror.pk).update(testcase_visibility_mode='A')
        sub = Submission.objects.create(problem=self.mirror, user=reader.profile, language=Language.get_python3(),
                                         status='D', result='AC', points=100)
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        self.client.force_login(reader)
        with patch('judge.views.submission.get_problem_testcases_data') as read:
            response = self.client.get(reverse('submission_status', args=[sub.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['can_view_test'])
        read.assert_not_called()

    def test_unlock_root_aborts_and_pauses_direct_mirrors(self):
        child = create_problem(code='mirror_sibling', mirror_of=self.root)
        sub = Submission.objects.create(problem=child, user=self.author.profile,
                                         language=Language.get_python3(), status='G')
        with patch('judge.utils.test_data_lock.abort_test_submissions'):
            unlock_tests(self.root.pk)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')
        with self.assertRaises(ValidationError):
            require_judging_allowed(child.pk)
        require_judging_allowed(self.other.pk)

    def test_download_and_yaml_follow_root_even_with_poisoned_local_copy(self):
        problem_data_storage.save(self.mirror.code + '/init.yml', ContentFile(b'poisoned: true'))
        response = self.client.get(reverse('problem_data_init', args=[self.mirror.code]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'poisoned')
        data = ProblemData.objects.get(problem=self.root)
        name = os.path.basename(data.zipfile.name)
        response = self.client.get(reverse('problem_data_file', args=[self.mirror.code, name]))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(reverse('problem_data_file', args=[self.mirror.code, '../init.yml']))
        self.assertEqual(response.status_code, 404)

    def test_switch_to_local_retires_foreign_files_and_blocks_stale_r2_entries(self):
        from judge.models import StorageProblemUsage
        owner = create_user(username='mirror_only_owner', user_permissions=('edit_own_problem',))
        self.mirror.authors.add(owner.profile)
        foreign = io.BytesIO()
        with zipfile.ZipFile(foreign, 'w') as archive:
            archive.writestr('secret.out', 'ROOT-PRIVATE-ANSWER')
        name = problem_data_storage.save(self.mirror.code + '/foreign.zip', ContentFile(foreign.getvalue()))
        problem_data_storage.save(self.mirror.code + '/old_checker.cpp', ContentFile('ROOT-PRIVATE-CHECKER'))
        ProblemData.objects.create(problem=self.mirror, zipfile=name, archive_source_problem=self.root)
        usage = StorageProblemUsage.objects.create(problem=self.mirror, code=self.mirror.code,
                                                   r2_status='READY', downloadable=True)
        old_url = reverse('problem_data_file', args=[self.mirror.code, 'foreign.zip'])
        self.client.force_login(owner)
        self.assertEqual(self.client.get(old_url).status_code, 404)
        self.unlock()
        response = self.client.post(self.url, self.local_payload())
        self.assertEqual(response.status_code, 302)
        data = ProblemData.objects.get(problem=self.mirror)
        self.assertNotEqual(os.path.basename(data.zipfile.name), 'own.zip')
        self.assertFalse(problem_data_storage.exists(name))
        usage.refresh_from_db()
        self.assertFalse(usage.downloadable)
        # Even a stale READY projection must not issue a URL for foreign files.
        StorageProblemUsage.objects.filter(pk=usage.pk).update(downloadable=True)
        with override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True), patch(
                'judge.utils.storage_client.request_download_url', return_value={'url': 'https://example.invalid/old'}) as issue:
            self.assertEqual(self.client.get(old_url).status_code, 404)
            self.assertEqual(self.client.get(reverse('problem_data_file', args=[self.mirror.code, 'old_checker.cpp'])).status_code, 404)
            self.assertEqual(self.client.get(reverse('problem_data_archive', args=[self.mirror.code])).status_code, 200)
            issue.assert_not_called()
        # A missing archive is requested by its new unique path, never by the
        # old snapshot's default/canonical ZIP.
        problem_data_storage.delete(data.zipfile.name)
        with override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True), patch(
                'judge.utils.storage_client.request_download_url', return_value=None) as issue:
            self.assertEqual(self.client.get(reverse('problem_data_archive', args=[self.mirror.code])).status_code, 404)
            issue.assert_called_once_with(str(self.mirror.pk), path=os.path.basename(data.zipfile.name))
        self.assertEqual(get_problem_testcases_data(self.root)[1]['answer'], '1\n')

    @override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True)
    def test_r2_archive_download_uses_root_id(self):
        data = ProblemData.objects.get(problem=self.root)
        problem_data_storage.delete(data.zipfile.name)
        with patch('judge.views.problem_data._storage_projection_ready_for_download', return_value=True), patch(
                'judge.utils.storage_client.request_download_url', return_value={'url': 'https://example.invalid/test.zip'}) as download:
            response = self.client.get(reverse('problem_data_archive', args=[self.mirror.code]))
        self.assertEqual(response.status_code, 302)
        download.assert_called_once_with(str(self.root.pk), path=os.path.basename(data.zipfile.name))

    @override_settings(STORAGE_DIRECT_DOWNLOAD_ENABLED=True)
    def test_r2_checker_download_uses_root_id_and_relative_path(self):
        data = ProblemData.objects.get(problem=self.root)
        data.custom_checker.name = self.root.code + '/checker.cpp'
        data.save()
        with patch('judge.views.problem_data._storage_projection_ready_for_download', return_value=True), patch(
                'judge.utils.storage_client.request_download_url', return_value={'url': 'https://example.invalid/checker'}) as download:
            response = self.client.get(reverse('problem_data_file', args=[self.mirror.code, 'checker.cpp']))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(download.call_args_list[-1].args, (str(self.root.pk),))
        self.assertEqual(download.call_args_list[-1].kwargs, {'path': 'checker.cpp'})
        self.assertEqual(download.call_args_list[0].kwargs, {'path': os.path.basename(data.zipfile.name)})

    def polygon_package(self, output='4'):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('problem.xml', '''<problem><assets><checker type="testlib" name="std::wcmp.cpp"/></assets><judging><testset name="tests">
                <time-limit>1000</time-limit><memory-limit>67108864</memory-limit>
                <input-path-pattern>tests/%02d</input-path-pattern>
                <answer-path-pattern>tests/%02d.a</answer-path-pattern>
                <tests><test points="100"/></tests></testset></judging></problem>''')
            z.writestr('tests/01', '1\n')
            z.writestr('tests/01.a', output + '\n')
        stream.seek(0)
        return stream

    def test_polygon_import_detaches_mirror_and_keeps_sibling_on_root(self):
        child = create_problem(code='polygon_sibling', mirror_of=self.root)
        self.unlock()
        with problem_data_lock('problem:%s' % self.root.pk):
            imported = import_polygon_package(self.polygon_package(), problem_code=self.mirror.code,
                                              do_update=True, override_statements=False, interactive=False)
        imported.refresh_from_db()
        child.refresh_from_db()
        self.assertIsNone(imported.mirror_of_id)
        self.assertTrue(imported.is_locked)
        self.assertEqual(child.mirror_root_id, self.root.pk)
        self.assertEqual(get_problem_testcases_data(imported)[1]['answer'], '4\n')
        self.assertEqual(get_problem_testcases_data(self.root)[1]['answer'], '1\n')
        self.assertEqual(ProblemData.objects.get(problem=imported).archive_source_problem_id, imported.pk)

    def test_failed_polygon_compile_rolls_back_source_and_keeps_unlocked(self):
        self.unlock()
        with patch('judge.utils.polygon_import.ProblemDataCompiler.generate', side_effect=CommandError('invalid test')):
            with self.assertRaises(CommandError):
                import_polygon_package(self.polygon_package(), problem_code=self.mirror.code,
                                       do_update=True, override_statements=False, interactive=False)
        self.mirror.refresh_from_db()
        self.assertEqual(self.mirror.mirror_of_id, self.root.pk)
        self.assertFalse(self.mirror.is_locked)
        self.assertFalse(ProblemData.objects.filter(problem=self.mirror).exists())
