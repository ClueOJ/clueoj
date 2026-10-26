import importlib
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.apps import apps
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings

from judge.admin.exam import ExamTagAdminForm
from judge.forms import ProblemImportPolygonForm
from judge.models import ExamHostLocation, ExamProvince, ExamTag
from judge.utils.exams import build_exam_snapshots, load_current_exam_snapshot
from judge.views.exams import ExamsListView
from judge.views.problem import ProblemImportPolygon


class ExamHostLocationTests(TestCase):
    def setUp(self):
        queue = patch('judge.signals.queue_exams_snapshot_rebuild')
        self.queue = queue.start()
        self.addCleanup(queue.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = override_settings(CLUE_EXAMS_SNAPSHOT_ROOT=tmp.name)
        settings.enable()
        self.addCleanup(settings.disable)
        ExamProvince.objects.create(name='Toàn quốc', sort_order=3)
        ExamProvince.objects.create(name='Hà Nội', sort_order=7, is_active=False)
        self.host = ExamHostLocation.objects.create(name='Bắc Ninh', sort_order=2)
        self.inactive_host = ExamHostLocation.objects.create(name='Inactive host', is_active=False)
        self.exam = ExamTag.objects.create(slug='host-test', name='DHBB test', year=2026, province='Toàn quốc')

    def test_migration_copies_all_options_without_assigning_exams_or_removing_scopes(self):
        migration = importlib.import_module('judge.migrations.0246_exam_host_locations')
        scopes = list(ExamProvince.objects.order_by('name').values_list('name', 'sort_order', 'is_active'))
        migration.copy_scope_options(apps, SimpleNamespace(connection=connection))
        self.assertEqual(
            list(ExamHostLocation.objects.filter(name__in=[row[0] for row in scopes]).order_by('name')
                 .values_list('name', 'sort_order', 'is_active')),
            scopes,
        )
        self.assertEqual(list(ExamProvince.objects.order_by('name').values_list('name', 'sort_order', 'is_active')), scopes)
        self.exam.refresh_from_db()
        self.assertEqual(self.exam.province, 'Toàn quốc')
        self.assertEqual(self.exam.host_location, '')
        # Re-running the seed must not overwrite later edits in the independent catalog.
        ExamHostLocation.objects.filter(name='Toàn quốc').update(sort_order=99)
        migration.copy_scope_options(apps, SimpleNamespace(connection=connection))
        self.assertEqual(ExamHostLocation.objects.get(name='Toàn quốc').sort_order, 99)

    def _admin_data(self, host=''):
        return {'slug': self.exam.slug, 'name': self.exam.name, 'year': 2026, 'province': 'Toàn quốc',
                'host_location': host, 'expected_count': 0, 'sort_order': 0, 'day_count': 0, 'is_public': 'on'}

    def test_admin_leaves_host_blank_until_selected(self):
        form = ExamTagAdminForm(self._admin_data(), instance=self.exam)
        self.assertEqual(list(form.fields['host_location'].choices)[0][0], '')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().host_location, '')
        form = ExamTagAdminForm(self._admin_data(self.host.name), instance=self.exam)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual((saved.province, saved.host_location), ('Toàn quốc', 'Bắc Ninh'))

    def test_inactive_option_is_preserved_on_edit_but_not_offered_for_new_selection(self):
        new_form = ExamTagAdminForm()
        with self.assertRaises(ValidationError):
            new_form.fields['host_location'].clean(self.inactive_host.name)
        self.exam.host_location = self.inactive_host.name
        self.exam.save()
        form = ExamTagAdminForm(self._admin_data(self.inactive_host.name), instance=self.exam)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().host_location, self.inactive_host.name)

    def test_polygon_host_is_optional_and_not_exposed_to_organization_imports(self):
        user = SimpleNamespace(is_superuser=True)
        form = ProblemImportPolygonForm(user=user)
        field = form.fields['new_exam_tag_host_location']
        self.assertEqual(field.clean(''), '')
        self.assertEqual(field.clean(self.host.name), self.host.name)
        with self.assertRaises(ValidationError):
            field.clean(self.inactive_host.name)
        for kwargs in ({'user': user, 'org_pk': 1}, {'user': SimpleNamespace(is_superuser=False)}):
            self.assertNotIn('new_exam_tag_host_location', ProblemImportPolygonForm(**kwargs).fields)

    def test_polygon_creator_saves_host_without_inferring_it_from_scope(self):
        view = ProblemImportPolygon()
        view.request = SimpleNamespace(user=SimpleNamespace(is_superuser=True))
        for index, host in enumerate(('', self.host.name)):
            form = SimpleNamespace(cleaned_data={
                'create_exam_tag': True, 'new_exam_tag_slug': 'import-host-%s' % index,
                'new_exam_tag_name': 'Imported exam', 'new_exam_tag_province': 'Toàn quốc',
                'new_exam_tag_host_location': host,
            })
            exam = view._create_exam_tag_from_form(form)
            self.assertEqual((exam.province, exam.host_location), ('Toàn quốc', host))

    def test_snapshots_refresh_host_and_legacy_filter_still_only_matches_scope(self):
        self.exam.host_location = self.host.name
        self.queue.reset_mock()
        self.exam.save()
        self.queue.assert_called_once()
        item = build_exam_snapshots()['items'][0]
        self.assertEqual((item['province'], item['host_location']), ('Toàn quốc', 'Bắc Ninh'))
        self.assertEqual(load_current_exam_snapshot(self.exam.slug)['host_location'], 'Bắc Ninh')
        view = ExamsListView()
        for scope, expected in (('Toàn quốc', [item]), ('Bắc Ninh', [])):
            view.request = RequestFactory().get('/exams-list/', {'province': scope})
            self.assertEqual(view._filter_items([item]), expected)
        self.exam.host_location = ''
        self.exam.save()
        build_exam_snapshots()
        self.assertEqual(load_current_exam_snapshot(self.exam.slug)['host_location'], '')

    def test_host_filter_matches_location_independently_and_combines_with_scope(self):
        items = [
            {'id': 1, 'province': 'Toàn quốc', 'host_location': 'Bắc Ninh'},
            {'id': 2, 'province': 'Bắc Ninh', 'host_location': 'Hà Nội'},
            {'id': 3, 'province': 'Bắc Ninh', 'host_location': 'Bắc Ninh'},
            {'id': 4, 'province': 'Bắc Ninh', 'host_location': ''},
            {'id': 5, 'province': 'Bắc Ninh'},
        ]
        view = ExamsListView()
        for query, expected in (
            ({}, [1, 2, 3, 4, 5]),
            ({'host_location': ' Bắc Ninh '}, [1, 3]),
            ({'province': 'Bắc Ninh'}, [2, 3, 4, 5]),
            ({'province': 'Toàn quốc', 'host_location': 'Bắc Ninh'}, [1]),
            ({'province': 'Bắc Ninh', 'host_location': 'Bắc Ninh'}, [3]),
            ({'host_location': 'Unknown host'}, []),
        ):
            with self.subTest(query=query):
                view.request = RequestFactory().get('/exams-list/', query)
                self.assertEqual([item['id'] for item in view._filter_items(items)], expected)

    def test_host_filter_choices_and_pagination_preserve_selected_location(self):
        view = ExamsListView()
        choices = view._host_location_choices('')
        self.assertIn((self.host.name, self.host.name), choices)
        self.assertNotIn((self.inactive_host.name, self.inactive_host.name), choices)
        self.assertIn((self.inactive_host.name, self.inactive_host.name),
                      view._host_location_choices(self.inactive_host.name))
        items = [
            {'id': index, 'name': 'Exam %s' % index, 'province': 'Toàn quốc', 'host_location': self.host.name}
            for index in range(26)
        ]
        view.request = RequestFactory().get('/exams-list/', {
            'host_location': self.host.name, 'province': 'Toàn quốc', 'page': 2,
            'compare_milestones': 1, 'hide_completed': 1,
        })
        view.request.user = AnonymousUser()
        with patch('judge.views.exams._normalize_payload', return_value={'items': items}):
            context = view.get_context_data()
        self.assertEqual(context['paginator'].count, 26)
        self.assertEqual(len(context['items']), 1)
        self.assertEqual(context['filters']['host_location'], self.host.name)
        self.assertTrue(context['filters']['compare_milestones'])
        self.assertTrue(context['filters']['hide_completed'])
        query = parse_qs(urlsplit(context['page_prefix'] + '1').query)
        self.assertEqual(query['host_location'], [self.host.name])
        self.assertEqual(query['province'], ['Toàn quốc'])
        self.assertEqual(query['page'], ['1'])
