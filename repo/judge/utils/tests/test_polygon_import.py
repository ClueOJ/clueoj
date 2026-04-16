import io
import os
import tempfile
import zipfile
from unittest import mock

from django.core.management.base import CommandError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from lxml import etree as ET

from judge.forms import ProblemImportPolygonForm
from judge.models import ProblemTranslation, Solution
from judge.models.tests.util import create_organization, create_problem, create_problem_group, create_problem_type, create_user
from judge.utils.organization import get_organization_code_prefix
from judge.utils.polygon_import import import_polygon_package, parse_solutions, update_or_create_problem


class OrganizationCodePrefixTestCase(SimpleTestCase):
    def test_prefix_matches_existing_rule(self):
        self.assertEqual(get_organization_code_prefix('Open-123_School'), 'openschool_')


class ProblemImportPolygonFormTestCase(TestCase):
    def _make_package(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as zf:
            zf.writestr('problem.xml', '<problem/>')
        return SimpleUploadedFile('package.zip', stream.getvalue(), content_type='application/zip')

    @classmethod
    def setUpTestData(cls):
        cls.org = create_organization(name='org-prefix', slug='Org-123')
        cls.user = create_user(username='normal-user')
        cls.superuser = create_user(username='super-user', is_superuser=True, is_staff=True)

    def test_requires_org_prefix_for_non_superuser(self):
        form = ProblemImportPolygonForm(
            data={'code': 'wrong_prefix_problem'},
            files={'package': self._make_package()},
            org_pk=self.org.pk,
            user=self.user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('code', form.errors)

    def test_allows_prefixed_code_for_non_superuser(self):
        form = ProblemImportPolygonForm(
            data={'code': 'org_problem'},
            files={'package': self._make_package()},
            org_pk=self.org.pk,
            user=self.user,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_allows_non_prefixed_code_for_superuser(self):
        form = ProblemImportPolygonForm(
            data={'code': 'any_problem'},
            files={'package': self._make_package()},
            org_pk=self.org.pk,
            user=self.superuser,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_sets_do_update_when_code_is_fixed(self):
        form = ProblemImportPolygonForm(
            code='existing_problem',
            user=self.superuser,
        )
        self.assertEqual(form.fields['do_update'].initial, True)
        self.assertIn('override_statements', form.fields)

    def test_hides_override_statements_for_create_import(self):
        form = ProblemImportPolygonForm(user=self.superuser)
        self.assertNotIn('override_statements', form.fields)


class ImportPolygonPackageUpdateModeTestCase(TestCase):
    def _build_package(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as zf:
            zf.writestr('problem.xml', '<problem/>')
        stream.seek(0)
        return stream

    @mock.patch('judge.utils.polygon_import.update_or_create_problem', return_value=object())
    @mock.patch('judge.utils.polygon_import.parse_solutions')
    @mock.patch('judge.utils.polygon_import.parse_statements')
    @mock.patch('judge.utils.polygon_import.parse_tests')
    @mock.patch('judge.utils.polygon_import.parse_assets')
    @mock.patch('judge.utils.polygon_import.validate_pandoc')
    def test_existing_problem_without_do_update_raises(
        self,
        _validate_pandoc,
        _parse_assets,
        _parse_tests,
        _parse_statements,
        _parse_solutions,
        _update_or_create_problem,
    ):
        create_user('import-update-user')
        create_organization('import-update-org')
        from judge.models.tests.util import create_problem
        create_problem('existing_problem')

        with self.assertRaises(CommandError):
            import_polygon_package(
                self._build_package(),
                problem_code='existing_problem',
                interactive=False,
            )

        _update_or_create_problem.assert_not_called()

    @mock.patch('judge.utils.polygon_import.update_or_create_problem', return_value=object())
    @mock.patch('judge.utils.polygon_import.parse_solutions')
    @mock.patch('judge.utils.polygon_import.parse_statements')
    @mock.patch('judge.utils.polygon_import.parse_tests')
    @mock.patch('judge.utils.polygon_import.parse_assets')
    @mock.patch('judge.utils.polygon_import.validate_pandoc')
    def test_existing_problem_with_do_update_calls_update(
        self,
        _validate_pandoc,
        _parse_assets,
        _parse_tests,
        _parse_statements,
        _parse_solutions,
        _update_or_create_problem,
    ):
        from judge.models.tests.util import create_problem
        create_problem('existing_problem_update')

        import_polygon_package(
            self._build_package(),
            problem_code='existing_problem_update',
            interactive=False,
            do_update=True,
            append_main_solution_to_tutorial=True,
        )

        _parse_solutions.assert_called_once()
        _update_or_create_problem.assert_called_once()

    @mock.patch('judge.utils.polygon_import.update_or_create_problem', return_value=object())
    @mock.patch('judge.utils.polygon_import.parse_solutions')
    @mock.patch('judge.utils.polygon_import.parse_statements')
    @mock.patch('judge.utils.polygon_import.parse_tests')
    @mock.patch('judge.utils.polygon_import.parse_assets')
    @mock.patch('judge.utils.polygon_import.validate_pandoc')
    def test_do_update_without_override_statements_skips_statement_parsing(
        self,
        _validate_pandoc,
        _parse_assets,
        _parse_tests,
        _parse_statements,
        _parse_solutions,
        _update_or_create_problem,
    ):
        from judge.models.tests.util import create_problem
        create_problem('existing_problem_keep_statement')

        import_polygon_package(
            self._build_package(),
            problem_code='existing_problem_keep_statement',
            interactive=False,
            do_update=True,
            override_statements=False,
            append_main_solution_to_tutorial=True,
        )

        _parse_statements.assert_not_called()
        _parse_solutions.assert_not_called()
        _update_or_create_problem.assert_called_once()


class ParseSolutionsTestCase(SimpleTestCase):
    def _build_package(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as zf:
            zf.writestr('main.cpp', '#include <bits/stdc++.h>\nint main() { return 0; }\n')
        stream.seek(0)
        return zipfile.ZipFile(stream, 'r')

    def test_append_main_solution_to_tutorial_when_enabled(self):
        root = ET.fromstring(
            b"""
            <problem>
                <solutions>
                    <solution tag="main">
                        <source type="cpp.g++17" path="main.cpp" />
                    </solution>
                </solutions>
            </problem>
            """
        )
        problem_meta = {
            'interactive': False,
            'append_main_solution_to_tutorial': True,
            'tutorial': 'Base tutorial',
        }

        with self._build_package() as package:
            parse_solutions(problem_meta, root, package)

        self.assertIn('<blockquote class="spoiler">', problem_meta['tutorial'])
        self.assertIn('```cpp', problem_meta['tutorial'])
        self.assertIn('int main()', problem_meta['tutorial'])

    def test_skip_append_when_disabled(self):
        root = ET.fromstring(
            b"""
            <problem>
                <solutions>
                    <solution tag="main">
                        <source type="cpp.g++17" path="main.cpp" />
                    </solution>
                </solutions>
            </problem>
            """
        )
        problem_meta = {
            'interactive': False,
            'append_main_solution_to_tutorial': False,
            'tutorial': 'Base tutorial',
        }

        with self._build_package() as package:
            parse_solutions(problem_meta, root, package)

        self.assertEqual(problem_meta['tutorial'], 'Base tutorial')


class UpdateOrCreateProblemTestCase(TestCase):
    def _make_zip_file_path(self):
        fd, path = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('grader.py', 'print("ok")\n')
        return path

    def _build_problem_meta(self, **overrides):
        zip_path = self._make_zip_file_path()
        self.addCleanup(lambda: os.path.exists(zip_path) and os.remove(zip_path))
        meta = {
            'code': 'existing_problem_meta',
            'name': 'Imported Name',
            'time_limit': 2.0,
            'memory_limit': 262144,
            'description': 'Imported description',
            'partial': False,
            'authors': [],
            'curators': [],
            'translations': [],
            'tutorial': 'Imported tutorial',
            'zipfile': zip_path,
            'grader': '',
            'checker': '',
            'grader_args': {},
            'batches': {},
            'cases_data': {},
            'normal_cases': [],
            'organization': None,
            'do_update': True,
            'override_statements': False,
        }
        meta.update(overrides)
        return meta

    @mock.patch('judge.utils.polygon_import.ProblemDataCompiler.generate')
    @mock.patch('judge.utils.polygon_import._sync_problem_testcases')
    def test_update_mode_preserves_points_group_type_and_statements(
        self,
        _sync_problem_testcases,
        _generate,
    ):
        create_problem_group('origgrp')
        create_problem_type('origtype')
        problem = create_problem(
            'existing_problem_meta',
            name='Original Name',
            description='Original description',
            points=77,
            group='origgrp',
            types=['origtype'],
        )
        ProblemTranslation.objects.create(
            problem=problem,
            language='vi',
            name='Original VI Name',
            description='Original VI Description',
        )
        Solution.objects.create(
            problem=problem,
            is_public=False,
            publish_on=timezone.now(),
            content='Original tutorial',
        )

        update_or_create_problem(self._build_problem_meta())
        problem.refresh_from_db()

        self.assertEqual(problem.points, 77)
        self.assertEqual(problem.group.name, 'origgrp')
        self.assertEqual(problem.name, 'Original Name')
        self.assertEqual(problem.description, 'Original description')
        self.assertEqual(
            list(problem.types.values_list('name', flat=True)),
            ['origtype'],
        )
        self.assertEqual(
            ProblemTranslation.objects.filter(problem=problem).values_list('language', flat=True).get(),
            'vi',
        )
        self.assertEqual(problem.solution.content, 'Original tutorial')
