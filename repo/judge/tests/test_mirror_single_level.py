import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.contrib.admin import AdminSite
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, transaction, DatabaseError
from django.test import RequestFactory, TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse

from judge.forms import ProblemEditForm
from judge.models import ExternalProblem, Language, Problem, Submission, SubmissionSource, problem_data_storage
from judge.models.tests.util import create_organization, create_problem, create_problem_type, create_user
from judge.utils.problem_mirror import get_mirrorable_source_queryset, validate_mirror_source_for_target
from judge.utils.test_data_lock import locked_problem, unlock_tests
from judge.views.problem_data import ProblemMirrorForm


class SingleLevelMirrorTests(TestCase):
    fixtures = ['language_all.json']

    def setUp(self):
        self.superuser = create_user(username='single_super', is_staff=True, is_superuser=True)
        self.owner = create_user(username='single_owner', user_permissions=('edit_own_problem',))
        self.editor = create_user(username='single_editor', user_permissions=('edit_own_problem',))
        self.org = create_organization(name='ownerorg', admins=('single_owner',), paid_until=date(2100, 1, 1))
        self.foreign_org = create_organization(name='foreignorg', paid_until=date(2100, 1, 1))
        create_problem_type(name='single_type')
        self.public = create_problem(code='single_public', is_public=True, authors=('single_editor',))
        self.private = create_problem(code='single_private', is_public=False, authors=('single_owner',))
        self.foreign = create_problem(code='single_foreign', is_public=False, is_organization_private=True,
                                      organizations=('foreignorg',), authors=('single_owner',))
        self.same_org = create_problem(code='single_sameorg', is_public=False, is_organization_private=True,
                                       organizations=('ownerorg',), testers=('single_owner',))
        self.target = create_problem(code='ownerorg_target', is_organization_private=True,
                                     organizations=('ownerorg',), authors=('single_owner', 'single_editor'),
                                     types=('single_type',))

    def grant(self, source=None):
        Problem.objects.filter(pk=self.target.pk).update(is_locked=False)
        self.target.refresh_from_db()
        self.target.mirror_of = source or self.foreign
        self.target.save()
        Problem.objects.filter(pk=self.target.pk).update(is_locked=True)
        self.target.refresh_from_db()

    def source_ids(self, user, target=None):
        return set(get_mirrorable_source_queryset(user, target_problem=target or self.target).values_list('pk', flat=True))

    def test_source_choices_require_editing_source_not_org_admin(self):
        self.assertTrue({self.public.pk, self.private.pk, self.foreign.pk, self.same_org.pk} <= self.source_ids(self.superuser))
        self.assertEqual(self.source_ids(self.owner), {self.private.pk, self.foreign.pk})
        self.assertEqual(self.source_ids(self.editor), {self.public.pk})
        self.assertEqual(self.source_ids(AnonymousUser()), set())

    def test_public_root_requires_edit_permission_and_owned_private_root_is_allowed(self):
        global_target = create_problem(code='single_global_target', authors=('single_editor',))
        self.assertFalse(self.public.is_editable_by(self.owner))
        self.assertNotIn(self.public.pk, self.source_ids(self.owner, global_target))
        self.assertIn(self.private.pk, self.source_ids(self.owner, global_target))
        self.assertEqual(self.source_ids(self.editor, global_target), {self.public.pk, self.target.pk})
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(self.owner, self.public, global_target)

    def test_private_root_with_multiple_orgs_is_allowed_when_editable(self):
        self.same_org.organizations.add(self.foreign_org)
        self.assertTrue(self.same_org.is_accessible_by(self.owner))
        self.assertNotIn(self.same_org.pk, self.source_ids(self.owner))
        self.same_org.authors.add(self.owner.profile)
        self.assertIn(self.same_org.pk, self.source_ids(self.owner))
        self.assertIn(self.same_org.pk, self.source_ids(self.superuser))

    def test_select2_uses_the_same_root_policy(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('mirror_problem_select2'), {'target': self.target.pk, 'term': 'single_'})
        self.assertEqual(response.status_code, 200)
        ids = {int(row['id']) for row in response.json()['results']}
        self.assertEqual(ids, {self.private.pk, self.foreign.pk})

    def test_admin_source_field_uses_request_user_and_preserves_grant(self):
        from judge.admin.problem import ProblemAdmin
        admin = ProblemAdmin(Problem, AdminSite())
        request = RequestFactory().get('/')
        request.user = self.superuser
        form_class = admin.get_form(request, self.target)
        form = form_class(instance=self.target)
        self.assertEqual(form.user, self.superuser)
        self.assertIn(self.foreign.pk, form.fields['mirror_of'].queryset.values_list('pk', flat=True))
        self.grant()
        request.user = self.editor
        form = admin.get_form(request, self.target)(instance=self.target)
        self.assertIn(self.foreign.pk, form.fields['mirror_of'].queryset.values_list('pk', flat=True))
        self.assertNotIn(self.private.pk, form.fields['mirror_of'].queryset.values_list('pk', flat=True))

    def test_superuser_can_assign_cross_org_and_global_private_roots(self):
        for root in (self.foreign, self.private):
            validate_mirror_source_for_target(self.superuser, root, self.target)
            with self.assertRaises(ValidationError):
                validate_mirror_source_for_target(self.editor, root, self.target)
        Problem.objects.filter(pk=self.target.pk).update(is_locked=False)
        self.client.force_login(self.superuser)
        response = self.client.post(reverse('problem_data', args=[self.target.code]), {
            'mirror-test_source': 'mirror', 'mirror-mirror_of': self.foreign.pk,
        })
        self.assertEqual(response.status_code, 302)
        self.target.refresh_from_db()
        self.assertEqual(self.target.mirror_of_id, self.foreign.pk)
        self.assertTrue(self.target.is_locked)

    def test_saved_exception_is_preserved_by_non_admin_editor(self):
        self.grant()
        self.assertNotIn(self.foreign.pk, self.source_ids(self.editor))
        form = ProblemMirrorForm(instance=self.target, user=self.editor, data={
            'test_source': 'mirror', 'mirror_of': self.foreign.pk,
        })
        self.assertIn(self.foreign.pk, form.fields['mirror_of'].queryset.values_list('pk', flat=True))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        changed = ProblemMirrorForm(instance=Problem.objects.get(pk=self.target.pk), user=self.editor, data={
            'test_source': 'mirror', 'mirror_of': self.private.pk,
        })
        self.assertFalse(changed.is_valid())

    def test_edit_form_updates_limits_and_ignores_forged_source(self):
        self.grant()
        p = self.target
        form = ProblemEditForm(instance=p, user=self.owner, org_pk=self.org.pk, data={
            'code': p.code, 'name': p.name, 'time_limit': '1.1', 'memory_limit': p.memory_limit,
            'points': p.points, 'source': '', 'group': p.group_id,
            'testcase_visibility_mode': p.testcase_visibility_mode, 'description': '',
            'types': list(p.types.values_list('pk', flat=True)), 'mirror_of': self.private.pk,
        })
        self.assertNotIn('mirror_of', form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        p.refresh_from_db()
        self.assertEqual(p.time_limit, 1.1)
        self.assertEqual((p.mirror_of_id, p.mirror_root_id), (self.foreign.pk, self.foreign.pk))

    def test_grant_does_not_grant_root_download_permission(self):
        self.grant()
        problem_data_storage.save(self.foreign.code + '/init.yml', ContentFile('secret: root data'))
        self.client.force_login(self.editor)
        response = self.client.get(reverse('problem_data_file', args=[self.target.code, 'init.yml']))
        self.assertEqual(response.status_code, 404)

    def test_a_assigns_source_and_b_can_edit_and_apply_without_viewing_it(self):
        # A and B edit the target; only A edits the private cross-org source.
        self.org.admins.clear()
        self.assertTrue(self.foreign.is_editable_by(self.owner))
        self.assertFalse(self.foreign.is_accessible_by(self.editor))
        url = reverse('problem_data', args=[self.target.code])
        selection = {'mirror-test_source': 'mirror', 'mirror-mirror_of': self.foreign.pk}
        self.client.force_login(self.owner)
        with patch('judge.utils.test_data_lock.abort_test_submissions'):
            self.assertEqual(self.client.post(url, {'action': 'unlock-tests'}).status_code, 302)
            self.assertEqual(self.client.post(url, selection).status_code, 302)
        self.client.force_login(self.editor)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['mirror_form']['mirror_of'].value(), self.foreign.pk)
        self.assertIn(self.foreign.pk, page.context['mirror_form'].fields['mirror_of'].queryset.values_list('pk', flat=True))
        self.assertNotIn(self.foreign.pk, self.source_ids(self.editor))

        p = Problem.objects.get(pk=self.target.pk)
        edit_url = reverse('problem_edit', args=[p.code])
        edit = self.client.get(edit_url)
        payload = {
            'code': p.code, 'name': p.name, 'time_limit': '1.1', 'memory_limit': p.memory_limit,
            'points': p.points, 'source': '', 'group': p.group_id, 'description': '',
            'testcase_visibility_mode': p.testcase_visibility_mode,
            'types': list(p.types.values_list('pk', flat=True)), 'mirror_of': self.private.pk,
        }
        for name in ('solution_formset', 'lang_limit_formset'):
            formset = edit.context[name]
            self.assertEqual(formset.initial_form_count(), 0)
            for field, value in (('TOTAL_FORMS', 0), ('INITIAL_FORMS', 0), ('MIN_NUM_FORMS', 0), ('MAX_NUM_FORMS', 1000)):
                payload[formset.prefix + '-' + field] = value
        self.assertEqual(self.client.post(edit_url, payload).status_code, 302)
        p.refresh_from_db()
        self.assertEqual(p.time_limit, 1.1)
        self.assertEqual((p.mirror_of_id, p.mirror_root_id), (self.foreign.pk, self.foreign.pk))

        with patch('judge.utils.test_data_lock.abort_test_submissions'):
            self.assertEqual(self.client.post(url, {'action': 'unlock-tests'}).status_code, 302)
            # Missing/forged fields cannot accidentally clear the saved source.
            self.assertEqual(self.client.post(url, {'mirror-test_source': 'mirror'}).status_code, 200)
            self.assertEqual(self.client.post(url, {'mirror-test_source': 'mirror', 'mirror-mirror_of': self.private.pk}).status_code, 200)
            self.assertEqual(self.client.post(url, selection).status_code, 302)
        p.refresh_from_db()
        self.assertEqual((p.mirror_of_id, p.mirror_root_id), (self.foreign.pk, self.foreign.pk))
        self.assertTrue(p.is_locked)
        self.assertFalse(self.foreign.is_accessible_by(self.editor))

    def test_replacing_source_does_not_grant_permission_to_choose_it_again(self):
        self.grant()
        Problem.objects.filter(pk=self.target.pk).update(is_locked=False)
        target = Problem.objects.get(pk=self.target.pk)
        changed = ProblemMirrorForm(instance=target, user=self.editor, data={
            'test_source': 'mirror', 'mirror_of': self.public.pk,
        })
        self.assertTrue(changed.is_valid(), changed.errors)
        changed.save()
        restore = ProblemMirrorForm(instance=Problem.objects.get(pk=target.pk), user=self.editor, data={
            'test_source': 'mirror', 'mirror_of': self.foreign.pk,
        })
        self.assertFalse(restore.is_valid())

    def test_only_a_persisted_relationship_grants_the_source_exception(self):
        unsaved = Problem(code='unsaved_mirror', mirror_of=self.foreign)
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(self.editor, self.foreign, unsaved)
        self.grant()
        stale = Problem.objects.get(pk=self.target.pk)
        stale.mirror_of = self.private
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(self.editor, self.private, stale)

    def test_superuser_cannot_choose_mirror_self_or_root_with_dependents_as_target(self):
        self.grant(self.public)
        with self.assertRaises(ValidationError):
            create_problem(code='single_too_deep', mirror_of=self.target)
        for source, target in ((self.target, self.target), (self.target, self.private), (self.private, self.public)):
            with self.assertRaises(ValidationError):
                validate_mirror_source_for_target(self.superuser, source, target)
        self.assertNotIn(self.target.pk, self.source_ids(self.superuser, self.private))
        self.assertEqual(self.source_ids(self.superuser, self.public), set())
        Problem.objects.filter(pk=self.public.pk).update(is_locked=False)
        self.public.mirror_of = self.private
        with self.assertRaises(ValidationError):
            self.public.save()

    def test_virtual_and_cumulative_roots_are_not_choices(self):
        ExternalProblem.objects.create(problem=self.public, is_active=True, oj='vjudge', external_problem_id='x')
        Problem.objects.filter(pk=self.private.pk).update(subtask_score_state='ON')
        choices = self.source_ids(self.superuser)
        self.assertNotIn(self.public.pk, choices)
        self.assertNotIn(self.private.pk, choices)
        with self.assertRaises(ValidationError):
            validate_mirror_source_for_target(self.superuser, self.public, self.target)

    def test_dispatch_uses_fresh_source_and_matching_file_lock(self):
        self.grant(self.public)
        sub = Submission.objects.create(problem=self.target, user=self.owner.profile, language=Language.get_python3(), status='D')
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        self.assertEqual(sub.problem.mirror_of.code, self.public.code)
        self.grant(self.private)
        with patch('judge.judgeapi.judge_request', return_value={'name': 'submission-received', 'submission-id': sub.pk}) as send:
            with self.captureOnCommitCallbacks(execute=True):
                sub.judge(rejudge=True)
        self.assertEqual(send.call_args.args[0]['problem-id'], self.private.code)
        from judge.bridge.judge_handler import JudgeHandler
        data = object.__new__(JudgeHandler).get_related_submission_data(sub.pk)
        self.assertEqual(data.data_lock, 'problem:%s' % self.private.pk)

    def test_unlock_refreshes_profile_after_aborting_rejudge(self):
        sub = Submission.objects.create(problem=self.public, user=self.owner.profile, language=Language.get_python3(),
                                        status='D', result='AC', points=1, case_points=1, case_total=1)
        profile = self.owner.profile
        profile.calculate_points()
        Submission.objects.filter(pk=sub.pk).update(status='G', points=None, result=None)
        with patch('judge.utils.test_data_lock.abort_test_submissions'), self.captureOnCommitCallbacks(execute=True):
            unlock_tests(self.public.pk)
        profile.refresh_from_db()
        self.assertEqual(profile.points, 0)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')

    def test_flatten_migration_changes_only_selected_relations(self):
        self.grant(self.public)
        nested = create_problem(code='nvnamson_phatqua')
        # Historical state before the new model invariant.
        Problem.objects.filter(pk=nested.pk).update(mirror_of_id=self.target.pk, mirror_root_id=self.public.pk)
        migration = importlib.import_module('judge.migrations.0255_flatten_mirrors')
        from django.apps import apps
        with patch.dict(migration.SOURCES, {'nvnamson_phatqua': self.public.code}, clear=True):
            with connection.schema_editor() as editor:
                migration.flatten_mirrors(apps, editor)
        nested.refresh_from_db()
        self.assertEqual((nested.mirror_of_id, nested.mirror_root_id), (self.public.pk, self.public.pk))
        self.target.refresh_from_db()
        self.assertEqual(self.target.mirror_of_id, self.public.pk)


class MirrorAdmissionRaceTests(TransactionTestCase):
    fixtures = ['language_all.json']

    @skipUnlessDBFeature('has_select_for_update_nowait')
    def test_root_change_retries_before_admission_and_unlock_aborts_new_submission(self):
        user = create_user(username='single_race_user')
        a = create_problem(code='single_race_a')
        b = create_problem(code='single_race_b')
        m = create_problem(code='single_race_m', mirror_of=a)
        language = Language.get_python3()
        read_old_root, continue_lock, before_insert, continue_insert, unlock_started = [Event() for _ in range(5)]

        def admit():
            close_old_connections()

            def pause(execute, sql, params, many, context):
                if 'FOR UPDATE' in sql and not read_old_root.is_set():
                    read_old_root.set()
                    if not continue_lock.wait(10):
                        raise RuntimeError('edit timeout')
                if sql.startswith('INSERT INTO `judge_submission`'):
                    before_insert.set()
                    if not continue_insert.wait(10):
                        raise RuntimeError('insert timeout')
                return execute(sql, params, many, context)
            try:
                with connection.execute_wrapper(pause):
                    return Submission.objects.create(problem_id=m.pk, user_id=user.profile.pk, language_id=language.pk).pk
            finally:
                close_old_connections()

        def unlock():
            close_old_connections()

            def observe(execute, sql, params, many, context):
                if 'FOR UPDATE' in sql:
                    unlock_started.set()
                return execute(sql, params, many, context)
            try:
                with connection.execute_wrapper(observe), patch('judge.utils.test_data_lock.abort_test_submissions'):
                    return unlock_tests(b.pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(admit)
            aborting = None
            try:
                self.assertTrue(read_old_root.wait(10))
                Problem.objects.filter(pk=m.pk).update(is_locked=False)
                changed = Problem.objects.get(pk=m.pk)
                changed.mirror_of = b
                changed.save()
                Problem.objects.filter(pk=m.pk).update(is_locked=True)
                continue_lock.set()
                self.assertTrue(before_insert.wait(10))
                # The new root must already be protected while admission is in flight.
                with self.assertRaises(DatabaseError), transaction.atomic():
                    Problem.objects.select_for_update(nowait=True).get(pk=b.pk)
                aborting = pool.submit(unlock)
                self.assertTrue(unlock_started.wait(10))
            finally:
                continue_lock.set()
                continue_insert.set()
                sub_id = admission.result(timeout=10)
                aborted = aborting.result(timeout=10) if aborting else None
        self.assertEqual(aborted, 1)
        self.assertEqual(Submission.objects.get(pk=sub_id).status, 'AB')
