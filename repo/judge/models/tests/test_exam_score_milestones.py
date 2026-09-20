import json
import tempfile
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import reversion
from django.contrib import admin
from django.contrib.auth.models import AnonymousUser, User, Permission
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms import inlineformset_factory
from django.template.loader import render_to_string
from django.test import TestCase, SimpleTestCase, RequestFactory, override_settings
from django.utils.translation import gettext, override
from reversion.models import Version

from judge.admin.exam import ExamTagAdmin, ExamScoreMilestoneForm
from judge.models import ExamTag, ExamScoreMilestone, ExamUserProgress, Profile
from judge.signals import queue_exams_snapshot_rebuild
from judge.utils.exams import (build_exam_snapshots, load_current_exam_snapshot, load_exam_detail_snapshot,
                               _atomic_write_json, exams_index_path, exam_detail_path)
from judge.utils.score_milestones import evaluate_score_milestones, format_milestone_score
from judge.views.exams import ExamsListApiView, ExamDetailApiView, ExamsListView, ExamDetailView


class MilestoneEvaluationTests(SimpleTestCase):
    def test_independent_precise_comparisons_and_no_mutation(self):
        metadata = {'score_context': 'Thang 40', 'note': '', 'milestones': [
            {'id': i, 'label': str(i), 'score': value, 'compare_with_practice_score': compare,
             'score_context': '', 'note': '', 'sort_order': i}
            for i, (value, compare) in enumerate([
                ('27.125', True), ('27.1251', True), ('0', True), ('1', False), ('27.125', True)])]}
        original = deepcopy(metadata)
        self.assertEqual([r['is_reached'] for r in evaluate_score_milestones(metadata, 27.125, True)['milestones']],
                         [True, False, True, None, True])
        self.assertEqual([r['is_reached'] for r in evaluate_score_milestones(metadata, 0, True)['milestones']],
                         [False, False, True, None, False])
        guest = evaluate_score_milestones(metadata, 100, False)
        self.assertFalse(guest['show_status'])
        self.assertTrue(all(row['is_reached'] is None for row in guest['milestones']))
        self.assertEqual(metadata, original)

    def test_highest_reached_uses_score_and_only_enabled_reached_milestones(self):
        metadata = {'milestones': [
            {'id': i, 'label': label, 'score': score, 'compare_with_practice_score': enabled}
            for i, (label, score, enabled) in enumerate([
                ('Low', '9', True), ('High', '27.125', True),
                ('Disabled', '28', False), ('Not reached', '27.1251', True),
                ('Equal', '27.125', True),
            ])
        ]}
        result = evaluate_score_milestones(metadata, 27.125, True)
        self.assertEqual(result['highest_reached']['label'], 'High')
        self.assertIsNone(evaluate_score_milestones(metadata, 0, True)['highest_reached'])
        self.assertIsNone(evaluate_score_milestones(metadata, 100, False)['highest_reached'])
        self.assertNotIn('highest_reached', metadata)
        metadata['milestones'] = [{'id': 0, 'label': 'Zero', 'score': '0',
                                   'compare_with_practice_score': True}]
        self.assertEqual(evaluate_score_milestones(metadata, 0, True)['highest_reached']['label'], 'Zero')

    def test_milestone_interface_translations(self):
        with override('en'):
            self.assertEqual(gettext('Xem mốc điểm tham khảo'), 'View reference score milestones')
            self.assertEqual(gettext('Đối chiếu mốc điểm'), 'Compare score milestones')
            self.assertEqual(gettext('Có mốc điểm tham khảo'), 'Reference score milestones available')
            self.assertEqual(gettext('Hủy'), 'Cancel')
            self.assertEqual(str(ExamScoreMilestone._meta.get_field('label').verbose_name), 'Milestone name')
            self.assertEqual(str(ExamTag._meta.get_field('milestone_source_note').verbose_name), 'Source notes (private)')
        with override('vi'):
            self.assertEqual(gettext('Xem mốc điểm tham khảo'), 'Xem mốc điểm tham khảo')
            self.assertEqual(str(ExamScoreMilestone._meta.get_field('label').verbose_name), 'Tên mốc')

    def test_decimal_input_validation(self):
        field = ExamScoreMilestoneForm.base_fields['score']
        for value in ('38,5', '38.5'):
            self.assertEqual(field.clean(value), Decimal('38.5'))
        for value in ('', '-1', 'NaN', 'Infinity', '38.12345', '100000000', '1,000.5'):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                field.clean(value)
        self.assertEqual(field.clean('0'), 0)
        with override('vi'):
            self.assertEqual(format_milestone_score('27.1250'), '27,125')
            self.assertEqual(format_milestone_score('0.0000'), '0')


class MilestoneTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        setting = override_settings(CLUE_EXAMS_SNAPSHOT_ROOT=self.tmp.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.exam = ExamTag.objects.create(slug='milestones', name='Milestones', milestone_score_context='Thang 40',
                                           milestone_source_note='PRIVATE-SOURCE-SECRET')
        self.milestone = ExamScoreMilestone.objects.create(exam_tag=self.exam, label='Top 32', score='27.1250',
                                                          compare_with_practice_score=True)
        self.factory = RequestFactory()

    def formset(self, context='', active=True, delete=False, own=''):
        self.exam.milestone_score_context = context
        cls = inlineformset_factory(ExamTag, ExamScoreMilestone, form=ExamScoreMilestoneForm,
                                    extra=0)
        data = {'score_milestones-TOTAL_FORMS': '1', 'score_milestones-INITIAL_FORMS': '1',
                'score_milestones-0-id': str(self.milestone.pk), 'score_milestones-0-label': ' Top 32 ',
                'score_milestones-0-score': '38,5', 'score_milestones-0-score_context': own,
                'score_milestones-0-sort_order': '0'}
        if active:
            data['score_milestones-0-is_active'] = 'on'
        if delete:
            data['score_milestones-0-DELETE'] = 'on'
        return cls(data, instance=self.exam)

    def test_context_is_optional_including_active_milestones(self):
        self.assertTrue(self.formset().is_valid())
        self.assertTrue(self.formset(context='Thang 40').is_valid())
        self.assertTrue(self.formset(own='Thang 50').is_valid())
        self.assertTrue(self.formset(active=False).is_valid())
        self.assertTrue(self.formset(delete=True).is_valid())
        self.milestone.label = '  '
        with self.assertRaises(ValidationError):
            self.milestone.full_clean()

    def test_single_explanation_preserves_legacy_context_on_edit(self):
        self.milestone.score_context = 'Thang 40'
        self.milestone.note = 'Công thức cũ'
        self.milestone.save()
        initial = ExamScoreMilestoneForm(instance=self.milestone)
        self.assertNotIn('score_context', initial.fields)
        self.assertEqual(initial.initial['note'], 'Thang 40\nCông thức cũ')
        form = ExamScoreMilestoneForm({
            'exam_tag': self.exam.pk, 'label': 'Điểm chuẩn', 'score': '13,5',
            'note': initial.initial['note'], 'sort_order': 0, 'is_active': True,
        }, instance=self.milestone)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.score_context, '')
        self.assertEqual(saved.note, 'Thang 40\nCông thức cũ')

    def test_snapshot_allowlist_order_precision_and_constant_queries(self):
        ExamScoreMilestone.objects.create(exam_tag=self.exam, label='Hidden', score=0, is_active=False,
                                          note='HIDDEN-NOTE')
        first = ExamScoreMilestone.objects.create(exam_tag=self.exam, label='First', score='27.1251', sort_order=-1)
        with self.assertNumQueries(3):
            payload = build_exam_snapshots()
        reference = payload['items'][0]['score_reference']
        self.assertEqual([r['id'] for r in reference['milestones']], [first.pk, self.milestone.pk])
        self.assertEqual([r['score'] for r in reference['milestones']], ['27.1251', '27.125'])
        self.assertEqual(reference, load_exam_detail_snapshot(self.exam.slug)['score_reference'])
        for marker in ('PRIVATE-SOURCE-SECRET', 'HIDDEN-NOTE', 'is_reached', 'user_progress'):
            self.assertNotIn(marker, json.dumps(payload))
        for i in range(5):
            exam = ExamTag.objects.create(slug='extra-%s' % i, name='Extra')
            ExamScoreMilestone.objects.create(exam_tag=exam, label='Zero', score=0, score_context='Thang 10')
        with self.assertNumQueries(3):
            build_exam_snapshots()

    def test_null_private_and_deleted_detail(self):
        self.milestone.is_active = False
        self.milestone.save()
        self.assertIsNone(build_exam_snapshots()['items'][0]['score_reference'])
        self.exam.is_public = False
        self.exam.save()
        self.assertEqual(build_exam_snapshots()['items'], [])
        self.assertIsNone(load_current_exam_snapshot(self.exam.slug))
        self.exam.delete()
        build_exam_snapshots()
        self.assertIsNone(load_exam_detail_snapshot('milestones'))

    def test_old_schema_rebuilt_once_and_missing_slug_not_rebuilt(self):
        _atomic_write_json(exams_index_path(), {'items': []})
        _atomic_write_json(exam_detail_path(self.exam.slug), {'slug': self.exam.slug})
        from judge.utils.exams import _build_exam_snapshots
        with patch('judge.utils.exams._build_exam_snapshots', wraps=_build_exam_snapshots) as build:
            self.assertEqual(load_current_exam_snapshot(self.exam.slug)['schema_version'], 2)
            self.assertEqual(load_current_exam_snapshot()['schema_version'], 2)
            self.assertIsNone(load_current_exam_snapshot('unknown'))
            self.assertIsNone(load_current_exam_snapshot('unknown'))
            self.assertEqual(build.call_count, 1)

    def test_commit_rollback_and_edits_during_build_are_not_lost(self):
        with patch('judge.signals.rebuild_exams_snapshots.apply_async') as dispatch:
            dispatch.return_value.id = 'test'
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        self.milestone.label = 'Rolled back'
                        self.milestone.save()
                        raise ValueError()
                except ValueError:
                    pass
            dispatch.assert_not_called()
            with self.captureOnCommitCallbacks(execute=True):
                self.milestone.label = 'Committed'
                self.milestone.save()
            self.assertEqual(dispatch.call_count, 1)
            original_write = _atomic_write_json
            changed = False
            def write(path, data):
                nonlocal changed
                if not changed:
                    changed = True
                    with self.captureOnCommitCallbacks(execute=True):
                        self.milestone.label = 'Latest edit during build'
                        self.milestone.save()
                original_write(path, data)
            with patch('judge.utils.exams._atomic_write_json', side_effect=write):
                build_exam_snapshots()
            self.assertEqual(dispatch.call_count, 2)
            from judge.tasks.exams import rebuild_exams_snapshots
            rebuild_exams_snapshots.run()
            self.assertEqual(load_current_exam_snapshot()['items'][0]['score_reference']['milestones'][0]['label'],
                             'Latest edit during build')

    def test_revision_follows_inline_and_restores_deleted_rows(self):
        with reversion.create_revision():
            self.exam.save()
        version = Version.objects.get_for_object(self.exam).first()
        self.milestone.delete()
        ExamScoreMilestone.objects.create(exam_tag=self.exam, label='Extra', score=1)
        with patch('judge.signals.rebuild_exams_snapshots.apply_async') as task, self.captureOnCommitCallbacks(execute=True):
            task.return_value.id = 'revision'
            version.revision.revert(delete=True)
        self.assertTrue(task.called)
        self.assertEqual(list(self.exam.score_milestones.values_list('label', flat=True)), ['Top 32'])

    def test_admin_staff_cannot_see_write_delete_or_revert_metadata(self):
        staff = User.objects.create_user(username='milestone-staff', is_staff=True)
        staff.user_permissions.add(*Permission.objects.filter(content_type__app_label='judge',
                                                             codename__in=['change_examtag', 'delete_examtag', 'add_examtag']))
        request = self.factory.get('/admin/judge/examtag/')
        request.user = staff
        model_admin = admin.site._registry[ExamTag]
        form = model_admin.get_form(request, self.exam)
        self.assertNotIn('milestone_source_note', form.base_fields)
        self.assertTrue(model_admin.has_change_permission(request, self.exam))
        self.assertFalse(model_admin.has_delete_permission(request, self.exam))
        self.assertEqual(len(model_admin.get_inline_instances(request, self.exam)), 0)
        for method, args in [('history_view', [str(self.exam.pk)]), ('revision_view', [str(self.exam.pk), '1']),
                             ('recover_view', ['1']), ('recoverlist_view', [])]:
            with self.subTest(method=method), self.assertRaises(PermissionDenied):
                getattr(model_admin, method)(request, *args)
        for data in ({'milestone_note': 'Forged'}, {'score_milestones-TOTAL_FORMS': '1'}):
            post = self.factory.post('/admin/judge/examtag/', data)
            post.user = staff
            with self.assertRaises(PermissionDenied):
                model_admin.changeform_view(post, str(self.exam.pk))
        with self.assertRaises(PermissionDenied):
            model_admin.delete_queryset(request, ExamTag.objects.filter(pk=self.exam.pk))
        staff.is_superuser = True
        self.assertIn('milestone_source_note', model_admin.get_form(request, self.exam).base_fields)
        self.assertTrue(model_admin.has_delete_permission(request, self.exam))

    def test_api_has_no_private_or_personal_data_and_html_escapes(self):
        self.milestone.label = '<script>alert(1)</script>'
        self.milestone.note = '<img src=x onerror=alert(1)>'
        self.milestone.save()
        build_exam_snapshots()
        request = self.factory.get('/api/exams/list')
        request.user = SimpleNamespace(is_authenticated=True, is_superuser=True)
        for response in (ExamsListApiView.as_view()(request),
                         ExamDetailApiView.as_view()(request, slug=self.exam.slug)):
            text = response.content.decode()
            self.assertNotIn('PRIVATE-SOURCE-SECRET', text)
            self.assertNotIn('is_reached', text)
            self.assertIn('compare_with_practice_score', text)
        reference = load_current_exam_snapshot()['items'][0]['score_reference']
        for authenticated in (False, True):
            html = render_to_string('exams/score-milestones.html', {
                'reference': evaluate_score_milestones(reference, 30, authenticated),
                'prefix': 'test-1', 'heading_id': 'score-milestones',
            })
            self.assertNotIn('<script>', html)
            self.assertIn('&lt;script&gt;', html)
            self.assertNotIn('PRIVATE-SOURCE-SECRET', html)
            self.assertEqual('✓' in html, authenticated)

    def test_list_and_detail_share_progress_and_guest_default(self):
        user = User.objects.create_user(username='milestone-user')
        Profile.objects.create(user=user)
        ExamUserProgress.objects.create(user=user.profile, exam_tag=self.exam, earned_points=27.125,
                                        total_points=40, percent=67.8)
        build_exam_snapshots()
        request = self.factory.get('/exams-list/')
        request.user = user
        view = ExamsListView()
        view.request = request
        list_reference = view.get_context_data()['items'][0]['score_reference_view']
        detail = ExamDetailView()
        detail.request = request
        detail.kwargs = {'slug': self.exam.slug}
        self.assertEqual(detail.get_context_data()['exam']['score_reference_view'], list_reference)
        self.assertTrue(list_reference['milestones'][0]['is_reached'])
        request.user = AnonymousUser()
        self.assertIsNone(detail.get_context_data()['exam']['score_reference_view']['milestones'][0]['is_reached'])
