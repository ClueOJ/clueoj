import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from judge.models import Language, Problem, ProblemInvocationSource, ProblemTestCase, Submission, SubmissionTestCase
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.utils.batch_scoring import finalize_batch_results, scoring_snapshot
from judge.utils.invocation_report import build_report, classify, expectation_match
from judge.utils.invocation_storage import decode_source, read_source, source_storage
from judge.utils.invocations import (can_invoke, create_invocation, delete_sources, save_source_settings,
                                     submission_source, update_source, upload_sources)


class InvocationResultTests(SimpleTestCase):
    def cases(self, *codes):
        return [SimpleNamespace(status=code) for code in codes]

    def test_pac_requires_accepted_outputs_not_partial_points_alone(self):
        self.assertEqual(classify(self.cases('AC', 'AC'), 60, 100), 'PAC')
        self.assertEqual(classify(self.cases('AC', 'AC'), 0, 100), 'PAC')
        self.assertEqual(classify(self.cases('AC', 'WA'), 60, 100), 'WA')
        self.assertEqual(classify(self.cases('AC', 'TLE'), 60, 100), 'TLE')

    def test_full_score_uses_representation_tolerance_not_rounding(self):
        self.assertEqual(classify(self.cases('AC'), 99.9999, 100), 'PAC')
        self.assertEqual(classify(self.cases('AC'), 100 - 1e-14, 100), 'AC')
        self.assertEqual(classify(self.cases('AC'), 0, 0), 'AC')
        self.assertEqual(classify(self.cases('AC'), 101, 100), 'IE')
        self.assertEqual(classify(self.cases('AC'), float('nan'), 100), 'IE')

    def test_error_order_and_skips(self):
        self.assertEqual(classify(self.cases('AC', 'WA', 'TLE'), 0, 100), 'TLE')
        self.assertEqual(classify(self.cases('WA', 'SC'), 0, 100, mode='min'), 'WA')
        self.assertEqual(classify(self.cases('SC'), 0, 100, mode='min'), 'SC')
        self.assertEqual(classify(self.cases('AC'), 10, 100, complete=False), 'PENDING')

    def test_matchers(self):
        for result in ('AC', 'PAC', 'TLE'):
            self.assertEqual(expectation_match('AC/TL', result), 'match')
        for result in ('WA', 'TLE', 'RTE', 'MLE', 'OLE', 'IR', 'CE'):
            self.assertEqual(expectation_match('RJ', result), 'match')
        for result in ('AC', 'PAC', 'IE', 'AB', 'SC'):
            self.assertEqual(expectation_match('RJ', result), 'mismatch')
        self.assertEqual(expectation_match('AC', 'G'), 'pending')
        self.assertEqual(expectation_match('SKIP', 'CE'), 'ignored')
        self.assertEqual(expectation_match('', 'WA'), 'ignored')

    def test_reject_invalid_source_text(self):
        for content in (b'', b'\0', b'\xff', b'x' * 65537):
            with self.assertRaises(ValidationError):
                decode_source(content)


@override_settings(STORAGE_CATALOG_SYNC_ENABLED=False, STORAGE_ENSURE_READY_ENABLED=False, STREAKS_ENABLED=False)
class InvocationTests(CommonDataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='matrixqa', is_public=True, partial=True, points=100)
        cls.language = Language.get_python3()
        cls.problem.allowed_languages.add(cls.language, *Language.objects.filter(key__in=('PYPY3', 'PAS')))
        for mode, maximum in (('sum', 40), ('min', 60)):
            order = cls.problem.cases.count()
            ProblemTestCase.objects.create(
                dataset=cls.problem, order=order, type='S', points=maximum, batch_type=mode, is_pretest=False)
            for i in range(2):
                ProblemTestCase.objects.create(
                    dataset=cls.problem, order=order + i + 1, type='C',
                    points=1, input_file='%s.in' % (order + i), output_file='%s.out' % (order + i), is_pretest=False)
            ProblemTestCase.objects.create(dataset=cls.problem, order=order + 3, type='E', is_pretest=False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        setting = override_settings(DMOJ_INVOCATION_SOURCE_ROOT=self.temporary.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.user = self.users['superuser']
        self.client.force_login(self.user)

    def upload(self, filename='answer.py', content=b'print(1)'):
        upload_sources(self.user, self.problem, [SimpleUploadedFile(filename, content)])
        return self.problem.invocation_sources.latest('created_at')

    def configured_source(self, **kwargs):
        source = self.upload(**kwargs)
        return update_source(self.user, self.problem, source.pk, self.language.pk, 'AC', {'1': 'AC', '2': 'AC'})

    def run_sources(self, sources=None, request_key=None):
        sources = sources or [self.configured_source()]
        with patch('judge.utils.invocations.enqueue_invocation') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                invocation = create_invocation(self.user, self.problem, [source.pk for source in sources],
                                               request_key or uuid.uuid4())
        self.assertEqual(enqueue.call_count, 1)
        return invocation

    def finish(self, invocation, statuses=('AC', 'AC', 'AC', 'AC'), ratios=(1, 1, 1, 1)):
        sub = invocation.items.first().submission
        sub.status, sub.result = 'D', 'AC' if set(statuses) == {'AC'} else 'WA'
        sub.batch_scoring = scoring_snapshot(self.problem)
        for index, (status, ratio) in enumerate(zip(statuses, ratios), 1):
            maximum = 20 if index <= 2 else 30
            SubmissionTestCase.objects.create(
                submission=sub, case=index, batch=1 if index <= 2 else 2,
                status=status, points=maximum * ratio, total=maximum, time=index * .01, memory=1024)
        sub.case_points, sub.case_total = finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        sub.save()
        return sub

    def test_upload_uses_private_files_no_submission(self):
        source = self.upload(content=b'not valid Python')
        self.assertEqual(read_source(source.path), 'not valid Python')
        self.assertTrue(source.path.startswith('%s/%s/' % (self.problem.pk, source.pk)))
        self.assertFalse(Submission.objects.exists())
        self.assertEqual(source.language.key, 'PYPY3')

    def test_upload_many_is_atomic_on_invalid_file(self):
        with self.assertRaises(ValidationError):
            upload_sources(self.user, self.problem, [
                SimpleUploadedFile('ok.py', b'print(1)'), SimpleUploadedFile('bad.py', b'\0')])
        self.assertFalse(self.problem.invocation_sources.exists())
        self.assertFalse([path for path in Path(self.temporary.name).rglob('*') if path.is_file()])

    def test_upload_existing_name_rejects_whole_batch_and_keeps_original(self):
        source = self.configured_source()
        with self.assertRaisesMessage(ValidationError, '"answer.py" already exists'):
            upload_sources(self.user, self.problem, [
                SimpleUploadedFile('new.py', b'print(2)'), SimpleUploadedFile('answer.py', b'print(3)')])
        source.refresh_from_db()
        self.assertEqual(self.problem.invocation_sources.count(), 1)
        self.assertEqual(read_source(source.path), 'print(1)')
        self.assertEqual(source.expected_overall, 'AC')
        self.assertEqual(len([path for path in Path(self.temporary.name).rglob('*') if path.is_file()]), 1)

    def test_upload_repeated_names_in_one_batch_is_atomic(self):
        for repeated_name in ('answer.py', r'folder\answer.py'):
            with self.subTest(name=repeated_name):
                with self.assertRaisesMessage(ValidationError, '"answer.py" already exists'):
                    upload_sources(self.user, self.problem, [
                        SimpleUploadedFile('answer.py', b'print(1)'),
                        SimpleUploadedFile(repeated_name, b'print(2)')])
                self.assertFalse(self.problem.invocation_sources.exists())
                self.assertFalse([path for path in Path(self.temporary.name).rglob('*') if path.is_file()])

    def test_source_names_are_unique_per_problem_not_globally(self):
        source = self.upload()
        other = create_problem(code='othermatrixqa')
        upload_sources(self.user, other, [SimpleUploadedFile('answer.py', b'print(2)')])
        self.assertEqual(read_source(source.path), 'print(1)')
        self.assertEqual(read_source(other.invocation_sources.get().path), 'print(2)')
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProblemInvocationSource.objects.create(problem=self.problem, filename='answer.py', path='unused')

    def test_http_duplicate_upload_returns_filename_error(self):
        source = self.upload()
        response = self.client.post(reverse('problem_invocations', args=[self.problem.code]), {
            'action': 'upload', 'sources': SimpleUploadedFile('answer.py', b'print(2)'),
        })
        self.assertContains(response, 'already exists in this problem.', status_code=400)
        self.assertContains(response, 'answer.py', status_code=400)
        self.assertEqual(self.problem.invocation_sources.count(), 1)
        self.assertEqual(read_source(source.path), 'print(1)')

    def test_default_languages_and_unknown_extension(self):
        cpp, _ = Language.objects.get_or_create(key='CPP20', defaults={'name': 'C++20', 'ace': 'c_cpp'})
        self.problem.allowed_languages.add(cpp)
        self.assertEqual(self.upload(filename='answer.cpp').language, cpp)
        self.assertEqual(self.upload(filename='answer.pas').language.key, 'PAS')
        self.assertIsNone(self.upload(filename='answer.unknown').language)

    def test_n_sources_create_n_normal_submissions_and_frozen_expectations(self):
        first, second = self.configured_source(), self.configured_source(filename='second.py')
        invocation = self.run_sources([first, second])
        self.assertEqual(invocation.items.count(), 2)
        for item in invocation.items.select_related('submission__source'):
            self.assertEqual(item.submission.user_id, self.user.profile.pk)
            self.assertIsNone(item.submission.contest_object_id)
            self.assertFalse(item.submission.offline_hidden)
            self.assertTrue(item.submission.source.source.startswith('# Invocations by user superuser at '))
            self.assertIn('+07:00\nprint(1)', item.submission.source.source)
        update_source(self.user, self.problem, first.pk, self.language.pk, 'RJ', {},
                      SimpleUploadedFile('new.py', b'broken code'))
        item = invocation.items.get(source=first)
        self.assertEqual(item.expected_overall, 'AC')
        self.assertEqual(item.submission.source.source.splitlines()[-1], 'print(1)')

    def test_duplicate_request_does_not_create_or_dispatch_again(self):
        source, key = self.configured_source(), uuid.uuid4()
        invocation = self.run_sources([source], key)
        with self.captureOnCommitCallbacks(execute=True), \
                patch('judge.utils.invocations.enqueue_invocation') as enqueue:
            duplicate = create_invocation(self.user, self.problem, [source.pk], key)
        self.assertEqual(invocation.pk, duplicate.pk)
        self.assertEqual(Submission.objects.count(), 1)
        enqueue.assert_not_called()

    def test_configuration_required_and_stale_groups_rejected(self):
        source = self.upload()
        with self.assertRaises(ValidationError):
            create_invocation(self.user, self.problem, [source.pk], uuid.uuid4())
        source = update_source(self.user, self.problem, source.pk, self.language.pk, 'AC', {'1': 'AC'})
        source.expected_layout = []
        source.save()
        with self.assertRaises(ValidationError):
            create_invocation(self.user, self.problem, [source.pk], uuid.uuid4())

    def test_header_uses_selected_runtime_and_respects_size(self):
        from django.utils import timezone
        for ace, prefix in (('c_cpp', '// '), ('python', '# '), ('pascal', '{ ')):
            result = submission_source('code', SimpleNamespace(ace=ace), 'alice', timezone.now())
            self.assertTrue(result.startswith(prefix + 'Invocations by user alice at '))
        with self.assertRaises(ValidationError):
            submission_source('x' * 65536, self.language, 'alice', timezone.now())

    def test_header_preserves_shebang_and_php_opening_tag(self):
        from django.utils import timezone
        now = timezone.now()
        for ace in ('javascript', 'ruby', 'sh'):
            source = '#!/usr/bin/env runtime\ncode\n'
            result = submission_source(source, SimpleNamespace(ace=ace), 'alice', now)
            self.assertTrue(result.startswith('#!/usr/bin/env runtime\n'))
            self.assertIn('Invocations by user alice', result.splitlines()[1])
            self.assertTrue(result.endswith('code\n'))
        for source in ('<?php echo 42;', '<?php\ndeclare(strict_types=1); echo 42;'):
            result = submission_source(source, SimpleNamespace(ace='php'), 'alice', now)
            self.assertTrue(result.startswith('<?php\n// Invocations by user alice'))
            self.assertTrue(result.endswith(source[5:]))
        for source in ('<?= 42 ?>', "<?= '<?php text' ?>"):
            result = submission_source(source, SimpleNamespace(ace='php'), 'alice', now)
            self.assertTrue(result.startswith('<?php // Invocations by user alice'))
            self.assertTrue(result.endswith('\n?>' + source))

    def test_permissions_are_hidden_in_html_and_direct_endpoints(self):
        source = self.configured_source()
        invocation = self.run_sources([source])
        editor = self.users['staff_problem_edit_all']
        self.assertFalse(can_invoke(editor, self.problem))
        self.client.force_login(editor)
        response = self.client.get(reverse('problem_data', args=[self.problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'invocation', html=False)
        for name, args in (
            ('problem_invocations', [self.problem.code]),
            ('problem_invocation_source', [self.problem.code, source.pk]),
            ('problem_invocation', [self.problem.code, invocation.pk]),
            ('problem_invocation_status', [self.problem.code, invocation.pk]),
        ):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)
            if name != 'problem_invocation_status':
                self.assertEqual(self.client.post(reverse(name, args=args), {}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('problem_invocations', args=[self.problem.code])).status_code, 404)

    def test_spam_permission_alone_is_insufficient(self):
        user = self.users['normal']
        user.user_permissions.add(Permission.objects.get(codename='spam_submission'))
        self.assertFalse(can_invoke(user, self.problem))

    def test_root_only(self):
        mirror = create_problem(code='invocationmirror', mirror_of=self.problem, mirror_root=self.problem)
        self.assertFalse(can_invoke(self.user, mirror))
        from judge.models import ExternalProblem
        ExternalProblem.objects.create(problem=self.problem, is_active=True)
        self.assertFalse(can_invoke(self.user, Problem.objects.get(pk=self.problem.pk)))

    def test_library_and_report_render_and_poll(self):
        invocation = self.run_sources()
        for name, args in (('problem_invocations', [self.problem.code]),
                           ('problem_invocation', [self.problem.code, invocation.pk]),
                           ('problem_invocation_status', [self.problem.code, invocation.pk])):
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertFalse(response.json()['finished'])

    def test_sum_has_individual_tests_min_has_single_max_time(self):
        invocation = self.run_sources()
        self.finish(invocation, ratios=(.5, 1, 1, 1))
        report = build_report(invocation)
        self.assertEqual(len(report['rows']), 3)
        self.assertEqual([row['batch'] for row in report['rows']], [1, 1, 2])
        self.assertEqual(report['rows'][2]['cells'][0]['time_ms'], 40)
        self.assertEqual(report['overall'][0]['actual'], 'PAC')
        self.assertEqual(report['groups'][0]['cells'][0]['actual'], 'PAC')
        self.assertEqual(report['groups'][1]['cells'][0]['actual'], 'AC')
        self.assertTrue(report['finished'])

    def test_ce_is_rejected_code_and_matches_rj(self):
        source = self.configured_source()
        source = update_source(self.user, self.problem, source.pk, self.language.pk, 'RJ', {'1': 'RJ'})
        invocation = self.run_sources([source])
        Submission.objects.filter(invocation_item__invocation=invocation).update(
            status='CE', result='CE', error='Compile error')
        report = build_report(invocation)
        self.assertEqual(report['overall'][0]['match'], 'match')
        self.assertEqual(report['groups'][0]['cells'][0]['match'], 'match')
        self.assertIsNone(report['rows'][2]['cells'][0]['time_ms'])

    def test_min_skip_does_not_hide_established_failure(self):
        invocation = self.run_sources()
        sub = self.finish(invocation, statuses=('AC', 'AC', 'TLE', 'SC'), ratios=(1, 1, 0, 0))
        SubmissionTestCase.objects.filter(submission=sub, case=4).update(time=None)
        report = build_report(invocation)
        self.assertEqual(report['rows'][2]['cells'][0]['actual'], 'TLE')
        self.assertEqual(report['rows'][2]['cells'][0]['time_ms'], 30)

    def test_changed_structure_is_not_compared_by_case_number(self):
        invocation = self.run_sources()
        sub = self.finish(invocation)
        sub.batch_scoring['full'][0]['count'] = 99
        sub.save()
        report = build_report(invocation)
        self.assertTrue(report['columns'][0]['changed'])
        self.assertEqual(report['rows'][0]['cells'][0]['actual'], 'CHANGED')

    def test_source_download_and_delete_preserve_submission(self):
        invocation = self.run_sources()
        source = invocation.items.first().source
        url = reverse('problem_invocation_source', args=[self.problem.code, source.pk])
        response = self.client.get(url)
        self.assertEqual(b''.join(response.streaming_content), b'print(1)')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('problem_invocations', args=[self.problem.code]),
                                        {'action': 'delete_sources', 'source': str(source.pk)})
            self.assertEqual(response.status_code, 302)
        self.assertFalse(source_storage().exists(source.path))
        self.assertEqual(Submission.objects.count(), 1)
        self.assertIsNone(invocation.items.first().source_id)

    def test_priority_full_tests_survive_rejudge(self):
        from judge.judgeapi import judge_submission
        invocation = self.run_sources()
        sub = invocation.items.first().submission
        for rejudge, batch_rejudge in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(rejudge=rejudge, batch_rejudge=batch_rejudge):
                Submission.objects.filter(pk=sub.pk).update(
                    status='D' if rejudge or batch_rejudge else 'QU', batch_scoring=None)
                with patch('judge.judgeapi.judge_request', return_value={
                        'name': 'submission-received', 'submission-id': sub.pk}) as judge_request:
                    with self.captureOnCommitCallbacks(execute=True):
                        judge_submission(sub, rejudge=rejudge, batch_rejudge=batch_rejudge)
                self.assertEqual(judge_request.call_args[0][0]['priority'], 1)
                sub.refresh_from_db()
                self.assertFalse(sub.is_pretested)
                self.assertEqual(sub.rejudged_date is not None, rejudge or batch_rejudge)

    def test_regular_submission_priorities_are_preserved(self):
        from judge.judgeapi import judge_submission
        from judge.models import ContestSubmission, SubmissionSource
        from judge.models.tests.util import create_contest, create_contest_participation, create_contest_problem

        contest = create_contest(key='prioritycontest', run_pretests_only=True)
        participation = create_contest_participation(contest=contest, user=self.user.profile)
        contest_problem = create_contest_problem(contest=contest, problem=self.problem, is_pretested=True)
        for in_contest in (False, True):
            for rejudge, batch_rejudge, expected in ((False, False, 0 if in_contest else 1),
                                                     (True, False, 2), (False, True, 3)):
                with self.subTest(in_contest=in_contest, rejudge=rejudge, batch_rejudge=batch_rejudge):
                    sub = Submission.objects.create(problem=self.problem, user=self.user.profile,
                                                    language=self.language, status='D')
                    SubmissionSource.objects.create(submission=sub, source='print(1)')
                    if in_contest:
                        ContestSubmission.objects.create(submission=sub, problem=contest_problem,
                                                         participation=participation)
                    with patch('judge.judgeapi.judge_request', return_value={
                            'name': 'submission-received', 'submission-id': sub.pk}) as send:
                        with self.captureOnCommitCallbacks(execute=True):
                            judge_submission(sub, rejudge=rejudge, batch_rejudge=batch_rejudge)
                    self.assertEqual(send.call_args[0][0]['priority'], expected)
                    sub.refresh_from_db()
                    self.assertEqual(sub.is_pretested, in_contest)

    def test_dispatch_task_redelivery_does_not_recreate_submissions(self):
        from judge.tasks.invocations import dispatch_invocation
        invocation = self.run_sources()
        with patch.object(Submission, 'judge') as judge:
            dispatch_invocation(str(invocation.pk))
            dispatch_invocation(str(invocation.pk))
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(Submission.objects.count(), 1)

    @patch('judge.bridge.judge_handler._ensure_connection')
    @patch('judge.bridge.judge_handler.threading.Timer')
    def test_bridge_sends_invocation_policy_only_for_invocation(self, timer, ensure_connection):
        from unittest.mock import Mock
        from judge.bridge.judge_handler import JudgeHandler
        from judge.bridge.judge_list import JudgeList
        invocation = self.run_sources()
        sub = invocation.items.first().submission
        Submission.objects.filter(pk=sub.pk).update(batch_scoring={'dispatch_id': 'invocation-dispatch'})
        handler = object.__new__(JudgeHandler)
        handler.name = 'invocation-test-judge'
        handler.send, handler.judges = Mock(), JudgeList()
        handler.submit(sub.pk, self.problem.code, self.language.key, 'print(1)')
        packet = handler.send.call_args[0][0]
        self.assertIs(packet['meta']['invocation'], True)
        self.assertIs(packet['short-circuit'], False)
        self.assertIs(packet['meta']['pretests-only'], False)

    def test_run_does_not_change_current_contest(self):
        from judge.models.tests.util import create_contest, create_contest_participation
        contest = create_contest(key='invocationcontest')
        participation = create_contest_participation(contest=contest, user=self.user.profile)
        self.user.profile.current_contest = participation
        self.user.profile.save(update_fields=['current_contest'])
        invocation = self.run_sources()
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.current_contest_id, participation.pk)
        self.assertIsNone(invocation.items.first().submission.contest_object_id)

    def test_locked_cumulative_data_still_has_invocations_tab(self):
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='ON')
        response = self.client.get(reverse('problem_data', args=[self.problem.code]))
        self.assertContains(response, reverse('problem_invocations', args=[self.problem.code]))

    def test_unlocked_tests_cannot_create_invocation(self):
        source = self.configured_source()
        Problem.objects.filter(pk=self.problem.pk).update(is_locked=False)
        with self.assertRaises(ValidationError):
            create_invocation(self.user, self.problem, [source.pk], uuid.uuid4())
        self.assertFalse(Submission.objects.exists())

    def test_http_upload_configure_and_run(self):
        library = reverse('problem_invocations', args=[self.problem.code])
        response = self.client.post(library, {'action': 'upload', 'sources': SimpleUploadedFile('web.py', b'print(1)')})
        self.assertEqual(response.status_code, 302)
        source = self.problem.invocation_sources.get()
        response = self.client.post(library, {
            'action': 'save_settings', 'settings_source': str(source.pk),
            'language-%s' % source.pk: str(self.language.pk), 'overall-%s' % source.pk: 'AC/TL',
            'subtask-%s-1' % source.pk: 'PAC'})
        self.assertEqual(response.status_code, 302)
        with self.captureOnCommitCallbacks(execute=True), patch('judge.utils.invocations.enqueue_invocation'):
            response = self.client.post(library, {
                'action': 'run', 'source': str(source.pk), 'request_key': str(uuid.uuid4())})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Submission.objects.count(), 1)

    def test_failed_replacement_keeps_original_file(self):
        source = self.configured_source()
        with patch.object(ProblemInvocationSource, 'save', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):
                update_source(self.user, self.problem, source.pk, self.language.pk, 'AC', {},
                              SimpleUploadedFile('replacement.py', b'new source'))
        source.refresh_from_db()
        self.assertEqual(read_source(source.path), 'print(1)')
        self.assertEqual(len([path for path in Path(self.temporary.name).rglob('*') if path.is_file()]), 1)

    def test_retry_reserved_dispatch_reuses_token_without_new_submission(self):
        from judge.tasks.invocations import dispatch_invocation
        invocation = self.run_sources()
        sub = invocation.items.first().submission
        Submission.objects.filter(pk=sub.pk).update(batch_scoring={'dispatch_id': 'reserved-token'})
        with patch('judge.judgeapi._send_submission') as send:
            with self.captureOnCommitCallbacks(execute=True):
                dispatch_invocation(str(invocation.pk))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args[0][-1], 'reserved-token')
        self.assertEqual(send.call_args[0][-2], 1)
        self.assertEqual(Submission.objects.count(), 1)

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    def test_restore_retry_still_uses_full_tests_and_priority_one(self):
        from judge.tasks.storage import storage_retry_judge_submission
        invocation = self.run_sources()
        sub = invocation.items.first().submission
        with patch('judge.models.submission._problem_data_local_usable', return_value=True), \
                patch('judge.judgeapi.judge_request', return_value={
                    'name': 'submission-received', 'submission-id': sub.pk}) as send:
            with self.captureOnCommitCallbacks(execute=True):
                storage_retry_judge_submission(sub.pk)
        self.assertEqual(send.call_args[0][0]['priority'], 1)
        sub.refresh_from_db()
        self.assertFalse(sub.is_pretested)
        self.assertIsNone(sub.rejudged_date)

    def test_save_all_updates_parameters_and_keeps_files_and_history(self):
        first, second = self.configured_source(), self.configured_source(filename='other.py')
        invocation = self.run_sources([first, second])
        paths = [first.path, second.path]
        save_source_settings(self.user, self.problem, [
            {'source': str(first.pk), 'language': str(self.language.pk), 'overall': 'PAC', 'subtasks': {'1': 'PAC'}},
            {'source': str(second.pk), 'language': '', 'overall': 'RJ', 'subtasks': {'2': 'SKIP'}},
        ])
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.expected_overall, first.expected_subtasks), ('PAC', {'1': 'PAC'}))
        self.assertEqual((second.expected_overall, second.expected_subtasks), ('RJ', {'2': 'SKIP'}))
        self.assertIsNone(second.language_id)
        self.assertEqual([first.path, second.path], paths)
        self.assertEqual([read_source(path) for path in paths], ['print(1)', 'print(1)'])
        self.assertEqual(list(invocation.items.values_list('expected_overall', flat=True)), ['AC', 'AC'])

    def test_save_all_rolls_back_every_row_if_one_is_invalid(self):
        first, second = self.configured_source(), self.configured_source(filename='other.py')
        with self.assertRaises(ValidationError):
            save_source_settings(self.user, self.problem, [
                {'source': str(first.pk), 'language': str(self.language.pk), 'overall': 'PAC', 'subtasks': {}},
                {'source': str(second.pk), 'language': str(self.language.pk), 'overall': 'AC',
                 'subtasks': {'99': 'AC'}},
            ])
        first.refresh_from_db()
        self.assertEqual(first.expected_overall, 'AC')
        self.assertEqual(first.expected_subtasks, {'1': 'AC', '2': 'AC'})

    def test_save_all_rejects_sources_from_another_problem(self):
        source = self.configured_source()
        other = create_problem(code='othersources')
        ProblemInvocationSource.objects.filter(pk=source.pk).update(problem=other)
        with self.assertRaises(ValidationError):
            save_source_settings(self.user, self.problem, [{'source': str(source.pk), 'overall': 'RJ'}])
        source.refresh_from_db()
        self.assertEqual(source.expected_overall, 'AC')

    def test_save_all_http_and_failed_values_are_preserved(self):
        source = self.configured_source()
        url = reverse('problem_invocations', args=[self.problem.code])
        data = {'action': 'save_settings', 'settings_source': str(source.pk),
                'language-%s' % source.pk: str(self.language.pk), 'overall-%s' % source.pk: 'PAC',
                'subtask-%s-1' % source.pk: 'PAC'}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        source.refresh_from_db()
        self.assertEqual((source.expected_overall, source.expected_subtasks), ('PAC', {'1': 'PAC'}))
        data['language-%s' % source.pk] = '-1'
        data['overall-%s' % source.pk] = 'TLE'
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'value="TLE" selected', status_code=400)
        source.refresh_from_db()
        self.assertEqual(source.expected_overall, 'PAC')

    def test_delete_selected_sources_removes_only_selected_rows_and_files(self):
        first, second = self.configured_source(), self.configured_source(filename='second.py')
        keep = self.configured_source(filename='keep.py')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('problem_invocations', args=[self.problem.code]), {
                'action': 'delete_sources', 'source': [str(first.pk), str(second.pk)],
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.problem.invocation_sources.values_list('pk', flat=True)), [keep.pk])
        self.assertFalse(source_storage().exists(first.path))
        self.assertFalse(source_storage().exists(second.path))
        self.assertTrue(source_storage().exists(keep.path))

    def test_delete_batch_is_atomic_for_foreign_missing_or_invalid_sources(self):
        source, foreign = self.configured_source(), self.configured_source(filename='foreign.py')
        other = create_problem(code='foreignsources')
        ProblemInvocationSource.objects.filter(pk=foreign.pk).update(problem=other)
        for ids in ([source.pk, foreign.pk], [source.pk, uuid.uuid4()], [], ['invalid']):
            with self.subTest(ids=ids), self.assertRaises(ValidationError):
                delete_sources(self.user, self.problem, ids)
            self.assertTrue(ProblemInvocationSource.objects.filter(pk=source.pk).exists())
            self.assertTrue(source_storage().exists(source.path))
            self.assertTrue(source_storage().exists(foreign.path))

    def test_source_download_endpoint_no_longer_mutates_files(self):
        source = self.configured_source()
        response = self.client.post(reverse('problem_invocation_source', args=[self.problem.code, source.pk]), {
            'action': 'replace', 'replacement': SimpleUploadedFile('new.py', b'print(2)'),
        })
        self.assertEqual(response.status_code, 405)
        self.assertEqual(read_source(source.path), 'print(1)')
