import io
import zipfile

from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.forms import ProblemEditForm
from judge.models import ContestParticipation, Language, LanguageLimit, Organization, Problem, ProblemData, \
    ProblemTestCase as ProblemTestCaseModel
from judge.models.problem import ProblemTestcaseAccess, disallowed_characters_validator
from judge.models.tests.util import CommonDataMixin, create_contest, create_contest_participation, \
    create_contest_problem, create_organization, create_problem, create_problem_type, create_solution, \
    create_user
from judge.utils.problem_mirror import get_mirrorable_source_queryset, sync_mirror_archive_for_problem, sync_mirror_archives_for_root, \
    validate_mirror_source_for_target


class ProblemTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        _now = timezone.now()

        self.users.update({
            'staff_problem_edit_only_all': create_user(
                username='staff_problem_edit_only_all',
                is_staff=True,
                user_permissions=('edit_all_problem',),
            ),
            'normal_in_contest': create_user(
                username='normal_in_contest',
            ),
        })

        self.users.update({
            'suggester': create_user(
                username='suggester',
                user_permissions=('edit_own_problem', 'suggest_new_problem', 'rejudge_submission'),
            ),
        })

        create_problem_type(name='type')

        self.basic_problem = create_problem(
            code='basic',
            allowed_languages=Language.objects.values_list('key', flat=True),
            types=('type',),
            authors=('normal',),
            testers=('staff_problem_edit_public',),
        )

        self.testcase_allow_all = create_problem(
            code='allow_all',
            allowed_languages=Language.objects.values_list('key', flat=True),
            types=('type',),
            authors=('superuser',),
            is_public=True,
            testcase_visibility_mode=ProblemTestcaseAccess.ALWAYS,
        )

        self.testcase_allow_out_contest = create_problem(
            code='allow_out_contest',
            allowed_languages=Language.objects.values_list('key', flat=True),
            types=('type',),
            authors=('superuser',),
            is_public=True,
            testcase_visibility_mode=ProblemTestcaseAccess.OUT_CONTEST,
        )

        self.basic_contest = create_contest(
            key='basic',
            start_time=_now - timezone.timedelta(days=1),
            end_time=_now + timezone.timedelta(days=100),
            authors=('superuser', ),
        )

        create_contest_problem(
            problem=self.testcase_allow_out_contest,
            contest=self.basic_contest,
        )

        for user in (
            'normal_in_contest', 'superuser', 'staff_problem_edit_own',
                'staff_problem_see_all', 'staff_problem_edit_all'):
            self.users[user].profile.current_contest = create_contest_participation(
                contest='basic',
                user=user,
                real_start=_now - timezone.timedelta(hours=1),
                virtual=ContestParticipation.LIVE,
            )
            self.users[user].profile.save()

        limits = []
        for lang in Language.objects.filter(common_name=Language.get_python3().common_name):
            limits.append(
                LanguageLimit(
                    problem=self.basic_problem,
                    language=lang,
                    time_limit=100,
                    memory_limit=131072,
                ),
            )
        LanguageLimit.objects.bulk_create(limits)

        self.organization_private_problem = create_problem(
            code='organization_private',
            time_limit=2,
            is_public=True,
            is_organization_private=True,
            curators=('staff_problem_edit_own', 'staff_problem_edit_own_no_staff'),
        )

        self.problem_organization = create_organization(
            name='problem organization',
            admins=('normal', 'staff_problem_edit_public'),
        )
        self.organization_admin_private_problem = create_problem(
            code='org_admin_private',
            is_organization_private=True,
            organizations=('problem organization',),
        )
        self.organization_admin_problem = create_problem(
            code='organization_admin',
            organizations=('problem organization',),
        )

        self.suggesting_problem = create_problem(
            code='suggesting',
            suggester=self.users['suggester'].profile,
        )

    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'

    def test_basic_problem(self):
        self.assertEqual(str(self.basic_problem), self.basic_problem.name)
        self.assertCountEqual(
            self.basic_problem.languages_list(),
            set(Language.objects.values_list('common_name', flat=True)),
        )
        self.basic_problem.user_count = -1000
        self.basic_problem.ac_rate = -1000
        self.basic_problem.update_stats()
        self.assertEqual(self.basic_problem.user_count, 0)
        self.assertEqual(self.basic_problem.ac_rate, 0)

        self.assertListEqual(list(self.basic_problem.author_ids), [self.users['normal'].profile.id])
        self.assertListEqual(list(self.basic_problem.editor_ids), [self.users['normal'].profile.id])
        self.assertListEqual(list(self.basic_problem.tester_ids), [self.users['staff_problem_edit_public'].profile.id])
        self.assertListEqual(list(self.basic_problem.usable_languages), [])
        self.assertListEqual(self.basic_problem.types_list, ['type'])
        self.assertSetEqual(self.basic_problem.usable_common_names, set())

        self.assertEqual(self.basic_problem.translated_name('ABCDEFGHIJK'), self.basic_problem.name)

        self.assertFalse(self.basic_problem.clarifications.exists())

    def test_basic_problem_language_limits(self):
        for common_name, memory_limit in self.basic_problem.language_memory_limit:
            self.assertEqual(memory_limit, 131072)
        for common_name, time_limit in self.basic_problem.language_time_limit:
            self.assertEqual(time_limit, 100)

    def test_basic_problem_methods(self):
        self.assertTrue(self.basic_problem.is_editor(self.users['normal'].profile))

        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.basic_problem, data)

    def test_testcase_visible(self):
        data_basic = {
            'superuser': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_testcase_accessible_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_testcase_accessible_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'normal': {
                'is_testcase_accessible_by': self.assertFalse,
            },
            'anonymous': {
                'is_testcase_accessible_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.basic_problem, data_basic)

        data_all = {
            'superuser': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'staff_problem_see_all': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_all': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'normal': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'anonymous': {
                'is_testcase_accessible_by': self.assertTrue,
            },
        }
        self._test_object_methods_with_users(self.testcase_allow_all, data_all)

        data_out_contest = {
            'superuser': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_testcase_accessible_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_testcase_accessible_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'normal': {
                'is_testcase_accessible_by': self.assertTrue,
            },
            'normal_in_contest': {
                'is_testcase_accessible_by': self.assertFalse,
            },
            'anonymous': {
                'is_testcase_accessible_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.testcase_allow_out_contest, data_out_contest)

    def test_organization_private_problem_methods(self):
        self.assertFalse(self.organization_private_problem.is_accessible_by(self.users['normal']))
        self.users['normal'].profile.organizations.add(self.organizations['open'])
        self.assertFalse(self.organization_private_problem.is_accessible_by(self.users['normal']))
        self.organizations['open'].plan = Organization.PLAN_PAID
        self.organizations['open'].save(update_fields=['plan'])
        self.organization_private_problem.organizations.add(self.organizations['open'])

        data = {
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_subs_manageable_by': self.assertTrue,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_subs_manageable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_edit_all_with_rejudge': {
                'is_editable_by': self.assertTrue,
                'is_subs_manageable_by': self.assertTrue,
            },
            'staff_problem_edit_own_no_staff': {
                'is_editable_by': self.assertTrue,
                'is_subs_manageable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_private_problem, data)

    def test_organization_admin_private_problem_methods(self):
        data = {
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
                'is_subs_manageable_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
                'is_subs_manageable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_organization_admin': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_admin_private_problem, data)

    def test_organization_admin_problem_methods(self):
        data = {
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_organization_admin': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.organization_admin_problem, data)

    def test_suggesting_problem_methods(self):
        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_organization_admin': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'staff_problem_see_organization': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
                'is_editable_by': self.assertFalse,
            },
            'suggester': {
                'is_accessible_by': self.assertTrue,
                'is_editable_by': self.assertTrue,
                'is_rejudgeable_by': self.assertTrue,
            },
        }
        self._test_object_methods_with_users(self.suggesting_problem, data)

    def test_problems_list(self):
        for name, user in self.users.items():
            with self.subTest(user=name):
                with self.subTest(list='accessible problems'):
                    # We only care about consistency between Problem.is_accessible_by and Problem.get_visible_problems
                    problem_codes = []
                    for problem in Problem.objects.prefetch_related('authors', 'curators', 'testers', 'organizations'):
                        if problem.is_accessible_by(user):
                            problem_codes.append(problem.code)

                    self.assertCountEqual(
                        Problem.get_visible_problems(user).distinct().values_list('code', flat=True),
                        problem_codes,
                    )

                with self.subTest(list='editable problems'):
                    # We only care about consistency between Problem.is_editable_by and Problem.get_editable_problems
                    problem_codes = []
                    for problem in Problem.objects.prefetch_related('authors', 'curators'):
                        if problem.is_editable_by(user):
                            problem_codes.append(problem.code)

                    self.assertCountEqual(
                        Problem.get_editable_problems(user).distinct().values_list('code', flat=True),
                        problem_codes,
                    )

    def test_mirror_chain_cache_and_rebuild(self):
        root = create_problem(code='mirror_root_public', is_public=True)
        first = create_problem(code='mirror_first_public', is_public=True, mirror_of=root)
        second = create_problem(code='mirror_second_public', is_public=True, mirror_of=first)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.mirror_root_id, root.id)
        self.assertEqual(second.mirror_root_id, root.id)

        first.mirror_of = None
        first.save()

        second.refresh_from_db()
        self.assertEqual(second.mirror_root_id, first.id)

    def test_mirror_cycle_is_rejected(self):
        root = create_problem(code='mirror_cycle_root', is_public=True)
        first = create_problem(code='mirror_cycle_first', is_public=True, mirror_of=root)
        with self.assertRaises(ValidationError):
            root.mirror_of = first
            root.save()

    def test_mirror_permission_policy(self):
        public_source = create_problem(code='mirror_policy_public', is_public=True)
        same_org_source = create_problem(
            code='mirror_policy_same_org',
            is_public=True,
            is_organization_private=True,
            organizations=('problem organization',),
        )
        other_org = create_organization(name='other mirror org', admins=('staff_problem_edit_own',))
        other_org_source = create_problem(
            code='mirror_policy_other_org',
            is_public=True,
            is_organization_private=True,
            organizations=('other mirror org',),
        )

        # Public source is allowed for org admins.
        validate_mirror_source_for_target(
            user=self.users['normal'],
            source=public_source,
            target_org=self.problem_organization,
        )
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(
                user=self.users['staff_problem_edit_own'],
                source=public_source,
                target_org=self.problem_organization,
            )

        # Same-org private source is only for org admins.
        validate_mirror_source_for_target(
            user=self.users['normal'],
            source=same_org_source,
            target_org=self.problem_organization,
        )
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(
                user=self.users['staff_problem_edit_own'],
                source=same_org_source,
                target_org=self.problem_organization,
            )

        # Cross-organization mirroring is forbidden even for admins.
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(
                user=self.users['normal'],
                source=other_org_source,
                target_org=self.problem_organization,
            )

    def test_mirror_source_queryset_requires_org_admin(self):
        public_source = create_problem(code='mirror_list_public', is_public=True)
        private_same_org = create_problem(
            code='mirror_list_same_org',
            is_public=True,
            is_organization_private=True,
            organizations=('problem organization',),
        )

        admin_qs = get_mirrorable_source_queryset(
            self.users['normal'],
            target_org=self.problem_organization,
        )
        self.assertIn(public_source.id, admin_qs.values_list('id', flat=True))
        self.assertIn(private_same_org.id, admin_qs.values_list('id', flat=True))

        non_admin_qs = get_mirrorable_source_queryset(
            self.users['staff_problem_edit_own'],
            target_org=self.problem_organization,
        )
        self.assertFalse(non_admin_qs.exists())

    def test_mirror_keeps_custom_cases_after_root_sync(self):
        root = create_problem(code='mirror_custom_root', is_public=True, types=('type',))
        ProblemTestCaseModel.objects.create(
            dataset=root, order=1, type='C', input_file='1.in', output_file='1.out', points=10, is_pretest=False,
        )

        mirror = create_problem(code='mirror_custom_child', is_public=True, mirror_of=root, types=('type',))
        mirror.refresh_from_db()
        case = mirror.cases.get(order=1)
        case.points = 123
        case.save(update_fields=['points'])

        root_case = root.cases.get(order=1)
        root_case.points = 7
        root_case.save(update_fields=['points'])
        sync_mirror_archives_for_root(root)

        mirror_case = mirror.cases.get(order=1)
        self.assertEqual(mirror_case.points, 123)

    def test_set_mirror_does_not_override_existing_cases(self):
        root = create_problem(code='mirror_keep_existing_root', is_public=True, types=('type',))
        ProblemTestCaseModel.objects.create(
            dataset=root, order=1, type='C', input_file='1.in', output_file='1.out', points=10, is_pretest=False,
        )

        child = create_problem(code='mirror_keep_existing_child', is_public=True, types=('type',))
        ProblemTestCaseModel.objects.create(
            dataset=child, order=1, type='C', input_file='x.in', output_file='x.out', points=33, is_pretest=False,
        )

        child.mirror_of = root
        child.save()

        self.assertEqual(child.cases.count(), 1)
        self.assertEqual(child.cases.get(order=1).points, 33)
        self.assertEqual(child.cases.get(order=1).input_file, 'x.in')

    def test_mirror_auto_repairs_missing_case_files_from_root_archive(self):
        root = create_problem(code='mirror_fix_files_root', is_public=True, types=('type',))
        root_data, _ = ProblemData.objects.get_or_create(problem=root)
        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('1.in', '1 2\n')
            archive.writestr('1.out', '3\n')
        root_data.zipfile.save('mirror_fix_files.zip', ContentFile(zip_bytes.getvalue()), save=True)

        ProblemTestCaseModel.objects.create(
            dataset=root, order=1, type='C', input_file='1.in', output_file='1.out', points=10, is_pretest=False,
        )

        mirror = create_problem(code='mirror_fix_files_child', is_public=True, mirror_of=root, types=('type',))
        case = mirror.cases.get(order=1)
        case.input_file = 'x.in'
        case.output_file = 'x.out'
        case.points = 77
        case.save(update_fields=['input_file', 'output_file', 'points'])

        sync_mirror_archive_for_problem(
            mirror, bootstrap_cases_if_empty=False, heal_missing_files=True, force_regenerate=True,
        )
        case.refresh_from_db()
        self.assertEqual(case.input_file, '1.in')
        self.assertEqual(case.output_file, '1.out')
        self.assertEqual(case.points, 77)

    def _problem_edit_payload(self, problem, mirror_of=None):
        payload = {
            'is_public': 'on' if problem.is_public else '',
            'code': problem.code,
            'name': problem.name,
            'time_limit': str(problem.time_limit),
            'memory_limit': str(problem.memory_limit),
            'batch_type': problem.batch_type,
            'points': str(problem.points),
            'source': problem.source or '',
            'group': str(problem.group_id),
            'testcase_visibility_mode': problem.testcase_visibility_mode,
            'description': problem.description or '',
            'types': [str(pk) for pk in problem.types.values_list('id', flat=True)],
            'testers': [str(pk) for pk in problem.testers.values_list('id', flat=True)],
            'mirror_of': str(mirror_of.pk) if mirror_of else '',
        }
        if problem.partial:
            payload['partial'] = 'on'
        return payload

    def _problem_data_payload(self, include_zip_clear=True):
        payload = {
            'problem-data-checker': 'standard',
            'problem-data-checker_args': '',
            'problem-data-checker_type': 'default',
            'problem-data-grader': 'standard',
            'problem-data-grader_args': '',
            'problem-data-io_method': 'standard',
            'problem-data-io_input_file': '',
            'problem-data-io_output_file': '',
            'problem-data-output_limit': '',
            'cases-TOTAL_FORMS': '0',
            'cases-INITIAL_FORMS': '0',
            'cases-MIN_NUM_FORMS': '0',
            'cases-MAX_NUM_FORMS': '1',
        }
        if include_zip_clear:
            payload['problem-data-zipfile-clear'] = 'on'
        return payload

    def test_problem_edit_form_rejects_mirror_for_non_org_admin(self):
        mirror_source = create_problem(code='mirror_form_public_source', is_public=True)
        org_problem = create_problem(
            code='problemorganization_mfna',
            is_public=True,
            is_organization_private=True,
            organizations=('problem organization',),
            types=('type',),
        )

        payload = self._problem_edit_payload(org_problem, mirror_of=mirror_source)
        form = ProblemEditForm(
            data=payload,
            instance=org_problem,
            user=self.users['staff_problem_edit_own'],
            org_pk=self.problem_organization.pk,
        )
        self.assertFalse(form.is_valid())
        self.assertIn('mirror_of', form.errors)

    def test_problem_edit_form_allows_mirror_for_org_admin(self):
        mirror_source = create_problem(code='mirror_form_public_source_admin', is_public=True)
        org_problem = create_problem(
            code='problemorganization_mfa',
            is_public=True,
            is_organization_private=True,
            organizations=('problem organization',),
            types=('type',),
        )

        payload = self._problem_edit_payload(org_problem, mirror_of=mirror_source)
        form = ProblemEditForm(
            data=payload,
            instance=org_problem,
            user=self.users['normal'],
            org_pk=self.problem_organization.pk,
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_problem_data_warns_before_root_archive_update(self):
        root = create_problem(
            code='mirror_warning_root',
            is_public=True,
            authors=('staff_problem_edit_own',),
            types=('type',),
        )
        ProblemData.objects.get_or_create(problem=root)
        mirror = create_problem(
            code='mirror_warning_child',
            is_public=True,
            types=('type',),
            mirror_of=root,
        )
        mirror.save()

        self.client.force_login(self.users['staff_problem_edit_all'])
        response = self.client.post(reverse('problem_data', args=[root.code]), data=self._problem_data_payload())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'confirm_mirror_root_archive_update')

    def test_problem_data_page_shows_mirror_selector(self):
        problem = create_problem(
            code='mirror_selector_page',
            is_public=True,
            authors=('staff_problem_edit_own',),
            types=('type',),
        )
        ProblemData.objects.get_or_create(problem=problem)

        self.client.force_login(self.users['staff_problem_edit_all'])
        response = self.client.get(reverse('problem_data', args=[problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id_mirror-mirror_of')
        self.assertContains(response, 'name="mirror-mirror_of"')
        self.assertContains(response, 'value=""')

    def test_problem_data_apply_none_removes_mirror(self):
        root = create_problem(code='mirror_none_root', is_public=True, types=('type',))
        mirror = create_problem(
            code='mirror_none_child',
            is_public=True,
            authors=('staff_problem_edit_all',),
            mirror_of=root,
            types=('type',),
        )
        mirror.save()

        self.client.force_login(self.users['staff_problem_edit_all'])
        payload = self._problem_data_payload(include_zip_clear=False)
        payload['mirror-mirror_of'] = ''
        response = self.client.post(reverse('problem_data', args=[mirror.code]), data=payload)
        self.assertEqual(response.status_code, 302)
        mirror.refresh_from_db()
        self.assertIsNone(mirror.mirror_of_id)

    def test_problem_data_accepts_confirmed_root_archive_update(self):
        root = create_problem(
            code='mirror_warning_confirm_root',
            is_public=True,
            authors=('staff_problem_edit_own',),
            types=('type',),
        )
        ProblemData.objects.get_or_create(problem=root)
        mirror = create_problem(
            code='mirror_warning_confirm_child',
            is_public=True,
            types=('type',),
            mirror_of=root,
        )
        mirror.save()

        self.client.force_login(self.users['staff_problem_edit_all'])
        payload = self._problem_data_payload()
        payload['confirm_mirror_root_archive_update'] = '1'
        response = self.client.post(reverse('problem_data', args=[root.code]), data=payload)
        self.assertEqual(response.status_code, 302)

    def test_problem_data_mirror_post_saves_cases(self):
        root = create_problem(code='mirror_post_save_root', is_public=True, types=('type',))
        root_data, _ = ProblemData.objects.get_or_create(problem=root)

        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('1.in', '1\n')
            archive.writestr('1.out', '1\n')
        root_data.zipfile.save('mirror_post_save.zip', ContentFile(zip_bytes.getvalue()), save=True)

        ProblemTestCaseModel.objects.create(
            dataset=root, order=1, type='C', input_file='1.in', output_file='1.out', points=10, is_pretest=False,
        )

        mirror = create_problem(
            code='mirror_post_save_child',
            is_public=True,
            authors=('staff_problem_edit_all',),
            mirror_of=root,
            types=('type',),
        )
        mirror.refresh_from_db()
        case = mirror.cases.get(order=1)

        self.client.force_login(self.users['staff_problem_edit_all'])
        payload = {
            'mirror-mirror_of': str(root.id),
            'problem-data-checker': 'standard',
            'problem-data-checker_args': '',
            'problem-data-checker_type': 'default',
            'problem-data-grader': 'standard',
            'problem-data-grader_args': '',
            'problem-data-io_method': 'standard',
            'problem-data-io_input_file': '',
            'problem-data-io_output_file': '',
            'problem-data-output_limit': '',
            'cases-TOTAL_FORMS': '1',
            'cases-INITIAL_FORMS': '1',
            'cases-MIN_NUM_FORMS': '0',
            'cases-MAX_NUM_FORMS': '1',
            'cases-0-id': str(case.id),
            'cases-0-order': '1',
            'cases-0-type': 'C',
            'cases-0-input_file': '1.in',
            'cases-0-output_file': '1.out',
            'cases-0-points': '55',
            'cases-0-checker': 'standard',
            'cases-0-checker_args': '',
            'cases-0-generator_args': '',
        }
        response = self.client.post(reverse('problem_data', args=[mirror.code]), data=payload)
        self.assertEqual(response.status_code, 302)
        case.refresh_from_db()
        self.assertEqual(case.points, 55)

    def test_problem_data_mirror_get_auto_heals_invalid_files(self):
        root = create_problem(code='mirror_get_heal_root', is_public=True, types=('type',))
        root_data, _ = ProblemData.objects.get_or_create(problem=root)

        zip_bytes = io.BytesIO()
        with zipfile.ZipFile(zip_bytes, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('1.in', '1\n')
            archive.writestr('1.out', '1\n')
        root_data.zipfile.save('mirror_get_heal.zip', ContentFile(zip_bytes.getvalue()), save=True)

        ProblemTestCaseModel.objects.create(
            dataset=root, order=1, type='C', input_file='1.in', output_file='1.out', points=10, is_pretest=False,
        )

        mirror = create_problem(
            code='mirror_get_heal_child',
            is_public=True,
            authors=('staff_problem_edit_all',),
            mirror_of=root,
            types=('type',),
        )
        case = mirror.cases.get(order=1)
        case.input_file = 'missing.in'
        case.output_file = 'missing.out'
        case.save(update_fields=['input_file', 'output_file'])

        self.client.force_login(self.users['staff_problem_edit_all'])
        response = self.client.get(reverse('problem_data', args=[mirror.code]))
        self.assertEqual(response.status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.input_file, '1.in')
        self.assertEqual(case.output_file, '1.out')


@override_settings(LANGUAGE_CODE='en-US', LANGUAGES=(('en', 'English'),))
class SolutionTestCase(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(self):
        super().setUpTestData()
        self.users.update({
            'staff_solution_see_all': create_user(
                username='staff_solution_see_all',
                user_permissions=('see_private_solution',),
            ),
        })

        _now = timezone.now()

        self.basic_solution = create_solution(problem='basic')

        self.private_solution = create_solution(
            problem='private',
            is_public=False,
            publish_on=_now - timezone.timedelta(days=100),
        )

        self.unpublished_problem = create_problem(
            code='unpublished',
            name='Unpublished',
            authors=('staff_problem_edit_own',),
        )
        self.unpublished_solution = create_solution(
            problem=self.unpublished_problem,
            is_public=False,
            publish_on=_now + timezone.timedelta(days=100),
            authors=('normal',),
        )

    def test_unpublished_solution(self):
        self.assertEqual(str(self.unpublished_solution), 'Editorial for Unpublished')

    def test_basic_solution_methods(self):
        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_solution_see_all': {
                'is_accessible_by': self.assertTrue,
            },
            'normal': {
                'is_accessible_by': self.assertTrue,
            },
            'anonymous': {
                'is_accessible_by': self.assertTrue,
            },
        }
        self._test_object_methods_with_users(self.basic_solution, data)

    def test_private_solution_methods(self):
        data = {
            'superuser': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_solution_see_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertFalse,
            },
            'staff_problem_see_all': {
                'is_accessible_by': self.assertFalse,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.private_solution, data)

    def test_unpublished_solution_methods(self):
        data = {
            'staff_solution_see_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_own': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_all': {
                'is_accessible_by': self.assertTrue,
            },
            'staff_problem_edit_public': {
                'is_accessible_by': self.assertFalse,
            },
            'normal': {
                'is_accessible_by': self.assertFalse,
            },
            'anonymous': {
                'is_accessible_by': self.assertFalse,
            },
        }
        self._test_object_methods_with_users(self.unpublished_solution, data)


class DisallowedCharactersValidatorTestCase(SimpleTestCase):
    def test_valid(self):
        with self.settings(DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS={'“', '”', '‘', '’'}):
            self.assertIsNone(disallowed_characters_validator(''))
            self.assertIsNone(disallowed_characters_validator('"\'string\''))

        with self.settings(DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS=set()):
            self.assertIsNone(disallowed_characters_validator(''))
            self.assertIsNone(disallowed_characters_validator('“”‘’'))

    def test_invalid(self):
        with self.settings(DMOJ_PROBLEM_STATEMENT_DISALLOWED_CHARACTERS={'“', '”', '‘', '’'}):
            with self.assertRaises(ValidationError, msg='Disallowed characters: “'):
                disallowed_characters_validator('“')
            with self.assertRaisesRegex(ValidationError, 'Disallowed characters: (?=.*‘)(?=.*’)'):
                disallowed_characters_validator('‘’')
