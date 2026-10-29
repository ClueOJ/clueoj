import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html import unescape
from html.parser import HTMLParser
from threading import Event
from unittest import skipUnless
from unittest.mock import patch
from zipfile import ZipFile

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.db import close_old_connections, connection, transaction
from django.test import Client, RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from judge.bridge.judge_handler import JudgeHandler
from judge.judgeapi import judge_submission
from judge.models import (Language, LanguageLimit, Problem, ProblemData, ProblemTestCase, Profile,
                          Submission, SubmissionSource, SubmissionTestCase)
from judge.models.tests.util import CommonDataMixin, create_contest, create_problem
from judge.performance_points import get_pp_breakdown
from judge.tasks.subtask_scoring import (_dispatch, _rebuild_step, rebuild_subtask_scores,
                                        refresh_contest_subtask_scores, set_subtask_scoring)
from judge.utils.batch_scoring import finalize_batch_results, save_batch_policies, scoring_snapshot
from judge.utils.problem_data import ProblemDataCompiler
from judge.utils.subtask_scoring import (cumulative_breakdown, schedule_contest_score_refresh,
                                        user_problem_scores, validate_configuration)
from judge.views.subtask_scoring import configure_subtask_scoring


class CumulativeFixtureMixin(CommonDataMixin):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.problem = create_problem(code='cumulative_fixture', is_public=True, partial=True, points=100)
        cls.profile = cls.users['normal'].profile
        cls.language = Language.get_python3()
        cls.data = ProblemData.objects.create(problem=cls.problem)
        content = io.BytesIO()
        with ZipFile(content, 'w') as archive:
            for i in range(4):
                archive.writestr('%s.in' % i, str(i))
                archive.writestr('%s.out' % i, '1')
        cls.data.zipfile.save('cumulative-tests.zip', ContentFile(content.getvalue()))
        for index, maximum in enumerate((30, 70)):
            ProblemTestCase.objects.create(dataset=cls.problem, type='S', order=index * 4,
                                          points=maximum, batch_type='min', is_pretest=False)
            for child in range(2):
                i = index * 2 + child
                ProblemTestCase.objects.create(dataset=cls.problem, type='C', order=index * 4 + child + 1,
                                              input_file='%s.in' % i, output_file='%s.out' % i, is_pretest=False)
            ProblemTestCase.objects.create(dataset=cls.problem, type='E', order=index * 4 + 3, is_pretest=False)
        ProblemDataCompiler.generate(cls.problem, cls.data, cls.problem.cases.order_by('order'),
                                     ['%s.%s' % (i, ext) for i in range(4) for ext in ('in', 'out')])

    def setUp(self):
        self.problem.refresh_from_db()

    def activate(self, state='ON', revision=1):
        config = scoring_snapshot(self.problem)
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state=state,
            subtask_score_revision=revision, subtask_score_config=config)
        self.problem.refresh_from_db()

    def submission(self, ratios=(1, 0), *, revision=1, hidden=False, pretested=False, statuses=None,
                   problem=None, user=None):
        problem = problem or self.problem
        snapshot = scoring_snapshot(problem)
        snapshot['cumulative_revision'] = revision
        sub = Submission.objects.create(problem=problem, user=user or self.profile, language=self.language,
            status='D', result='WA', offline_hidden=hidden, batch_scoring=snapshot, is_pretested=pretested)
        SubmissionSource.objects.create(submission=sub, source='print(1)')
        maxima = [item['total'] for item in snapshot['full']]
        for batch, (maximum, ratio) in enumerate(zip(maxima, ratios), 1):
            for child in range(2):
                SubmissionTestCase.objects.create(submission=sub, case=(batch - 1) * 2 + child + 1,
                    batch=batch, points=maximum / 2 * ratio, total=maximum / 2,
                    status=(statuses or {}).get((batch, child), 'AC' if ratio == 1 else 'WA'))
        sub.case_points, sub.case_total = finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        sub.points = round(sub.case_points / sub.case_total * problem.points, 3)
        sub.save()
        return sub


class CumulativeScoringTests(CumulativeFixtureMixin, TestCase):
    def test_max_per_subtask_changes_profile_and_pp_without_fake_ac(self):
        first = self.submission((1, 0))
        second = self.submission((0, 1))
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 70)
        self.activate()
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 100)
        self.assertEqual(self.profile.problem_count, 0)
        breakdown = cumulative_breakdown(self.profile.pk, self.problem)
        self.assertEqual([row['submission_id'] for row in breakdown['subtasks']], [first.pk, second.pk])
        rows, _ = get_pp_breakdown(self.profile)
        self.assertEqual(rows[0].points, 100)
        self.assertIsNone(rows[0].sub_id)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((first.points, second.points), (30, 70))

    def test_off_keeps_existing_max_submission_rule(self):
        self.submission((1, 0))
        self.submission((0, 1))
        self.assertEqual(user_problem_scores(self.profile.pk)[self.problem.pk], 70)
        rows, _ = get_pp_breakdown(self.profile)
        self.assertIsNotNone(rows[0].sub_id)
        self.assertEqual(rows[0].points, 70)

    def test_duplicate_and_lower_scores_do_not_stack(self):
        self.submission((0.5, 0))
        self.submission((0.5, 0))
        self.submission((0.2, 1))
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 85)

    def test_min_does_not_merge_complementary_testcases(self):
        first = self.submission((1, 0))
        second = self.submission((1, 0))
        first.test_cases.filter(case=1).update(points=0)
        second.test_cases.filter(case=2).update(points=0)
        for sub in (first, second):
            finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 0)

    def test_sum_takes_best_batch_total_not_best_individual_cases(self):
        self.problem.cases.filter(type='S', order=0).update(batch_type='sum')
        first = self.submission((1, 0))
        second = self.submission((1, 0))
        first.test_cases.filter(case=1).update(points=0)
        second.test_cases.filter(case=2).update(points=0)
        for sub in (first, second):
            finalize_batch_results(sub, list(sub.test_cases.order_by('case')))
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 15)

    def test_wrong_revision_and_hidden_or_incomplete_results_are_excluded(self):
        self.submission((1, 1), revision=0)
        self.submission((1, 1), hidden=True)
        incomplete = self.submission((1, 1))
        incomplete.batch_results.filter(batch=2).delete()
        valid = self.submission((0.5, 0.5))
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 50)
        self.assertTrue(all(row['submission_id'] == valid.pk for row in
                            cumulative_breakdown(self.profile.pk, self.problem)['subtasks']))

    def test_true_pretests_excluded_but_full_fallback_is_accepted(self):
        fallback = self.submission((1, 0), pretested=True)
        actual_pretests = self.submission((0, 1), pretested=True)
        config = actual_pretests.batch_scoring
        config['pretests'] = config['full']
        actual_pretests.batch_scoring = config
        actual_pretests.save()
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 30)

    def test_nonfinite_or_out_of_range_batch_is_not_credited(self):
        invalid = self.submission((1, 1))
        invalid.batch_results.filter(batch=1).update(points=31)
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 0)

    def test_scale_and_fractional_scores(self):
        self.problem.points = 200
        self.problem.save()
        self.submission((1 / 3, 0.5))
        self.activate()
        score = cumulative_breakdown(self.profile.pk, self.problem)
        self.assertEqual(score['points'], 90)
        self.assertEqual((score['case_points'], score['case_total']), (45, 100))

    def test_display_uses_test_scale_while_profile_and_pp_keep_problem_scale(self):
        self.problem.points = 40
        self.problem.save()
        self.problem.cases.filter(type='S', order=0).update(points=60)
        self.problem.cases.filter(type='S', order=4).update(points=140)
        self.submission((1, 0))
        self.submission((0, 0.5))
        self.activate()
        score = cumulative_breakdown(self.profile.pk, self.problem)
        self.assertEqual((score['case_points'], score['case_total']), (130, 200))
        self.assertEqual((score['points'], score['total']), (26, 40))
        self.assertEqual([(row['points'], row['total']) for row in score['subtasks']], [(60, 60), (70, 140)])
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 26)
        self.assertEqual(self.profile.performance_points, 26)
        rows, _ = get_pp_breakdown(self.profile)
        self.assertEqual((rows[0].sub_points, rows[0].sub_total), (130, 200))
        self.assertEqual(rows[0].points, 26)
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, '130 / 200')
        self.assertContains(response, '60 / 60')
        self.assertContains(response, '70 / 140')

    def test_no_submissions_still_displays_full_test_scale(self):
        self.problem.points = 40
        self.problem.save()
        self.problem.cases.filter(type='S', order=0).update(points=60)
        self.problem.cases.filter(type='S', order=4).update(points=140)
        self.activate()
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, '0 / 200')

    def test_pp_reorders_other_problem_weights(self):
        self.submission((1, 0))
        self.submission((0, 1))
        other = create_problem(code='cumulative_other', is_public=True, partial=True)
        Submission.objects.create(problem=other, user=self.profile, language=self.language,
                                  status='D', result='WA', points=80, case_points=80, case_total=100)
        self.activate()
        self.profile.calculate_points(table=[1, 0.5])
        self.assertEqual(self.profile.points, 180)
        self.assertEqual(self.profile.performance_points, 140)

    def test_other_users_and_problems_do_not_contribute(self):
        self.submission((1, 1), user=self.users['superuser'].profile)
        self.submission((1, 0))
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 30)

    def test_configuration_validates_actual_compiled_archive(self):
        config = validate_configuration(self.problem)
        self.assertEqual([row['total'] for row in config['full']], [30, 70])

    def test_outdated_init_is_rejected_even_with_same_count_and_points(self):
        self.problem.cases.filter(type='C', input_file='0.in').update(input_file='1.in')
        with self.assertRaisesMessage(ValidationError, 'compiled test configuration is outdated'):
            validate_configuration(self.problem)

    def test_zero_point_subtask_is_valid_but_all_zero_subtasks_are_not(self):
        self.problem.cases.filter(type='S', order=4).update(points=0)
        validate_configuration(self.problem, check_files=False)
        self.submission((1, 1))
        self.activate()
        self.assertEqual(cumulative_breakdown(self.profile.pk, self.problem)['points'], 100)
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='OFF')
        self.problem.cases.filter(type='S').update(points=0)
        with self.assertRaises(ValidationError):
            validate_configuration(Problem.objects.get(pk=self.problem.pk), check_files=False)

    def test_any_standalone_case_including_zero_point_pretests_is_rejected(self):
        for points in (0, 10):
            for pretest in (False, True):
                with self.subTest(points=points, pretest=pretest):
                    case = ProblemTestCase.objects.create(dataset=self.problem, type='C', order=20,
                        input_file='0.in', output_file='0.out', points=points, is_pretest=pretest)
                    with self.assertRaisesMessage(ValidationError, 'Every test'):
                        validate_configuration(self.problem, check_files=False)
                    case.delete()

    def test_no_subtasks_and_empty_subtask_are_rejected(self):
        self.problem.cases.all().delete()
        with self.assertRaises(ValidationError):
            validate_configuration(self.problem, check_files=False)
        ProblemTestCase.objects.create(dataset=self.problem, type='S', order=0, points=100, is_pretest=False)
        with self.assertRaises(ValidationError):
            validate_configuration(self.problem, check_files=False)

    def test_organization_private_and_unsupported_scoring_cannot_enable(self):
        for field, value in (('is_organization_private', True), ('partial', False), ('is_manually_managed', True)):
            with self.subTest(field=field):
                original = getattr(self.problem, field)
                setattr(self.problem, field, value)
                with self.assertRaises(ValidationError):
                    validate_configuration(self.problem, check_files=False)
                setattr(self.problem, field, original)

    def test_nonpublic_nonorganization_problem_can_enable_and_show_controls(self):
        self.problem.is_public = False
        self.problem.save()
        self.client.force_login(self.users['superuser'])
        response = self.client.get(reverse('problem_edit', args=[self.problem.code]))
        self.assertContains(response, 'Enable and rejudge all submissions')
        response = self.client.post(reverse('problem_subtask_scoring', args=[self.problem.code]), {'action': 'enable'})
        self.assertEqual(response.status_code, 302)
        self.problem.refresh_from_db()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'ON')
        self.problem.refresh_from_db()
        self.assertFalse(self.problem.is_public)

    def test_nonpublic_organization_private_problem_is_still_rejected(self):
        self.problem.is_public = False
        self.problem.is_organization_private = True
        self.problem.save()
        with self.assertRaisesMessage(ValidationError, 'organization-private'):
            set_subtask_scoring(self.problem.pk, self.users['superuser'], True)

    def test_private_cumulative_score_and_publication_keep_existing_ranking_scope(self):
        self.submission((1, 0))
        self.submission((0, 1))
        self.activate()
        self.problem.authors.add(self.profile)
        self.problem.is_public = False
        self.problem.save()
        selected = Problem.objects.filter(pk=self.problem.pk)
        self.assertEqual(user_problem_scores(self.profile.pk, selected)[self.problem.pk], 100)
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 0)
        self.assertEqual(get_pp_breakdown(self.profile)[0], [])
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, '100 / 100')
        self.assertNotContains(response, 'name="action" value="enable"')
        self.client.logout()
        self.assertEqual(self.client.get(reverse('problem_detail', args=[self.problem.code])).status_code, 404)
        selected.update(is_public=True)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'ON')
        self.assertEqual(self.problem.subtask_score_revision, 1)
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 100)
        self.assertEqual(get_pp_breakdown(self.profile)[0][0].points, 100)

    def test_admin_can_change_public_visibility_while_scoring_is_on(self):
        from django.contrib.admin import AdminSite
        from judge.admin.problem import ProblemAdmin

        self.activate()
        request = RequestFactory().post('/')
        request.user = self.users['superuser']
        admin = ProblemAdmin(Problem, AdminSite())
        self.assertNotIn('is_public', admin.get_readonly_fields(request, self.problem))
        selected = Problem.objects.filter(pk=self.problem.pk)
        with patch.object(admin, '_rescore') as rescore, patch.object(admin, 'message_user'):
            admin.make_private(request, selected)
            self.problem.refresh_from_db()
            self.assertFalse(self.problem.is_public)
            self.assertEqual(self.problem.subtask_score_state, 'ON')
            admin.make_public_and_update_publish_date(request, selected)
            self.problem.refresh_from_db()
            self.assertTrue(self.problem.is_public)
            self.assertEqual(self.problem.subtask_score_revision, 1)
            self.assertEqual(rescore.call_count, 2)

    def test_locked_history_is_rejected_without_forcing_rejudge(self):
        sub = self.submission()
        sub.locked_after = timezone.now() - timedelta(days=1)
        sub.save()
        with self.assertRaisesMessage(ValidationError, 'locked submissions'):
            validate_configuration(self.problem, check_files=False)

    def test_permission_is_superuser_not_staff_or_edit_problem(self):
        for name in ('normal', 'staff_problem_edit_all'):
            with self.subTest(user=name), self.assertRaises(PermissionDenied):
                set_subtask_scoring(self.problem.pk, self.users[name], True)

    def test_locks_case_model_bulk_writes_policies_data_and_limits(self):
        self.activate()
        case = self.problem.cases.filter(type='C').first()
        actions = [lambda: case.save(), lambda: case.delete(),
                   lambda: self.problem.cases.update(points=0), lambda: self.problem.cases.all().delete(),
                   lambda: self.data.save(),
                   lambda: save_batch_policies(self.problem, {'batch-mode-full:1': 'sum', 'batch-mode-full:2': 'sum'}),
                   lambda: ProblemTestCase.objects.bulk_create([
                       ProblemTestCase(dataset=self.problem, type='C', order=100, is_pretest=False)]),
                   lambda: LanguageLimit.objects.create(problem=self.problem, language=self.language,
                                                        time_limit=1, memory_limit=1000)]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ValidationError):
                with transaction.atomic():
                    action()

    def test_locked_problem_settings_but_statement_remains_editable(self):
        self.activate()
        for field, value in (('points', 200), ('is_organization_private', True), ('partial', False), ('time_limit', 9)):
            with self.subTest(field=field):
                setattr(self.problem, field, value)
                with self.assertRaises(ValidationError):
                    self.problem.save()
                self.problem.refresh_from_db()
        with self.assertRaises(ValidationError):
            Problem.objects.filter(pk=self.problem.pk).update(is_organization_private=True)
        self.problem.description = 'An edited statement'
        self.problem.save()
        self.assertEqual(Problem.objects.get(pk=self.problem.pk).description, 'An edited statement')

    def test_cannot_move_cases_into_or_out_of_a_locked_problem(self):
        other = create_problem(code='moving_tests')
        case = self.problem.cases.filter(type='C').first()
        other_case = ProblemTestCase.objects.create(dataset=other, type='C', order=0, is_pretest=False)
        self.activate()
        case.dataset = other
        other_case.dataset = self.problem
        actions = [case.save, other_case.save,
                   lambda: other.cases.update(dataset=self.problem),
                   lambda: self.problem.cases.update(dataset=other)]
        for action in actions:
            with self.subTest(action=action), self.assertRaises(ValidationError), transaction.atomic():
                action()

    def test_locked_problem_cannot_acquire_mirror_dependents(self):
        other = create_problem(code='mirror_into_locked')
        self.activate()
        other.mirror_of = self.problem
        with self.assertRaises(ValidationError):
            other.save()
        with self.assertRaises(ValidationError):
            Problem.objects.filter(pk=other.pk).update(mirror_of=self.problem)

    def test_contest_score_is_released_after_end_with_a_deduplicated_refresh(self):
        sub = self.submission((1, 0))
        sub.contest_object = create_contest(key='cumulative_contest')
        sub.save()
        self.activate()
        sub.problem = self.problem
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 0)
        with patch.object(refresh_contest_subtask_scores, 'apply_async') as enqueue:
            with self.captureOnCommitCallbacks(execute=True):
                schedule_contest_score_refresh(sub)
                schedule_contest_score_refresh(sub)
            self.assertEqual(enqueue.call_count, 1)
            refresh_contest_subtask_scores.run(sub.contest_object_id, self.problem.pk)
            self.assertEqual(enqueue.call_count, 2)
        type(sub.contest_object).objects.filter(pk=sub.contest_object_id).update(
            end_time=timezone.now() - timedelta(seconds=1))
        refresh_contest_subtask_scores.run(sub.contest_object_id, self.problem.pk)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.points, 30)

    def test_large_healthy_rebuild_does_not_timeout_from_total_elapsed_time(self):
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        progress = {**self.problem.subtask_score_progress,
                    'started_at': (timezone.now() - timedelta(hours=4)).isoformat()}
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress=progress)
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'ON')

    def test_stale_metadata_save_cannot_disable_scoring(self):
        stale = Problem.objects.get(pk=self.problem.pk)
        self.activate()
        stale.name = 'New title'
        stale.save()
        self.assertEqual(Problem.objects.get(pk=self.problem.pk).subtask_score_state, 'ON')

    def test_clone_starts_off_and_does_not_move_locked_source_language_limits(self):
        limit = LanguageLimit.objects.create(problem=self.problem, language=self.language,
                                             time_limit=2, memory_limit=1024)
        self.activate()
        self.client.force_login(self.users['superuser'])
        response = self.client.post(reverse('problem_clone', args=[self.problem.code]), {'code': 'cumulative_clone'})
        self.assertEqual(response.status_code, 302)
        clone = Problem.objects.get(code='cumulative_clone')
        self.assertEqual(clone.subtask_score_state, 'OFF')
        self.assertEqual(clone.subtask_score_revision, 0)
        limit.refresh_from_db()
        self.assertEqual(limit.problem_id, self.problem.pk)
        self.assertEqual(clone.language_limits.get().time_limit, 2)

    def test_admission_blocked_during_rebuild(self):
        self.activate(state='REBUILDING')
        with self.assertRaises(ValidationError):
            Submission.objects.create(problem=self.problem, user=self.profile, language=self.language)

    def test_enable_empty_history_and_disable_publish_correct_modes(self):
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'REBUILDING')
        revision = self.problem.subtask_score_revision
        self.assertEqual(_rebuild_step(self.problem.pk, revision), 'ON')
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'OFF')

    def test_disable_refreshes_profile_to_legacy_max(self):
        self.submission((1, 0))
        self.submission((0, 1))
        self.activate()
        self.profile.calculate_points()
        self.assertEqual(self.profile.points, 100)
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        _rebuild_step(self.problem.pk, self.problem.subtask_score_revision)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.points, 70)

    def test_obsolete_task_does_not_publish_on_after_disable(self):
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        old = self.problem.subtask_score_revision
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.assertEqual(_rebuild_step(self.problem.pk, old), 'obsolete')
        self.assertNotEqual(Problem.objects.get(pk=self.problem.pk).subtask_score_state, 'ON')

    def test_duplicate_dispatch_does_not_rejudge_twice(self):
        sub = self.submission()
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        revision = self.problem.subtask_score_revision
        self.assertEqual(_rebuild_step(self.problem.pk, revision), 'dispatched')
        with patch.object(Submission, 'judge') as judge:
            _dispatch(sub.pk, self.problem.pk, revision)
            _dispatch(sub.pk, self.problem.pk, revision)
        self.assertEqual(judge.call_count, 1)
        self.assertTrue(judge.call_args.kwargs['force_full'])

    def test_delayed_dispatch_cannot_claim_a_later_pending_submission(self):
        old = self.submission()
        current = self.submission()
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        progress = {**self.problem.subtask_score_progress, 'pending': current.pk, 'dispatched': False}
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress=progress)
        with patch.object(Submission, 'judge') as judge:
            _dispatch(old.pk, self.problem.pk, self.problem.subtask_score_revision)
        judge.assert_not_called()

    def defer_rebuild_for_restore(self):
        from judge.tasks.storage import storage_retry_judge_submission
        sub = self.submission()
        sub.is_pretested = True
        sub.save(update_fields=['is_pretested'])
        self.activate('REBUILDING')
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress={
            'phase': 'rejudge', 'pending': sub.pk, 'dispatched': False})
        with patch('judge.models.submission._problem_data_local_usable', return_value=False), \
             patch('judge.utils.storage_client.ensure_problem_ready', return_value={'state': 'restoring'}), \
             patch.object(storage_retry_judge_submission, 'apply_async') as retry, \
             patch('judge.tasks.subtask_scoring._enqueue'), self.captureOnCommitCallbacks(execute=True):
            _dispatch(sub.pk, self.problem.pk, 1)
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')
        self.assertEqual(sub.batch_scoring['cumulative_restore_revision'], 1)
        return sub, retry.call_args.kwargs

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    def test_restore_retry_keeps_full_tests_and_rebuild_revision(self):
        from judge.tasks.storage import storage_retry_judge_submission
        sub, retry = self.defer_rebuild_for_restore()
        self.assertEqual(retry['kwargs']['cumulative_revision'], 1)
        self.assertTrue(retry['kwargs']['force_full'])
        with patch('judge.models.submission._problem_data_local_usable', return_value=True), \
             patch('judge.judgeapi._send_submission') as send, self.captureOnCommitCallbacks(execute=True):
            storage_retry_judge_submission.run(*retry['args'], **retry['kwargs'])
        send.assert_called_once()
        sub.refresh_from_db()
        self.assertFalse(sub.is_pretested)
        self.assertEqual(sub.batch_scoring['cumulative_revision'], 1)
        self.assertNotIn('cumulative_restore_revision', sub.batch_scoring)

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    def test_disabled_rebuild_drains_deferred_restore_without_judging(self):
        from judge.tasks.storage import storage_retry_judge_submission
        sub, retry = self.defer_rebuild_for_restore()
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        with patch.object(Submission, 'judge') as judge:
            storage_retry_judge_submission.run(*retry['args'], **retry['kwargs'])
        judge.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'OFF')

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    def test_obsolete_restore_retry_cannot_abort_a_newer_dispatch(self):
        from judge.tasks.storage import storage_retry_judge_submission
        sub, retry = self.defer_rebuild_for_restore()
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_revision=2)
        config = {'dispatch_id': 'replacement', 'cumulative_revision': 2}
        Submission.objects.filter(pk=sub.pk).update(batch_scoring=config)
        with patch.object(Submission, 'judge') as judge:
            storage_retry_judge_submission.run(*retry['args'], **retry['kwargs'])
        judge.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'QU')
        self.assertEqual(sub.batch_scoring, config)

    @override_settings(STORAGE_ENSURE_READY_ENABLED=True)
    def test_restore_retry_does_not_force_newly_locked_history(self):
        from judge.tasks.storage import storage_retry_judge_submission
        sub, retry = self.defer_rebuild_for_restore()
        Submission.objects.filter(pk=sub.pk).update(locked_after=timezone.now() - timedelta(seconds=1))
        with patch.object(Submission, 'judge') as judge:
            storage_retry_judge_submission.run(*retry['args'], **retry['kwargs'])
        judge.assert_not_called()
        sub.refresh_from_db()
        self.assertEqual(sub.status, 'AB')

    def prepare_profile_refresh(self):
        self.submission((1, 0))
        last = self.submission((0, 1))
        self.profile.calculate_points()
        self.activate('REBUILDING')
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress={
            'phase': 'refresh', 'pending': None, 'cursor': last.pk, 'ceiling': last.pk,
            'completed': 2, 'total': 2, 'started_at': timezone.now().isoformat()})
        self.problem.refresh_from_db()

    def test_unpublished_refresh_keeps_normal_score_and_pp_on_legacy_rule(self):
        self.prepare_profile_refresh()
        self.assertEqual(user_problem_scores(self.profile.pk)[self.problem.pk], 70)
        rows, _ = get_pp_breakdown(self.profile)
        self.assertEqual(rows[0].points, 70)
        self.assertIsNotNone(rows[0].sub_id)
        self.assertEqual(user_problem_scores(self.profile.pk, rebuilding_problem=self.problem)[self.problem.pk], 100)

    def test_failed_refresh_rolls_back_profile_and_pp_before_marking_failed(self):
        from judge.utils.subtask_scoring import refresh_problem_profiles
        self.prepare_profile_refresh()
        def fail_after_refresh(*args, **kwargs):
            refresh_problem_profiles(*args, **kwargs)
            self.assertEqual(Profile.objects.get(pk=self.profile.pk).points, 100)
            raise RuntimeError('Refresh interrupted')
        with patch('judge.tasks.subtask_scoring.refresh_problem_profiles', side_effect=fail_after_refresh):
            with self.assertRaisesMessage(RuntimeError, 'Refresh interrupted'):
                rebuild_subtask_scores.run(self.problem.pk, 1)
        self.problem.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'FAILED')
        self.assertEqual(self.profile.points, 70)
        self.assertEqual(self.profile.performance_points, 70)
        self.assertEqual(user_problem_scores(self.profile.pk)[self.problem.pk], 70)

    def test_queued_dispatch_token_prevents_duplicate_manual_rejudge(self):
        sub = self.submission()
        with patch('judge.judgeapi._send_submission') as send:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(judge_submission(sub, rejudge=True))
                self.assertFalse(judge_submission(sub, rejudge=True))
        self.assertEqual(send.call_count, 1)

    def test_stop_waits_until_running_submissions_drain(self):
        sub = self.submission()
        self.activate()
        Submission.objects.filter(pk=sub.pk).update(status='G')
        set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
        self.problem.refresh_from_db()
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'draining')
        self.assertTrue(Problem.objects.get(pk=self.problem.pk).subtask_tests_locked)
        Submission.objects.filter(pk=sub.pk).update(status='D')
        self.assertEqual(_rebuild_step(self.problem.pk, self.problem.subtask_score_revision), 'OFF')

    def test_failed_judge_does_not_publish_successful_rebuild(self):
        sub = self.submission()
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        progress = {**self.problem.subtask_score_progress, 'phase': 'rejudge', 'pending': sub.pk, 'dispatched': True}
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress=progress)
        Submission.objects.filter(pk=sub.pk).update(status='IE')
        with self.assertRaises(ValidationError):
            rebuild_subtask_scores.run(self.problem.pk, self.problem.subtask_score_revision)
        self.assertEqual(Problem.objects.get(pk=self.problem.pk).subtask_score_state, 'FAILED')

    def test_post_endpoint_enforces_permission_and_action(self):
        request = RequestFactory().post('/', {'action': 'enable'})
        request.user = self.users['normal']
        with self.assertRaises(PermissionDenied):
            configure_subtask_scoring(request, self.problem.code)
        request.user = self.users['superuser']
        self.client.force_login(request.user)
        response = self.client.post(reverse('problem_subtask_scoring', args=[self.problem.code]),
                                    {'action': 'invalid'})
        self.assertEqual(response.status_code, 400)
        message = unescape(response.content.decode())
        self.assertIn('Unsupported action "invalid"', message)
        self.assertIn('"enable"', message)
        self.assertIn('"disable"', message)

    def test_missing_empty_or_multiple_actions_explain_the_request_error(self):
        self.client.force_login(self.users['superuser'])
        url = reverse('problem_subtask_scoring', args=[self.problem.code])
        for payload in ({}, {'action': ''}, {'action': ' '}):
            with self.subTest(payload=payload):
                response = self.client.post(url, payload)
                self.assertEqual(response.status_code, 400)
                self.assertIn('the "action" field is missing or empty', unescape(response.content.decode()))
                self.assertContains(response, 'reload it', status_code=400)
        response = self.client.post(url, {'action': ['enable', 'disable']})
        self.assertContains(response, 'Multiple actions were submitted', status_code=400)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'OFF')

    def test_get_endpoint_explains_how_to_submit_an_action(self):
        self.client.force_login(self.users['superuser'])
        response = self.client.get(reverse('problem_subtask_scoring', args=[self.problem.code]))
        self.assertContains(response, 'This address only accepts POST requests', status_code=405)
        self.assertEqual(response['Allow'], 'POST')

    def test_unsupported_action_is_escaped_in_the_error_page(self):
        self.client.force_login(self.users['superuser'])
        response = self.client.post(reverse('problem_subtask_scoring', args=[self.problem.code]),
                                    {'action': '<script>alert(1)</script>'})
        self.assertContains(response, 'Unsupported action', status_code=400)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;', status_code=400)
        self.assertNotContains(response, '<script>alert(1)</script>', status_code=400)

    def test_valid_action_reports_the_configuration_reason(self):
        self.problem.cases.all().delete()
        self.client.force_login(self.users['superuser'])
        response = self.client.post(reverse('problem_subtask_scoring', args=[self.problem.code]), {'action': 'enable'})
        self.assertContains(response, 'Define at least one full-test subtask', status_code=400)
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_state, 'OFF')

    def test_failed_rebuild_has_separate_retry_and_disable_forms(self):
        self.activate(state='FAILED')
        self.client.force_login(self.users['superuser'])
        response = self.client.get(reverse('problem_edit', args=[self.problem.code]))
        url = reverse('problem_subtask_scoring', args=[self.problem.code])
        self.assertContains(response, '<form method="post" action="%s">' % url, count=2)
        self.assertContains(response, '<input type="hidden" name="action" value="enable">', count=1)
        self.assertContains(response, '<input type="hidden" name="action" value="disable">', count=1)

    @override_settings(CSRF_FAILURE_VIEW='django.views.csrf.csrf_failure')
    def test_management_forms_render_csrf_and_submit_outside_the_edit_form(self):
        url = reverse('problem_subtask_scoring', args=[self.problem.code])

        class ScoringFormParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inside_form = False
                self.csrf_token = None
                self.hidden_fields = {}
                self.form_depth = 0
                self.nested_scoring_form = False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form':
                    self.inside_form = attrs.get('action') == url
                    if self.inside_form and self.form_depth:
                        self.nested_scoring_form = True
                    self.form_depth += 1
                elif tag == 'input' and self.inside_form and attrs.get('type') == 'hidden':
                    self.hidden_fields[attrs.get('name')] = attrs.get('value')
                    if attrs.get('name') == 'csrfmiddlewaretoken':
                        self.csrf_token = attrs.get('value')

            def handle_endtag(self, tag):
                if tag == 'form':
                    self.inside_form = False
                    self.form_depth -= 1

        pages = [('edit', reverse('problem_edit', args=[self.problem.code])),
                 ('admin', reverse('admin:judge_problem_change', args=[self.problem.pk]))]
        for return_to, page_url in pages:
            with self.subTest(page=return_to):
                client = Client(enforce_csrf_checks=True)
                client.force_login(self.users['superuser'])
                response = client.get(page_url)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, '{{ csrf_input }}')
                parser = ScoringFormParser()
                parser.feed(response.content.decode())
                self.assertTrue(parser.csrf_token, 'The scoring form must contain its own rendered CSRF input')
                self.assertFalse(parser.nested_scoring_form)
                self.assertEqual(parser.hidden_fields['action'], 'enable')
                self.assertEqual(parser.hidden_fields['return_to'], return_to)
                self.assertEqual(client.post(url, {'action': 'enable'}).status_code, 403)
                # common.js disables submit buttons before sending. Only hidden
                # inputs are submitted here, with no button name/value added.
                response = client.post(url, parser.hidden_fields)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], page_url + '#subtask-score-controls')
                self.problem.refresh_from_db()
                self.assertEqual(self.problem.subtask_score_state, 'REBUILDING')
                Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='OFF')

    def test_controls_and_progress_move_to_edit_and_admin_for_superusers(self):
        self.activate(state='REBUILDING')
        Problem.objects.filter(pk=self.problem.pk).update(
            subtask_score_progress={'completed': 2, 'total': 7}, subtask_score_error='Judge unavailable')
        self.client.force_login(self.users['superuser'])
        detail = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertNotContains(detail, 'id="subtask-score-controls"')
        self.assertNotContains(detail, 'Judge unavailable')
        for page in (reverse('problem_edit', args=[self.problem.code]),
                     reverse('admin:judge_problem_change', args=[self.problem.pk])):
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertContains(response, 'id="subtask-score-controls"', count=1)
                self.assertContains(response, '2 / 7')
                self.assertContains(response, 'Judge unavailable')
                self.assertContains(response, 'Turn off cumulative scoring')

    def test_non_superuser_editors_do_not_get_scoring_controls(self):
        self.client.force_login(self.users['staff_problem_edit_all'])
        for page in (reverse('problem_edit', args=[self.problem.code]),
                     reverse('admin:judge_problem_change', args=[self.problem.pk])):
            with self.subTest(page=page):
                response = self.client.get(page)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, 'id="subtask-score-controls"')
                self.assertNotContains(response, 'Enable and rejudge all submissions')

    def test_public_score_table_stays_after_controls_are_moved(self):
        self.submission((1, 0), user=self.users['superuser'].profile)
        self.submission((0, 1), user=self.users['superuser'].profile)
        self.activate()
        self.client.force_login(self.users['superuser'])
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertContains(response, '100 / 100')
        self.assertNotContains(response, 'id="subtask-score-controls"')
        self.assertNotContains(response, 'Enable and rejudge all submissions')
        self.assertNotContains(response, 'Turn off cumulative scoring')

    def test_control_redirect_cannot_target_an_external_url(self):
        self.client.force_login(self.users['superuser'])
        response = self.client.post(reverse('problem_subtask_scoring', args=[self.problem.code]),
                                    {'action': 'enable', 'return_to': 'https://example.com'})
        self.assertEqual(response['Location'], reverse('problem_edit', args=[self.problem.code]) +
                         '#subtask-score-controls')

    def test_invalid_problem_edits_keep_controls_bound_to_the_saved_problem(self):
        self.client.force_login(self.users['superuser'])
        action_url = reverse('problem_subtask_scoring', args=[self.problem.code])
        for page in (reverse('problem_edit', args=[self.problem.code]),
                     reverse('admin:judge_problem_change', args=[self.problem.pk])):
            with self.subTest(page=page):
                response = self.client.post(page, {'code': 'unsaved_cumulative_code', 'name': ''})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'action="%s"' % action_url)
                self.assertNotContains(response, 'action="/problem/unsaved_cumulative_code/subtask-scoring"')
                self.problem.refresh_from_db()
                self.assertEqual(self.problem.code, 'cumulative_fixture')

    def test_full_rebuild_updates_users_profile_and_problem_breakdown(self):
        first = self.submission((1, 0))
        second = self.submission((0, 1))
        set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
        self.problem.refresh_from_db()
        revision = self.problem.subtask_score_revision

        def grade(submission, **kwargs):
            self.assertTrue(kwargs['force_full'])
            config = scoring_snapshot(Problem.objects.get(pk=self.problem.pk))
            Submission.objects.filter(pk=submission.pk).update(batch_scoring=config, status='D', is_pretested=False)

        with patch.object(Submission, 'judge', autospec=True, side_effect=grade):
            for sub in (first, second):
                self.assertEqual(_rebuild_step(self.problem.pk, revision), 'dispatched')
                _dispatch(sub.pk, self.problem.pk, revision)
            self.assertEqual(_rebuild_step(self.problem.pk, revision), 'ON')
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.points, 100)
        self.assertEqual(self.profile.performance_points, 100)
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('problem_detail', args=[self.problem.code]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '100 / 100')
        self.assertContains(response, reverse('submission_status', args=[first.pk]))
        self.assertContains(response, reverse('submission_status', args=[second.pk]))

    def test_data_page_and_direct_policy_post_are_locked(self):
        self.activate()
        self.client.force_login(self.users['superuser'])
        url = reverse('problem_data', args=[self.problem.code])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {'action': 'save-batch-scoring', 'batch-mode-full:1': 'sum'})
        self.assertEqual(response.status_code, 409)

    def test_late_bridge_callback_does_not_match_new_dispatch(self):
        sub = self.submission()
        sub.batch_scoring = {**sub.batch_scoring, 'dispatch_id': 'new'}
        sub.save()
        handler = object.__new__(JudgeHandler)
        handler._working = sub.pk
        handler._dispatch_id = 'old'
        self.assertFalse(handler._current_score_run({'submission-id': sub.pk}))
        handler._dispatch_id = 'new'
        self.assertTrue(handler._current_score_run({'submission-id': sub.pk}))


@skipUnless(connection.vendor == 'mysql', 'Requires MariaDB row locking')
class CumulativeConcurrencyTests(CumulativeFixtureMixin, TransactionTestCase):
    def setUp(self):
        type(self).setUpTestData()
        super().setUp()

    @staticmethod
    def isolated(function):
        close_old_connections()
        try:
            return function()
        finally:
            close_old_connections()

    def test_enable_waits_for_test_edit_and_validates_committed_layout(self):
        entered, release, enabling = Event(), Event(), Event()

        def edit():
            with transaction.atomic():
                Problem.objects.select_for_update().get(pk=self.problem.pk)
                self.problem.cases.filter(type='S', order=0).update(points=31)
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Test edit was not released')

        def enable():
            enabling.set()
            with self.assertRaisesMessage(ValidationError, 'compiled test configuration is outdated'):
                set_subtask_scoring(self.problem.pk, self.users['superuser'], True)

        with ThreadPoolExecutor(2) as pool:
            editing = pool.submit(self.isolated, edit)
            self.assertTrue(entered.wait(5))
            activation = pool.submit(self.isolated, enable)
            self.assertTrue(enabling.wait(5))
            try:
                self.assertFalse(activation.done())
            finally:
                release.set()
            editing.result(timeout=10)
            activation.result(timeout=10)
        self.assertEqual(Problem.objects.get(pk=self.problem.pk).subtask_score_state, 'OFF')

    def test_disable_during_refresh_rolls_back_unpublished_profile_scores(self):
        from judge.utils.subtask_scoring import refresh_problem_profiles
        self.submission((1, 0))
        last = self.submission((0, 1))
        self.profile.calculate_points()
        self.activate('REBUILDING')
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress={
            'phase': 'refresh', 'pending': None, 'cursor': last.pk, 'ceiling': last.pk,
            'completed': 2, 'total': 2, 'started_at': timezone.now().isoformat()})
        refreshed, release = Event(), Event()

        def pause_after_refresh(*args, **kwargs):
            refresh_problem_profiles(*args, **kwargs)
            refreshed.set()
            if not release.wait(10):
                raise AssertionError('Refresh was not released')

        with patch('judge.tasks.subtask_scoring._enqueue'), \
             patch('judge.tasks.subtask_scoring.refresh_problem_profiles', side_effect=pause_after_refresh), \
             ThreadPoolExecutor(1) as pool:
            rebuilding = pool.submit(self.isolated, lambda: _rebuild_step(self.problem.pk, 1))
            try:
                self.assertTrue(refreshed.wait(5))
                # Another connection must still see the old committed profile.
                self.assertEqual(Profile.objects.get(pk=self.profile.pk).points, 70)
                set_subtask_scoring(self.problem.pk, self.users['superuser'], False)
            finally:
                release.set()
            self.assertEqual(rebuilding.result(timeout=10), 'obsolete')
        self.profile.refresh_from_db()
        self.problem.refresh_from_db()
        self.assertEqual(self.profile.points, 70)
        self.assertEqual(self.problem.subtask_score_state, 'STOPPING')
        self.assertEqual(self.problem.subtask_score_revision, 2)
        self.assertEqual(_rebuild_step(self.problem.pk, 2), 'OFF')

    def test_simultaneous_activation_only_creates_one_generation(self):
        start = Event()

        def enable():
            start.wait(5)
            try:
                set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
                return 'enabled'
            except ValidationError:
                return 'rejected'

        with patch('judge.tasks.subtask_scoring._enqueue'), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.isolated, enable) for _ in range(2)]
            start.set()
            self.assertCountEqual([future.result(timeout=10) for future in futures], ['enabled', 'rejected'])
        self.problem.refresh_from_db()
        self.assertEqual(self.problem.subtask_score_revision, 1)

    def test_simultaneous_dispatch_only_hands_off_once_after_commit(self):
        sub = self.submission(revision=0)
        with patch('judge.tasks.subtask_scoring._enqueue'):
            set_subtask_scoring(self.problem.pk, self.users['superuser'], True)
            self.problem.refresh_from_db()
            revision = self.problem.subtask_score_revision
            progress = {**self.problem.subtask_score_progress, 'phase': 'rejudge', 'pending': sub.pk}
            Problem.objects.filter(pk=self.problem.pk).update(subtask_score_progress=progress)
            start = Event()

            def dispatch():
                start.wait(5)
                _dispatch(sub.pk, self.problem.pk, revision)

            def bridge_read(packet):
                # A separate DB connection models the bridge process. It must
                # see the new run, not a snapshot from before the outer commit.
                with ThreadPoolExecutor(1) as reader:
                    fresh = reader.submit(self.isolated, lambda: Submission.objects.get(pk=sub.pk)).result(5)
                self.assertEqual(fresh.batch_scoring['cumulative_revision'], revision)
                self.assertTrue(fresh.batch_scoring['dispatch_id'])
                self.assertFalse(fresh.is_pretested)
                self.assertEqual(fresh.status, 'QU')
                return {'name': 'submission-received', 'submission-id': packet['submission-id']}

            with patch('judge.judgeapi.judge_request', side_effect=bridge_read) as bridge, \
                    patch('judge.judgeapi._post_update_submission'), ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(self.isolated, dispatch) for _ in range(2)]
                start.set()
                for future in futures:
                    future.result(timeout=10)
            self.assertEqual(bridge.call_count, 1)

    def test_stale_profile_recalculation_waits_and_reads_all_problem_scores(self):
        self.submission((1, 0))
        self.submission((0, 1))
        self.activate()
        other = create_problem(code='concurrent_other', is_public=True, partial=True, points=100)
        other_sub = Submission.objects.create(problem=other, user=self.profile, language=self.language,
                                              status='D', result='WA', points=0)
        entered, release, recalculating = Event(), Event(), Event()
        stale = Profile.objects.get(pk=self.profile.pk)

        def first_update():
            with transaction.atomic():
                profile = Profile.objects.select_for_update().get(pk=self.profile.pk)
                profile.calculate_points()
                entered.set()
                if not release.wait(10):
                    raise AssertionError('Profile update was not released')

        def recalculate():
            recalculating.set()
            stale.calculate_points()

        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(self.isolated, first_update)
            self.assertTrue(entered.wait(5))
            Submission.objects.filter(pk=other_sub.pk).update(points=40)
            second = pool.submit(self.isolated, recalculate)
            self.assertTrue(recalculating.wait(5))
            try:
                self.assertFalse(second.done())
            finally:
                release.set()
            first.result(timeout=10)
            second.result(timeout=10)
        stale.refresh_from_db()
        self.assertEqual(stale.points, 140)
        self.assertEqual(stale.performance_points, 100 + 40 * Profile._pp_table[1])
