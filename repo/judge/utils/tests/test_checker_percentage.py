import io
import json
import tempfile
import zipfile
from unittest import mock
from types import SimpleNamespace

import yaml
from django.test import SimpleTestCase, TestCase
from lxml import etree as ET

from judge.models import Problem, ProblemData, ProblemTestCase
from judge.models.tests.util import create_problem
from judge.utils.polygon_import import parse_assets
from judge.utils.problem_data import ProblemDataCompiler
from judge.views.problem_data import ProblemDataForm


class CheckerPercentageTestCase(SimpleTestCase):
    def test_polygon_custom_checker_percentage(self):
        for attribute, expected in [('true', True), ('false', None), (None, None)]:
            with self.subTest(attribute=attribute), tempfile.TemporaryDirectory() as directory:
                root = ET.fromstring(
                    '<problem><judging/><assets><checker type="testlib">'
                    '<source path="check.cpp"/></checker></assets></problem>'
                )
                if attribute is not None:
                    root.find('judging').set('treat-points-from-checker-as-percent', attribute)
                meta = {'tmp_dir': SimpleNamespace(name=directory)}
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as package:
                    package.writestr('check.cpp', '// checker')
                    parse_assets(meta, root, package)
                self.assertEqual(meta['checker'], 'bridged')
                self.assertIs(meta['checker_args'].get('treat_checker_points_as_percentage'), expected)

    def test_polygon_without_judging_and_standard_checker(self):
        for checker_name in [None, 'std::wcmp.cpp']:
            with self.subTest(checker_name=checker_name), tempfile.TemporaryDirectory() as directory:
                root = ET.fromstring(
                    '<problem><checker type="testlib"><source path="check.cpp"/></checker></problem>'
                )
                if checker_name:
                    root.find('checker').set('name', checker_name)
                    judging = ET.SubElement(root, 'judging')
                    judging.set('treat-points-from-checker-as-percent', 'true')
                meta = {'tmp_dir': SimpleNamespace(name=directory)}
                with zipfile.ZipFile(io.BytesIO(), 'w') as package:
                    package.writestr('check.cpp', '// checker')
                    parse_assets(meta, root, package)
                self.assertNotIn('treat_checker_points_as_percentage', meta.get('checker_args', {}))

    def test_form_arguments_survive_yaml_generation(self):
        for percentage in [True, False, None]:
            with self.subTest(percentage=percentage):
                args = {'files': 'checker.cpp', 'lang': 'CPP17', 'type': 'testlib'}
                if percentage is not None:
                    args['treat_checker_points_as_percentage'] = percentage
                form = ProblemDataForm()
                form.cleaned_data = {'checker_args': json.dumps(args)}
                data = ProblemData(
                    checker='bridged', custom_checker='percentage/checker.cpp',
                    checker_args=form.clean_checker_args(),
                )
                case = ProblemTestCase(
                    type='C', input_file='1.in', output_file='1.out', points=10, is_pretest=False,
                )
                case.save = mock.Mock()
                cases = mock.MagicMock()
                cases.count.return_value = 1
                cases.__iter__.side_effect = lambda: iter([case])
                init = ProblemDataCompiler(Problem(code='percentage'), data, cases, {'1.in', '1.out'}).make_init()
                result = yaml.safe_load(yaml.safe_dump(init))
                self.assertEqual(result['checker'], {'name': 'bridged', 'args': args})
                self.assertEqual(result['test_cases'][0]['points'], 10)


class CheckerPercentagePersistenceTestCase(TestCase):
    def test_form_save_reload_and_compile(self):
        problem = create_problem('checker_percentage')
        data, _ = ProblemData.objects.get_or_create(problem=problem)
        data.custom_checker = 'checker_percentage/checker.cpp'
        data.save()
        case = ProblemTestCase.objects.create(
            dataset=problem, order=0, points=10, is_pretest=False,
            input_file='1.in', output_file='1.out',
        )
        for percentage in [True, False]:
            with self.subTest(percentage=percentage):
                args = {
                    'files': 'checker.cpp', 'lang': 'CPP17', 'type': 'testlib',
                    'treat_checker_points_as_percentage': percentage,
                }
                form = ProblemDataForm(instance=data, data={
                    'grader': 'standard', 'checker': 'bridged', 'checker_type': 'testlib',
                    'checker_args': json.dumps(args), 'io_method': 'standard',
                })
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                data.refresh_from_db()
                self.assertEqual(json.loads(data.checker_args), args)
                init = ProblemDataCompiler(
                    problem, data, ProblemTestCase.objects.filter(pk=case.pk), {'1.in', '1.out'},
                ).make_init()
                self.assertEqual(yaml.safe_load(yaml.safe_dump(init))['checker']['args'], args)
