import copy
import io
import os
import shutil
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

import yaml
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.bridge.judge_list import InvalidSubmission
from judge.models import Language, Problem, ProblemData, Submission, SubmissionSource, SubmissionTestCase
from judge.models import problem_data_storage
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.tasks.subtask_scoring import _dispatch, _rebuild_step, rebuild_subtask_scores, set_subtask_scoring
from judge.utils.batch_scoring import finalize_batch_results, scoring_dataset, scoring_snapshot, save_batch_policies
from judge.utils.manual_subtasks import validate_manual_files
from judge.utils.subtask_scoring import cumulative_breakdown, validate_configuration


class ManualSubtaskTests(CommonDataMixin, TestCase):
    def setUp(self):
        # Bridge normally owns a long-lived connection. Keep TestCase's enclosing
        # transaction open when exercising that code in this process.
        connection_guard = patch('judge.bridge.judge_handler._ensure_connection')
        connection_guard.start()
        self.addCleanup(connection_guard.stop)
        self.problem = create_problem(code='manual_cumulative', is_public=True, partial=True,
                                      is_manually_managed=True, points=100)
        self.profile = self.users['normal'].profile
        self.language = Language.get_python3()
        self.data = ProblemData.objects.create(problem=self.problem, legacy_batch_type='min')
        self.root = Path(problem_data_storage.path(self.problem.code))
        self.root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.root)
        for name, content in [('a.in', '1'), ('a.out', '1'), ('grader.py', '# grader'), ('checker.cpp', '// checker')]:
            (self.root / name).write_text(content)
        self.config = {'custom_judge': 'grader.py', 'checker': {'name': 'bridged', 'args': {'files': 'checker.cpp'}},
                       'test_cases': [self.group(30), self.group(70)]}
        self.write_config()

    def group(self, points, count=2):
        return {'batched': [{'in': 'a.in', 'out': 'a.out', 'points': 1} for _ in range(count)], 'points': points}

    def write_config(self):
        (self.root / 'init.yml').write_text(yaml.safe_dump(self.config, sort_keys=False))

    def enable(self):
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()

    def publish(self):
        self.enable()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'ON')
        self.problem.refresh_from_db()

    def submission(self, ratios=(1, 0), config=None, pretests=False):
        config = config or scoring_snapshot(self.problem)
        sub = Submission.objects.create(problem=self.problem, user=self.profile, language=self.language,
                                        status='D', result='WA', is_pretested=pretests, batch_scoring=config)
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        layout = config['pretests' if pretests else 'full'] or config['full']
        case = 0
        for group, ratio in zip(layout, ratios):
            for _ in range(group['count']):
                case += 1
                maximum = group['total'] / group['count']
                SubmissionTestCase.objects.create(submission=sub, case=case, batch=group['batch'],
                                                  points=maximum * ratio, total=maximum,
                                                  status='AC' if ratio == 1 else 'WA')
        sub.case_points, sub.case_total = finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        sub.points = round(sub.case_points / sub.case_total * self.problem.points, 3)
        sub.save()
        return sub

    def handler(self):
        handler = object.__new__(JudgeHandler)
        handler.name = 'manual-judge'
        handler._dispatch_id = None
        handler._free_self = Mock()
        handler._make_json_log = Mock(return_value={})
        handler._post_update_submission = Mock()
        return handler

    def test_manual_without_database_cases_or_zipfile_can_enable(self):
        self.assertFalse(self.problem.cases.exists())
        self.assertFalse(self.data.zipfile)
        config = validate_configuration(self.problem)
        self.assertEqual(config['full'], [
            {'batch': 1, 'count': 2, 'total': 30, 'mode': 'min'},
            {'batch': 2, 'count': 2, 'total': 70, 'mode': 'min'},
        ])
        self.assertEqual(config['pretests'], [])
        self.assertEqual(config['manual_files']['version'], 1)
        self.publish()
        self.assertEqual(scoring_snapshot(self.problem)['full'], config['full'])

    def test_no_problemdata_record_uses_existing_sum_default(self):
        self.data.delete()
        self.problem = Problem.objects.get(pk=self.problem.pk)
        self.assertEqual([r['mode'] for r in validate_configuration(self.problem)['full']], ['sum', 'sum'])

    def test_off_never_reads_files_or_changes_manual_submission_policy(self):
        (self.root / 'init.yml').unlink()
        with patch('judge.utils.manual_subtasks.manual_fingerprint') as fingerprint:
            snapshot = scoring_snapshot(self.problem)
            validate_manual_files(self.problem)
        fingerprint.assert_not_called()
        self.assertIsNone(snapshot['full'])
        self.assertIsNone(snapshot['pretests'])

    def test_full_rebuild_combines_subtasks_then_disable_returns_to_max(self):
        old = validate_configuration(self.problem)
        first = self.submission((1, 0), config=old)
        second = self.submission((0, 1), config=old)
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 70)
        self.enable()

        def grade(submission, **kwargs):
            self.assertTrue(kwargs['force_full'])
            config = scoring_snapshot(Problem.objects.get(pk=self.problem.pk))
            Submission.objects.filter(pk=submission.pk).update(batch_scoring=config, status='D', is_pretested=False)

        with patch.object(Submission, 'judge', autospec=True, side_effect=grade):
            for sub in (first, second):
                self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'dispatched')
                _dispatch(sub.pk, self.problem.pk, self.problem.subtask_score_revision)
            self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'ON')
        self.problem.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.points, 100)
        self.assertEqual(self.profile.performance_points, 100)
        self.assertEqual(self.profile.problem_count, 0)
        self.assertEqual([r['submission_id'] for r in cumulative_breakdown(self.profile.pk, self.problem)['subtasks']],
                         [first.pk, second.pk])
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'OFF')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.points, 70)

    def test_laserstrike_layout_has_five_independent_min_batches(self):
        maxima = [8, 9, 21, 36, 26]
        counts = [4, 9, 36, 16, 62]
        self.config['test_cases'] = [self.group(points, count) for points, count in zip(maxima, counts)]
        self.write_config()
        self.publish()
        snapshot = scoring_snapshot(self.problem)
        self.assertEqual([r['count'] for r in snapshot['full']], counts)
        self.submission((1, 0, 1, 0, 1))
        self.submission((0, 1, 0, 1, 0))
        score = cumulative_breakdown(self.profile.pk, self.problem)
        self.assertEqual((score['case_points'], score['case_total']), (100, 100))

    def test_mixed_modes_and_pretest_layouts_preserve_weights_and_numbering(self):
        self.config['pretest_test_cases'] = [self.group(5, 1)]
        self.config['test_cases'][1]['batched'][1]['points'] = 3
        self.data.batch_scoring_overrides = {'full:2': 'sum'}
        self.data.save()
        self.write_config()
        self.publish()
        sub = self.submission((0, 1))
        cases = list(sub.test_cases.order_by('case'))
        cases[-1].points = 0
        self.assertEqual(finalize_batch_results(sub, cases), (35, 100))
        self.assertEqual([r.scoring_type for r in sub.batch_results.order_by('batch')], ['min', 'sum'])
        self.assertEqual(scoring_dataset(sub), 'full')
        pretest = self.submission((1,), pretests=True)
        self.assertEqual(scoring_dataset(pretest), 'pretests')
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 35)

    def test_no_pretests_is_an_explicit_empty_list_for_legacy_fallback(self):
        self.publish()
        sub = self.submission(pretests=True)
        self.assertEqual(scoring_dataset(sub), 'full')
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 30)

    def test_archive_and_loose_files_are_both_supported(self):
        content = io.BytesIO()
        with ZipFile(content, 'w') as archive:
            archive.writestr('a.in', '1')
            archive.writestr('a.out', '1')
        (self.root / 'secret.zip').write_bytes(content.getvalue())
        (self.root / 'a.in').unlink()
        (self.root / 'a.out').unlink()
        self.config['archive'] = 'secret.zip'
        self.write_config()
        self.publish()
        validate_manual_files(self.problem)

    def test_missing_or_invalid_archive_init_and_helpers_are_rejected(self):
        for filename in ('init.yml', 'a.in', 'a.out', 'grader.py', 'checker.cpp'):
            with self.subTest(filename=filename):
                p = self.root / filename
                original = p.read_bytes()
                p.unlink()
                with self.assertRaises(ValidationError):
                    validate_configuration(self.problem)
                p.write_bytes(original)
        for content in ('[', '[]', ''):
            (self.root / 'init.yml').write_text(content)
            with self.assertRaises(ValidationError):
                validate_configuration(self.problem)
        self.config['archive'] = 'missing.zip'
        self.write_config()
        with self.assertRaises(ValidationError):
            validate_configuration(self.problem)
        (self.root / 'missing.zip').write_text('not a zip')
        with self.assertRaises(ValidationError):
            validate_configuration(self.problem)

    def test_standalone_tests_including_zero_point_pretests_are_rejected(self):
        for key in ('test_cases', 'pretest_test_cases'):
            for points in (0, 5):
                with self.subTest(dataset=key, points=points):
                    self.config[key] = [{'in': 'a.in', 'out': 'a.out', 'points': points}]
                    self.write_config()
                    with self.assertRaisesMessage(ValidationError, 'Every test'):
                        validate_configuration(self.problem)

    def test_empty_nested_dynamic_and_generated_datasets_are_rejected(self):
        original = copy.deepcopy(self.config)
        variants = [
            {'test_cases': []}, {'test_cases': {}}, {'test_cases': [{'batched': []}]},
            {'test_cases': [{'batched': [self.group(1)], 'points': 1}]},
            {'test_cases+': 'open("do-not-execute", "w")', 'test_cases': original['test_cases']},
            {'generator': 'a.py'}, {'test_cases': [{'batched': [{'generator': 'a.py'}]}]},
            {'test_cases': [dict(self.group(10), dependencies=[1])]},
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                self.config = {**original, **variant}
                self.write_config()
                with self.assertRaises(ValidationError):
                    validate_configuration(self.problem)

    def test_invalid_maxima_weights_and_all_zero_totals_are_rejected(self):
        for maximum in (-1, float('nan'), float('inf'), True, '10'):
            with self.subTest(maximum=maximum):
                self.config['test_cases'] = [self.group(maximum)]
                self.write_config()
                with self.assertRaises(ValidationError):
                    validate_configuration(self.problem)
        for weight in (-1, 0, float('inf'), True):
            self.config['test_cases'] = [self.group(10, 1)]
            self.config['test_cases'][0]['batched'][0]['points'] = weight
            self.write_config()
            with self.assertRaises(ValidationError):
                validate_configuration(self.problem)
        self.config['test_cases'] = [self.group(0)]
        self.write_config()
        with self.assertRaises(ValidationError):
            validate_configuration(self.problem)
        self.config['test_cases'] = [self.group(1e308, 1), self.group(1e308, 1)]
        self.write_config()
        with self.assertRaises(ValidationError):
            validate_configuration(self.problem)

    def test_zero_point_group_is_allowed_with_positive_total(self):
        self.config['test_cases'][0]['points'] = 0
        self.write_config()
        self.assertEqual(validate_configuration(self.problem)['full'][0]['total'], 0)

    def test_inherited_file_paths_and_default_weights_match_judge_semantics(self):
        self.config.update({'in': 'a.in', 'out': 'a.out', 'points': 7})
        self.config['test_cases'] = [{'batched': [{}, {}]}]
        self.write_config()
        self.assertEqual(validate_configuration(self.problem)['full'],
                         [{'batch': 1, 'count': 2, 'total': 7, 'mode': 'min'}])

    def test_recursive_yaml_and_implicit_filename_discovery_are_rejected(self):
        for source in ('test_cases: &cases [*cases]', 'archive: secret.zip',
                       'test_cases: {input_format: "(?P<case>.*)"}'):
            with self.subTest(source=source):
                (self.root / 'init.yml').write_text(source)
                with self.assertRaises(ValidationError):
                    validate_configuration(self.problem)

    def test_paths_and_symlinks_cannot_escape_the_fingerprinted_directory(self):
        for filename in ('../other.in', '/tmp/other.in', 'other\\file.in'):
            self.config['test_cases'][0]['batched'][0]['in'] = filename
            self.write_config()
            with self.assertRaises(ValidationError):
                validate_configuration(self.problem)
        self.config['test_cases'][0]['batched'][0]['in'] = 'a.in'
        self.write_config()
        (self.root / 'link').symlink_to(self.root / 'a.in')
        with self.assertRaisesMessage(ValidationError, 'symlinks'):
            validate_configuration(self.problem)

    def test_unknown_policy_keys_and_modes_are_rejected(self):
        for overrides in ({'full:99': 'min'}, {'pretests:1': 'sum'}, {'full:1': 'invalid'}, ['invalid']):
            self.data.batch_scoring_overrides = overrides
            self.data.save()
            self.problem = Problem.objects.get(pk=self.problem.pk)
            with self.assertRaises(ValidationError):
                validate_configuration(self.problem)

    def test_invalid_auxiliary_checker_configuration_is_rejected(self):
        for helper in (4, {'name': 'bridged', 'args': []},
                       {'name': 'bridged', 'args': {'files': '../outside.cpp'}}):
            self.config['checker'] = helper
            self.write_config()
            with self.assertRaises(ValidationError):
                validate_configuration(self.problem)

    def test_file_content_changes_are_detected_even_with_same_size_and_mtime(self):
        self.publish()
        for filename in ('a.in', 'checker.cpp', 'grader.py'):
            with self.subTest(filename=filename):
                path = self.root / filename
                original = path.read_bytes()
                info = path.stat()
                path.write_bytes(b'x' * len(original))
                os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
                with self.assertRaisesMessage(ValidationError, 'Manual test data changed'):
                    validate_manual_files(self.problem)
                path.write_bytes(original)
                validate_manual_files(self.problem)

    def test_same_count_and_total_but_new_group_order_or_weights_is_detected(self):
        self.publish()
        self.config['test_cases'].reverse()
        self.write_config()
        with self.assertRaises(ValidationError):
            validate_manual_files(self.problem)
        # Dispatch uses the locked layout even if someone changed init.yml outside the web editor.
        self.assertEqual([r['total'] for r in scoring_snapshot(self.problem)['full']], [30, 70])

    def test_added_or_removed_auxiliary_file_changes_the_fingerprint(self):
        self.publish()
        helper = self.root / 'extra.txt'
        helper.write_text('extra helper imported by custom grader')
        with self.assertRaises(ValidationError):
            validate_manual_files(self.problem)
        helper.unlink()
        validate_manual_files(self.problem)
        (self.root / 'a.out').unlink()
        with self.assertRaises(ValidationError):
            validate_manual_files(self.problem)

    def test_changed_archive_is_detected_even_with_identical_layout(self):
        content = io.BytesIO()
        with ZipFile(content, 'w') as archive:
            archive.writestr('a.in', '1')
        (self.root / 'secret.zip').write_bytes(content.getvalue())
        self.config['archive'] = 'secret.zip'
        self.write_config()
        self.publish()
        with ZipFile(self.root / 'secret.zip', 'a') as archive:
            archive.writestr('another.in', '2')
        with self.assertRaises(ValidationError):
            validate_manual_files(self.problem)

    def test_snapshot_is_an_independent_copy_of_locked_layout(self):
        self.publish()
        snapshot = scoring_snapshot(self.problem)
        snapshot['full'][0]['total'] = 999
        self.assertEqual(scoring_snapshot(self.problem)['full'][0]['total'], 30)

    def test_file_change_during_profile_refresh_rolls_back_publication(self):
        self.enable()
        with patch('judge.tasks.subtask_scoring.refresh_problem_profiles',
                   side_effect=lambda *a, **kw: (self.root / 'grader.py').write_text('# changed')):
            with self.assertRaises(ValidationError):
                rebuild_subtask_scores.run(self.problem.pk, self.problem.subtask_score_revision)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'FAILED')

    def test_off_can_be_reached_even_when_manual_directory_is_missing(self):
        self.publish()
        shutil.rmtree(self.root)
        self.root.mkdir()
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'OFF')

    def test_rebuild_cannot_publish_or_retry_changed_files(self):
        self.enable()
        (self.root / 'a.in').write_text('2')
        with self.assertRaises(ValidationError):
            rebuild_subtask_scores.run(self.problem.pk, self.problem.subtask_score_revision)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'FAILED')
        with self.assertRaises(ValidationError):
            self.enable()
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'OFF')
        self.problem.refresh_from_db()
        self.publish()

    def test_web_policy_and_manual_flag_changes_stay_locked(self):
        self.publish()
        with self.assertRaises(ValidationError):
            save_batch_policies(self.problem, {'batch-mode-full:1': 'sum', 'batch-mode-full:2': 'sum'})
        with self.assertRaises(ValidationError):
            Problem.objects.filter(pk=self.problem.pk).update(is_manually_managed=False)
        self.client.force_login(self.users['superuser'])
        response = self.client.post(reverse('problem_data', args=[self.problem.code]),
                                    {'action': 'save-batch-scoring', 'batch-mode-full:1': 'sum'})
        self.assertEqual(response.status_code, 409)

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_bridge_refuses_changed_files_before_sending_to_judge(self, finished, post):
        self.publish()
        sub = self.submission()
        Submission.objects.filter(pk=sub.pk).update(status='QU')
        (self.root / 'a.in').write_text('2')
        handler = self.handler()
        handler.send = Mock()
        with self.assertRaises(InvalidSubmission):
            handler.submit(sub.pk, self.problem.code, self.language.key, 'print(1)')
        handler.send.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.points), ('IE', 0))
        self.assertIn('Manual test data changed', sub.error)
        self.assertFalse(sub.batch_results.exists())

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_change_during_grading_is_not_published(self, finished, post):
        self.publish()
        sub = self.submission((1, 1))
        Submission.objects.filter(pk=sub.pk).update(status='G')
        (self.root / 'grader.py').write_text('# changed')
        self.handler().on_grading_end({'submission-id': sub.pk})
        sub.refresh_from_db()
        self.assertEqual((sub.status, sub.points), ('IE', 0))
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 0)

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_bridge_sends_known_layout_and_rejects_mismatched_result_counts(self, finished, post):
        self.publish()
        sub = self.submission((1, 1))
        handler = self.handler()
        data = handler.get_related_submission_data(sub.pk)
        self.assertEqual(data.batch_scoring['full'], self.problem.subtask_score_config['full'])
        self.assertNotIn('manual_files', data.batch_scoring)  # Not part of the judge protocol.
        sub.test_cases.filter(case=4).delete()
        handler.on_grading_end({'submission-id': sub.pk})
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'IE')

    def test_retry_from_failed_uses_identical_locked_config(self):
        self.enable()
        original = self.problem.subtask_score_config
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='FAILED')
        self.enable()
        self.assertEqual(self.problem.subtask_score_config, original)
        self.assertEqual(self.problem.subtask_score_revision, 2)
        self.assertEqual(scoring_snapshot(self.problem)['cumulative_revision'], 2)

    def test_superuser_permission_and_organization_privacy_still_apply(self):
        self.client.force_login(self.users['staff_problem_edit_all'])
        url = reverse('problem_subtask_scoring', args=[self.problem.code])
        self.assertEqual(self.client.post(url, {'action': 'enable'}).status_code, 403)
        self.problem.is_organization_private = True
        self.problem.save()
        with self.assertRaises(ValidationError):
            self.enable()
