"""Business rules for immutable, first-result practice streaks."""
import copy
import io
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock, patch

import pytz
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from judge.models import (Language, Problem, Submission, SubmissionBatchResult, SubmissionTestCase,
                          StreakContribution, StreakDay, StreakRun, StreakSummary,
                          StreakEpoch, StreakEvent, StreakProgress, ExamTag)
from judge.models.tests.util import CommonDataMixin, create_problem, create_contest, create_user
from judge.tasks.streaks import drain_streak_work
from judge.utils.streaks import (prepare_streak_run, record_completion, process_event, public_summary,
                                public_summary_map, leaderboard_annotations, score_decimal,
                                streak_day, STREAK_TIMEZONE_NAME, publish_offline_streaks)


class AchievementMixin:
    def setUp(self):
        self.profile = self.users['normal'].profile
        self.profile.timezone = 'Asia/Ho_Chi_Minh'
        self.profile.save(update_fields=['timezone'])
        self.problem = create_problem(code='achievement', is_public=True, partial=True, points=100)
        self.language = Language.get_python3()
        self.base = (timezone.now() - timedelta(days=10)).replace(hour=16, minute=59, second=0, microsecond=0)
        self.epoch = StreakEpoch.objects.create(pk=1, submission_id=0, started_at=self.base - timedelta(days=30))
        self.client.defaults['HTTP_HOST'] = 'localhost'

    def new(self, day=0, problem=None, initial=True, **kwargs):
        problem = problem or self.problem
        sub = Submission.objects.create(user=self.profile, problem=problem, language=self.language,
                                        status='QU', **kwargs)
        sub.date = self.base + timedelta(days=day)
        Submission.objects.filter(pk=sub.pk).update(date=sub.date)
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=problem.pk)
            metadata = prepare_streak_run(sub, problem, initial=initial)
        sub.batch_scoring = {'dispatch_id': str(uuid.uuid4()), 'streak': metadata,
            'cumulative_revision': problem.subtask_score_revision, 'pretests': [], 'full': None, 'default': 'min'}
        sub.save(update_fields=['batch_scoring'])
        return sub

    def finish(self, sub, score, vector=None, pretested=False):
        sub.status, sub.result, sub.points = 'D', 'WA', score
        sub.is_pretested = pretested
        sub.case_points, sub.case_total = score, 100
        if vector is not None:
            groups = sub.problem.subtask_score_config['full']
            sub.batch_results.all().delete()
            SubmissionBatchResult.objects.bulk_create([
                SubmissionBatchResult(submission=sub, batch=item['batch'], scoring_type=item['mode'],
                                      points=value, total=item['total']) for item, value in zip(groups, vector)])
        with transaction.atomic():
            sub.save()
            record_completion(sub)
        return sub

    def earn(self, score, day=0, **kwargs):
        sub = self.finish(self.new(day, **kwargs), score)
        drain_streak_work.run()
        return sub

    def days(self):
        return list(StreakDay.objects.filter(user=self.profile).order_by('day').values_list('day', flat=True))

    def cumulative(self, totals=(50, 50), maximum=100, revision=1):
        self.problem.subtask_score_state = 'ON'
        self.problem.subtask_score_revision = revision
        self.problem.points = maximum
        self.problem.subtask_score_config = {'full': [
            {'batch': i, 'count': 1, 'mode': 'min', 'total': total} for i, total in enumerate(totals, 1)]}
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='OFF')
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='ON',
            subtask_score_revision=revision, points=maximum, subtask_score_config=self.problem.subtask_score_config)


@override_settings(STREAKS_ENABLED=True, ALLOWED_HOSTS=['localhost', 'testserver'])
class StreakTests(AchievementMixin, CommonDataMixin, TestCase):
    def test_everyone_starts_zero_without_backfill(self):
        self.assertEqual(public_summary(self.profile)['current'], 0)
        self.assertEqual(public_summary(self.profile)['longest'], 0)
        self.assertFalse(StreakDay.objects.exists())

    def test_only_new_personal_best(self):
        for day, score in enumerate([20, 0, 20, 30, 100, 0, 100]):
            self.earn(score, day)
        self.assertEqual(len(self.days()), 3)
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 2)

    def test_same_day_improvements_and_problems_count_once(self):
        self.earn(20)
        self.earn(30)
        self.earn(10, problem=create_problem(code='achievement2', is_public=True))
        self.assertEqual(len(self.days()), 1)
        self.assertEqual(StreakContribution.objects.count(), 3)

    def test_historical_best_is_seeded_without_a_day(self):
        old = Submission.objects.create(user=self.profile, problem=self.problem, language=self.language,
                                        status='D', points=100)
        self.epoch.submission_id = old.pk
        self.epoch.save()
        self.earn(100)
        self.assertEqual(self.days(), [])
        self.assertEqual(StreakProgress.objects.get().best_points, 100)

    def test_pre_cutover_and_in_flight_cannot_award(self):
        sub = self.new()
        self.epoch.submission_id = sub.pk
        self.epoch.started_at = timezone.now()
        self.epoch.save()
        metadata = prepare_streak_run(sub, self.problem, initial=True)
        self.assertFalse(metadata['eligible'])
        self.finish(sub, 50)
        drain_streak_work.run()
        self.assertFalse(StreakEvent.objects.exists())

    def test_direct_model_save_cannot_award(self):
        sub = Submission.objects.create(user=self.profile, problem=self.problem, language=self.language,
                                        status='D', points=50)
        record_completion(sub)
        drain_streak_work.run()
        self.assertEqual(self.days(), [])

    def test_private_organization_problems_do_not_award(self):
        for i, flags in enumerate([{'is_public': False}, {'is_public': True, 'organizations': ('open',)},
                                   {'is_public': True, 'is_organization_private': True}]):
            self.earn(100, problem=create_problem(code='excluded%d' % i, **flags))
        self.assertEqual(self.days(), [])

    def test_rejudge_cannot_add_or_remove_or_change_baseline(self):
        sub = self.earn(50)
        before = list(StreakContribution.objects.values())
        for status, score in [('D', 0), ('D', 100), ('CE', 0), ('IE', 0), ('AB', 0)]:
            with self.subTest(status=status, score=score):
                sub.status, sub.points = status, score
                sub.batch_scoring['streak'] = prepare_streak_run(sub, self.problem, initial=False)
                sub.save()
                record_completion(sub)
                drain_streak_work.run()
                self.assertEqual(list(StreakContribution.objects.values()), before)
                self.assertEqual(StreakProgress.objects.get().best_points, 50)
        self.earn(60, 1)
        self.assertEqual(len(self.days()), 2)

    def test_rejudge_before_worker_cannot_overwrite_snapshot(self):
        sub = self.finish(self.new(), 50)
        Submission.objects.filter(pk=sub.pk).update(points=0, status='CE', batch_scoring={})
        drain_streak_work.run()
        self.assertEqual(StreakContribution.objects.get().points, 50)

    def test_deletion_before_worker_preserves_achievement(self):
        sub = self.finish(self.new(), 20)
        sub.delete()
        self.problem.delete()
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)
        self.assertEqual(StreakContribution.objects.get().points, 20)

    def test_visibility_and_timezone_changes_never_rewrite_history(self):
        self.earn(30)
        before = self.days()
        self.problem.is_public = False
        self.problem.save(update_fields=['is_public'])
        self.profile.timezone = 'Asia/Tokyo'
        self.profile.save(update_fields=['timezone'])
        drain_streak_work.run()
        self.assertEqual(self.days(), before)

    def test_old_publication_cannot_award(self):
        self.problem.is_public = False
        self.problem.save(update_fields=['is_public'])
        self.earn(40)
        self.problem.is_public = True
        self.problem.save(update_fields=['is_public'])
        drain_streak_work.run()
        self.assertEqual(self.days(), [])

    def test_duplicate_completion_and_worker_delivery(self):
        sub = self.finish(self.new(), 20)
        record_completion(sub)
        event = StreakEvent.objects.get()
        self.assertTrue(process_event(event.pk))
        self.assertFalse(process_event(event.pk))
        self.assertEqual(StreakContribution.objects.count(), 1)

    def test_transaction_rollback_leaves_retryable_work(self):
        self.finish(self.new(), 20)
        event = StreakEvent.objects.get()
        with patch('judge.utils.streaks._add_day', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):
                process_event(event.pk)
        self.assertTrue(StreakEvent.objects.get().pending)
        self.assertFalse(StreakContribution.objects.exists())
        process_event(event.pk)
        self.assertEqual(len(self.days()), 1)

    def test_worker_retries_failed_event(self):
        self.finish(self.new(), 20)
        with patch('judge.utils.streaks._add_day', side_effect=RuntimeError('retry')):
            drain_streak_work.run()
        event = StreakEvent.objects.get()
        self.assertTrue(event.pending)
        self.assertEqual(event.failures, 1)
        StreakEvent.objects.update(retry_at=None)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    @override_settings(STREAKS_BATCH_SIZE=25)
    def test_withheld_backlog_does_not_starve_ready_achievements(self):
        # Four batches cover the entire 60-second retry interval with a
        # 15-second Beat schedule. ID ordering would keep selecting them forever.
        for _ in range(100):
            self.finish(self.new(offline_hidden=True), 10)
        ready = self.finish(self.new(), 20)
        now = timezone.now()
        for tick in range(5):
            with patch('django.utils.timezone.now', return_value=now + timedelta(seconds=15 * tick)):
                drain_streak_work.run()
        self.assertTrue(StreakContribution.objects.filter(submission_id=ready.pk).exists())
        self.assertEqual(StreakContribution.objects.count(), 1)
        self.assertEqual(StreakEvent.objects.filter(pending=True, applied=False).count(), 100)

    def test_late_grading_merges_days_on_original_submission_date(self):
        late = self.new(1)
        self.earn(20, 0)
        self.earn(30, 2)
        self.finish(late, 40)
        drain_streak_work.run()
        run = StreakRun.objects.get()
        self.assertEqual(run.length, 3)
        self.assertEqual(run.start, streak_day(self.base))

    def test_timezone_change_while_grading_keeps_vietnam_submission_day(self):
        self.base = self.base.replace(hour=10, minute=30)
        self.profile.timezone = 'Pacific/Kiritimati'
        self.profile.save(update_fields=['timezone'])
        sub = self.new()
        self.assertEqual(sub.batch_scoring['streak']['timezone'], STREAK_TIMEZONE_NAME)
        self.profile.timezone = 'Pacific/Pago_Pago'
        self.profile.save(update_fields=['timezone'])
        self.finish(sub, 30)
        drain_streak_work.run()
        self.assertEqual(self.days(), [streak_day(sub.date)])
        self.assertEqual(StreakSummary.objects.get(user=self.profile).timezone, STREAK_TIMEZONE_NAME)

    def test_switching_timezones_cannot_turn_minutes_into_three_streak_days(self):
        start = self.base.replace(hour=10, minute=30)
        for index, zone in enumerate(('Pacific/Pago_Pago', 'Asia/Ho_Chi_Minh', 'Pacific/Kiritimati')):
            self.profile.timezone = zone
            self.profile.save(update_fields=['timezone'])
            self.base = start + timedelta(minutes=index)
            self.earn(10 + index)
        self.assertEqual(self.days(), [streak_day(start)])
        self.assertEqual(StreakContribution.objects.filter(user=self.profile).count(), 3)
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 1)

    def test_cumulative_timezone_switches_keep_one_vietnam_day(self):
        self.cumulative()
        start = self.base.replace(hour=10, minute=30)
        for index, zone in enumerate(('Pacific/Pago_Pago', 'Asia/Ho_Chi_Minh', 'Pacific/Kiritimati')):
            self.profile.timezone = zone
            self.profile.save(update_fields=['timezone'])
            self.base = start + timedelta(minutes=index)
            self.finish(self.new(), 10 + index, vector=(10 + index, 0))
        drain_streak_work.run()
        self.assertEqual(self.days(), [streak_day(start)])
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 1)
        self.assertEqual(StreakContribution.objects.filter(user=self.profile).count(), 3)

    def test_summary_and_leaderboard_ignore_profile_and_server_timezones(self):
        now = self.base.replace(hour=10, minute=30)
        today = streak_day(now)
        summary = StreakSummary.objects.create(user=self.profile, timezone='Pacific/Pago_Pago',
            last_day=today, current_length=7, longest=9)
        with override_settings(TIME_ZONE='Pacific/Kiritimati'):
            for age, expected in ((0, 7), (1, 7), (2, 0)):
                summary.last_day = today - timedelta(days=age)
                summary.save(update_fields=['last_day'])
                for zone in ('Pacific/Pago_Pago', 'Pacific/Kiritimati', 'UTC', 'Asia/Tokyo'):
                    with self.subTest(age=age, zone=zone):
                        self.profile.timezone = zone
                        self.profile.save(update_fields=['timezone'])
                        public = public_summary(self.profile, now=now)
                        batched = public_summary_map([self.profile], now=now)[self.profile.pk]
                        ranked = type(self.profile).objects.annotate(**leaderboard_annotations(now=now)).get(pk=self.profile.pk)
                        self.assertEqual(public['today'], today)
                        self.assertEqual(public['current'], expected)
                        self.assertEqual(public['kept_today'], age == 0)
                        self.assertEqual(batched, public)
                        self.assertEqual(ranked.streak_current, expected)
                        self.assertEqual(ranked.streak_longest, 9)

    def test_vietnam_midnight_starts_next_day_for_every_profile_timezone(self):
        self.profile.timezone = 'Pacific/Pago_Pago'
        self.profile.save(update_fields=['timezone'])
        self.base = self.base.replace(hour=16, minute=59)
        before_midnight = self.base
        self.earn(10)
        self.assertTrue(public_summary(self.profile, now=before_midnight)['kept_today'])
        self.base += timedelta(minutes=1)
        self.assertEqual(streak_day(self.base), streak_day(before_midnight) + timedelta(days=1))
        self.assertFalse(public_summary(self.profile, now=self.base)['kept_today'])
        self.earn(20)
        self.assertEqual(self.days(), [streak_day(before_midnight), streak_day(self.base)])
        self.assertTrue(public_summary(self.profile, now=self.base)['kept_today'])
        self.assertEqual(public_summary(self.profile, now=self.base)['current'], 2)

    def test_old_dispatch_day_is_ignored_when_first_grading_finishes(self):
        self.base = self.base.replace(hour=10, minute=30)
        sub = self.new()
        # A run admitted just before this change could still carry a different
        # profile timezone/day. Completion must use the fixed calendar anyway.
        sub.batch_scoring['streak'].update(timezone='Pacific/Kiritimati',
            day=(streak_day(sub.date) + timedelta(days=1)).isoformat())
        self.finish(sub, 30)
        drain_streak_work.run()
        self.assertEqual(StreakEvent.objects.get(submission_id=sub.pk).day, streak_day(sub.date))
        self.assertEqual(self.days(), [streak_day(sub.date)])

    def test_record_survives_missed_day(self):
        self.earn(10, 0)
        self.earn(20, 1)
        self.assertEqual(public_summary(self.profile, now=self.base + timedelta(days=5))['current'], 0)
        self.assertEqual(public_summary(self.profile)['longest'], 2)

    def test_future_leaderboard_matches_public_summary(self):
        today = streak_day()
        StreakSummary.objects.create(user=self.profile, timezone=self.profile.timezone,
            last_day=today + timedelta(days=1), current_length=4, longest=4)
        annotated = type(self.profile).objects.annotate(**leaderboard_annotations()).get(pk=self.profile.pk)
        self.assertEqual(annotated.streak_current, public_summary(self.profile)['current'])
        self.assertEqual(annotated.streak_current, 0)

    def test_summary_map_stays_one_query(self):
        self.earn(30)
        with CaptureQueriesContext(connection) as queries:
            public_summary_map([self.profile])
        self.assertEqual(len(queries), 1)

    def test_unlisted_users_keep_earning_and_reappear_without_rebuilding(self):
        self.base = timezone.now() - timedelta(days=1)
        self.profile.is_unlisted = True
        self.profile.save(update_fields=['is_unlisted'])
        self.earn(10)
        self.earn(20, 1)
        summary = StreakSummary.objects.get(user=self.profile)
        self.assertEqual((summary.current_length, summary.longest), (2, 2))
        self.assertEqual(StreakProgress.objects.get(user=self.profile).best_points, 20)
        self.assertEqual(StreakContribution.objects.filter(user=self.profile).count(), 2)
        self.assertFalse(StreakEvent.objects.filter(pending=True).exists())
        saved_days = self.days()
        with self.assertNumQueries(0):
            self.assertIsNone(public_summary(self.profile))
            self.assertEqual(public_summary_map([self.profile]), {})

        self.profile.is_unlisted = False
        self.profile.save(update_fields=['is_unlisted'])
        self.assertEqual(public_summary(self.profile)['current'], 2)
        self.assertEqual(public_summary(self.profile)['longest'], 2)
        self.assertEqual(self.days(), saved_days)

    def test_unlisting_between_dispatch_completion_and_worker_keeps_cumulative_progress(self):
        self.cumulative()
        first = self.new()
        self.profile.is_unlisted = True
        self.profile.save(update_fields=['is_unlisted'])
        self.finish(first, 40, vector=(40, 0))
        second = self.new(1)
        self.finish(second, 40, vector=(0, 40))
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 2)
        self.assertEqual(StreakProgress.objects.get(user=self.profile).best_points, 80)
        self.assertIsNone(public_summary(self.profile))
        self.profile.is_unlisted = False
        self.profile.save(update_fields=['is_unlisted'])
        self.assertEqual(public_summary(self.profile)['longest'], 2)

    def test_summary_map_excludes_unlisted_users_in_one_query(self):
        self.earn(30)
        hidden = self.users['staff_problem_edit_own'].profile
        hidden.is_unlisted = True
        hidden.save(update_fields=['is_unlisted'])
        StreakSummary.objects.create(user=hidden, last_day=streak_day(), current_length=365, longest=365)
        with self.assertNumQueries(1):
            summaries = public_summary_map([self.profile, hidden, self.profile])
        self.assertEqual(set(summaries), {self.profile.pk})

    def test_submission_profiles_preload_unlisted_flag(self):
        from judge.views.submission import submission_related
        sub = self.earn(30)
        loaded = submission_related(Submission.objects.all()).get(pk=sub.pk)
        with self.assertNumQueries(1):
            self.assertIn(self.profile.pk, public_summary_map([loaded.user]))

    def test_unlisted_profiles_hide_streaks_for_every_viewer(self):
        self.base = timezone.now()
        sub = self.earn(30)
        self.profile.is_unlisted = True
        self.profile.save(update_fields=['is_unlisted'])
        username = self.users['normal'].username
        calendar = reverse('user_streaks', args=[username])
        for viewer in (None, self.users['normal'], self.users['staff_problem_edit_own'], self.users['superuser']):
            with self.subTest(viewer=viewer.username if viewer else 'anonymous'):
                self.client.logout()
                if viewer:
                    self.client.force_login(viewer)
                pages = ['user_page', 'user_problems', 'user_blog']
                if viewer and viewer.is_superuser:
                    pages.append('user_comment')
                for page in pages:
                    response = self.client.get(reverse(page, args=[username]))
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.context['streak_enabled'])
                    self.assertNotContains(response, 'href="%s"' % calendar)
                    self.assertNotContains(response, 'streak-public-card')
                    self.assertNotContains(response, 'streak-header-badge')
                response = self.client.get(calendar)
                self.assertEqual(response.status_code, 404)
                self.assertNotContains(response, 'streak-heat-grid', status_code=404)
                submissions = self.client.get(reverse('all_user_submissions', args=[username]))
                self.assertEqual(submissions.status_code, 200)
                self.assertNotContains(submissions, 'class="streak-badge')
                row = self.client.get(reverse('submission_single_query'), {'id': sub.pk})
                self.assertEqual(row.status_code, 200)
                self.assertNotContains(row, 'class="streak-badge')

        self.profile.is_unlisted = False
        self.profile.save(update_fields=['is_unlisted'])
        response = self.client.get(calendar)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['streak']['current'], 1)
        self.assertContains(self.client.get(reverse('user_page', args=[username])), 'streak-public-card')
        self.assertContains(self.client.get(reverse('all_user_submissions', args=[username])), 'class="streak-badge')
        self.assertContains(self.client.get(reverse('submission_single_query'), {'id': sub.pk}), 'class="streak-badge')

    def test_unrevealed_offline_result_waits_and_keeps_original_date(self):
        sub = self.finish(self.new(offline_hidden=True), 40)
        drain_streak_work.run()
        self.assertEqual(self.days(), [])
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=False)
        StreakEvent.objects.update(retry_at=None)
        drain_streak_work.run()
        self.assertEqual(self.days(), [streak_day(sub.date)])
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=True)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    def test_blind_contests_never_leak_awards(self):
        for flag in ('offline_mode', 'has_hidden_subtasks'):
            contest = create_contest(key='blind_' + flag, end_time=timezone.now() - timedelta(hours=1), **{flag: True})
            sub = self.finish(self.new(contest_object=contest), 40)
            drain_streak_work.run()
            self.assertEqual(self.days(), [])
            contest.delete()
            StreakEvent.objects.all().delete()
            StreakProgress.objects.all().delete()

    def test_withheld_result_does_not_suppress_a_visible_achievement(self):
        hidden = self.finish(self.new(offline_hidden=True), 80)
        self.earn(30, 1)
        self.assertEqual(len(self.days()), 1)
        self.assertEqual(StreakProgress.objects.get().best_points, 30)
        Submission.objects.filter(pk=hidden.pk).update(offline_hidden=False)
        StreakEvent.objects.update(retry_at=None)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(StreakProgress.objects.get().best_points, 80)

    def test_pending_cumulative_snapshot_survives_rejudge_before_publication(self):
        self.cumulative()
        hidden = self.finish(self.new(offline_hidden=True), 40, vector=(40, 0))
        hidden.batch_results.all().update(points=0)
        Submission.objects.filter(pk=hidden.pk).update(points=0, offline_hidden=False)
        drain_streak_work.run()
        self.assertEqual(StreakProgress.objects.get().best_points, 40)
        self.assertEqual(len(self.days()), 1)

    def test_active_contest_award_waits_until_end(self):
        contest = create_contest(key='active_streak', end_time=timezone.now() + timedelta(hours=1))
        self.finish(self.new(contest_object=contest), 40)
        drain_streak_work.run()
        self.assertEqual(self.days(), [])
        type(contest).objects.filter(pk=contest.pk).update(end_time=timezone.now() - timedelta(seconds=1))
        StreakEvent.objects.update(available_at=timezone.now(), retry_at=None)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    def test_evidence_hides_newly_private_problem_and_blind_contest(self):
        from django.test import RequestFactory
        from judge.views.streaks import UserStreakPage
        sub = self.earn(37)
        self.problem.is_public = False
        self.problem.save(update_fields=['is_public'])
        request = RequestFactory().get('/', {'year': sub.date.year, 'day': self.days()[0].isoformat()})
        request.user, request.profile = self.users['normal'], self.profile
        view = UserStreakPage()
        view.request, view.object, view.hide_solved = request, self.profile, False
        self.assertEqual(view.get_context_data()['contributions'], [])
        self.assertEqual(len(self.days()), 1)

    @patch('statici18n.templatetags.statici18n.staticfiles_storage.open')
    def test_public_calendar_targets_requested_user_for_every_viewer(self, static_open):
        static_open.return_value.read.return_value = b''
        self.earn(30)
        url = reverse('user_streaks', args=[self.users['normal'].username])
        self.profile.timezone = 'Pacific/Pago_Pago'
        self.profile.save(update_fields=['timezone'])
        for viewer in (None, self.users['normal'], self.users['staff_problem_edit_own'], self.users['superuser']):
            with self.subTest(viewer=viewer.username if viewer else 'anonymous'):
                self.client.logout()
                if viewer:
                    self.client.force_login(viewer)
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['user'].pk, self.profile.pk)
                self.assertEqual(response.context['streak']['longest'], 1)
                self.assertEqual(len(response.context['streak_runs']), 1)
                self.assertIn('no-store', response['Cache-Control'])
                self.assertContains(response, 'streak-heat-grid')
                self.assertEqual(response.context['streak_timezone'], STREAK_TIMEZONE_NAME)
                self.assertContains(response, STREAK_TIMEZONE_NAME)
                self.assertNotContains(response, 'My streak')

    def test_streak_tab_is_visible_on_other_users_profiles(self):
        url = reverse('user_page', args=[self.users['normal'].username])
        calendar = reverse('user_streaks', args=[self.users['normal'].username])
        for viewer in (None, self.users['superuser']):
            self.client.logout()
            if viewer:
                self.client.force_login(viewer)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'href="%s"' % calendar)
            self.assertNotContains(response, 'My streak')

    def test_anonymous_calendar_shows_public_progress_for_the_requested_user(self):
        sub = self.earn(37)
        response = self.client.get(reverse('user_streaks', args=[self.users['normal'].username]),
                                   {'year': self.days()[0].year, 'day': self.days()[0].isoformat()})
        self.assertEqual(response.status_code, 200)
        evidence = response.context['contributions']
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]['submission'].pk, sub.pk)
        self.assertEqual((evidence[0]['before'], evidence[0]['after']), (0, 37))

    def test_public_calendar_keeps_days_but_hides_private_or_offline_evidence(self):
        sub = self.earn(37)
        url = reverse('user_streaks', args=[self.users['normal'].username])
        query = {'year': self.days()[0].year, 'day': self.days()[0].isoformat()}
        for field, hidden, visible in (('is_public', False, True), ('is_organization_private', True, False)):
            with self.subTest(field=field):
                Problem.objects.filter(pk=self.problem.pk).update(**{field: hidden})
                response = self.client.get(url, query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['contributions'], [])
                self.assertEqual(response.context['streak']['longest'], 1)
                self.assertEqual(len(response.context['streak_runs']), 1)
                Problem.objects.filter(pk=self.problem.pk).update(**{field: visible})
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=True)
        response = self.client.get(url, query)
        self.assertEqual(response.context['contributions'], [])
        self.assertEqual(response.context['streak']['longest'], 1)

    def test_public_calendar_hides_withheld_contest_evidence(self):
        sub = self.earn(37)
        url = reverse('user_streaks', args=[self.users['normal'].username])
        query = {'year': self.days()[0].year, 'day': self.days()[0].isoformat()}
        for flag in ('offline_mode', 'has_hidden_subtasks'):
            contest = create_contest(key='evidence_' + flag,
                                     end_time=timezone.now() - timedelta(hours=1), **{flag: True})
            Submission.objects.filter(pk=sub.pk).update(contest_object=contest)
            response = self.client.get(url, query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context['contributions'], [])
            self.assertEqual(response.context['streak']['longest'], 1)
        contest = create_contest(key='public_evidence_active', end_time=timezone.now() + timedelta(hours=1))
        Submission.objects.filter(pk=sub.pk).update(contest_object=contest)
        self.assertEqual(self.client.get(url, query).context['contributions'], [])

    def test_public_streaks_still_follow_feature_flag(self):
        with override_settings(STREAKS_ENABLED=False):
            response = self.client.get(reverse('user_streaks', args=[self.users['normal'].username]))
            self.assertEqual(response.status_code, 404)
            profile = self.client.get(reverse('user_page', args=[self.users['normal'].username]))
            self.assertNotContains(profile, reverse('user_streaks', args=[self.users['normal'].username]))

    def test_cumulative_equal_raw_score_can_improve(self):
        self.cumulative()
        first = self.new()
        self.finish(first, 40, vector=(40, 0))
        second = self.new(1)
        self.finish(second, 40, vector=(0, 40))
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(StreakProgress.objects.get().best_points, 80)

    def test_cumulative_lower_raw_score_can_improve(self):
        self.cumulative()
        self.finish(self.new(), 50, vector=(50, 0))
        self.finish(self.new(1), 40, vector=(0, 40))
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(StreakContribution.objects.order_by('-id').first().points, 90)

    def test_cumulative_higher_raw_score_without_improvement_does_not_award(self):
        self.cumulative()
        self.finish(self.new(), 40, vector=(40, 0))
        self.finish(self.new(1), 40, vector=(0, 40))
        self.finish(self.new(2), 50, vector=(30, 20))
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(StreakProgress.objects.get().best_points, 80)

    def test_cumulative_partial_subtask_and_scaling_matches_oj(self):
        from judge.utils.subtask_scoring import cumulative_breakdown
        self.cumulative(totals=(60, 140), maximum=40)
        self.finish(self.new(), 6, vector=(30, 0))
        self.finish(self.new(1), 20, vector=(0, 100))
        drain_streak_work.run()
        self.assertEqual(StreakProgress.objects.get().best_points, Decimal('26'))
        self.assertEqual(float(StreakProgress.objects.get().best_points), cumulative_breakdown(self.profile.pk, self.problem)['points'])

    def test_cumulative_old_vector_is_baseline_without_streak(self):
        self.cumulative()
        old = self.finish(self.new(), 40, vector=(40, 0))
        StreakEvent.objects.all().delete()
        StreakProgress.objects.all().delete()
        self.epoch.submission_id = old.pk
        self.epoch.save()
        self.finish(self.new(1), 40, vector=(0, 40))
        drain_streak_work.run()
        row = StreakContribution.objects.get()
        self.assertEqual((row.previous_points, row.points), (40, 80))

    def test_cumulative_rejudge_vector_cannot_change_ledger(self):
        self.cumulative()
        sub = self.finish(self.new(), 40, vector=(40, 0))
        sub.batch_scoring['streak'] = prepare_streak_run(sub, self.problem, initial=False)
        self.finish(sub, 100, vector=(50, 50))
        self.finish(self.new(1), 40, vector=(0, 40))
        drain_streak_work.run()
        self.assertEqual(StreakProgress.objects.get().best_points, 80)
        self.assertEqual(len(self.days()), 2)

    def test_cumulative_revisions_do_not_mix_and_mode_change_does_not_award(self):
        self.cumulative()
        self.finish(self.new(), 50, vector=(50, 0))
        drain_streak_work.run()
        self.cumulative(revision=2)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)
        self.finish(self.new(1), 20, vector=(0, 20))
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(list(StreakProgress.objects.order_by('id').values_list('best_points', flat=True)), [50, 20])

    def test_real_pretests_never_award_but_full_fallback_does(self):
        self.cumulative()
        sub = self.new()
        sub.batch_scoring['pretests'] = [{'batch': 1, 'count': 1, 'total': 50, 'mode': 'min'}]
        self.finish(sub, 50, vector=(50, 0), pretested=True)
        self.assertFalse(StreakEvent.objects.exists())
        self.finish(self.new(1), 50, vector=(50, 0), pretested=True)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    def test_invalid_subtask_rows_and_wrong_revision_do_not_award(self):
        self.cumulative()
        for i, invalid in enumerate(('missing', 'negative', 'total', 'revision')):
            sub = self.new(i)
            self.finish(sub, 20, vector=(20, 0))
            StreakEvent.objects.all().delete()
            StreakProgress.objects.update(best_points=0, subtasks={})
            if invalid == 'missing':
                sub.batch_results.filter(batch=2).delete()
            elif invalid == 'negative':
                sub.batch_results.filter(batch=1).update(points=-1)
            elif invalid == 'total':
                sub.batch_results.filter(batch=1).update(total=100)
            else:
                sub.batch_scoring['cumulative_revision'] = 999
            record_completion(sub)
            self.assertFalse(StreakEvent.objects.exists())

    def test_decimal_comparison_uses_score_precision(self):
        self.earn(0.001)
        self.earn(0.0010000001, 1)
        self.earn(0.002, 2)
        self.assertEqual(len(self.days()), 2)
        self.assertEqual(score_decimal(float('nan')), 0)
        self.assertEqual(score_decimal(float('inf')), 0)

    @patch('judge.judgeapi._send_submission', return_value=True)
    def test_dispatch_marks_initial_and_rejudge_explicitly(self, send):
        from judge.judgeapi import judge_submission
        sub = Submission.objects.create(user=self.profile, problem=self.problem, language=self.language)
        judge_submission(sub)
        sub.refresh_from_db()
        self.assertEqual(sub.batch_scoring['streak']['kind'], 'initial')
        self.assertTrue(sub.batch_scoring['streak']['eligible'])
        Submission.objects.filter(pk=sub.pk).update(status='D')
        judge_submission(sub, rejudge=True)
        sub.refresh_from_db()
        self.assertEqual(sub.batch_scoring['streak']['kind'], 'rejudge')
        self.assertFalse(sub.batch_scoring['streak']['eligible'])

    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_bridge_finalization_captures_batch_results_and_event(self, finished, post):
        from judge.bridge.judge_handler import JudgeHandler
        sub = self.new()
        SubmissionTestCase.objects.create(submission=sub, case=1, batch=None,
                                          status='WA', points=40, total=100, time=0, memory=0)
        handler = object.__new__(JudgeHandler)
        handler.name, handler._working = 'streak-test', sub.pk
        handler._dispatch_id = sub.batch_scoring['dispatch_id']
        handler._free_self = Mock()
        handler._make_json_log = Mock(return_value={})
        handler._post_update_submission = Mock()
        handler._update_scoring_stats = Mock()
        handler.on_grading_end({'name': 'grading-end', 'submission-id': sub.pk})
        self.assertEqual(StreakEvent.objects.get().points, 40)
        self.assertEqual(sub.batch_results.get().points, 40)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    @patch('judge.external_judge._finish_submission_updates')
    def test_external_initial_completion_and_rejudge(self, finished):
        from judge.external_judge import finalize_external_submission
        from judge.models import ExternalSubmission
        sub = self.new()
        ext = ExternalSubmission.objects.create(submission=sub, pcd_submission_id=uuid.uuid4())
        with patch('judge.external_judge.get_external_status_canonical', return_value='AC'):
            self.assertTrue(finalize_external_submission(sub, ext, {}))
        self.assertEqual(StreakEvent.objects.get().points, 100)
        sub.refresh_from_db()
        sub.batch_scoring['streak'] = prepare_streak_run(sub, self.problem, initial=False)
        sub.save(update_fields=['batch_scoring'])
        with patch('judge.external_judge.get_external_status_canonical', return_value='WA'):
            self.assertTrue(finalize_external_submission(sub, ext, {}))
        self.assertEqual(StreakEvent.objects.count(), 1)
        self.assertEqual(StreakProgress.objects.get().best_points, 100)

    @patch('judge.external_judge._finish_submission_updates')
    def test_stale_external_callback_cannot_award(self, finished):
        from judge.external_judge import finalize_external_submission
        from judge.models import ExternalSubmission
        sub = self.new()
        ext = ExternalSubmission.objects.create(submission=sub, pcd_submission_id=uuid.uuid4())
        ExternalSubmission.objects.filter(pk=ext.pk).update(pcd_submission_id=uuid.uuid4())
        with patch('judge.external_judge.get_external_status_canonical', return_value='AC'):
            self.assertFalse(finalize_external_submission(sub, ext, {}))
        self.assertFalse(StreakEvent.objects.exists())

    def test_mode_disable_baseline_does_not_award_or_remove(self):
        self.cumulative()
        self.finish(self.new(), 40, vector=(40, 0))
        drain_streak_work.run()
        Problem.objects.filter(pk=self.problem.pk).update(subtask_score_state='OFF', subtask_score_revision=2)
        self.problem.refresh_from_db()
        self.finish(self.new(1), 40)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    def test_normal_late_lower_result_does_not_exceed_completion_highwater(self):
        late = self.new(0)
        self.earn(60, 1)
        self.finish(late, 50)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)
        self.assertEqual(StreakProgress.objects.get().best_points, 60)

    def test_actual_offline_attempt_reveal_releases_original_snapshot(self):
        from judge.models import ExamTag
        from judge.utils.exam_offline import start_attempt, finish_attempt, reveal_attempt, link_submission
        exam = ExamTag.objects.create(slug='streak_offline', name='Streak offline', duration_minutes=30,
                                      virtual_offline_enabled=True, day_count=1)
        self.problem.exam_tags.add(exam)
        attempt = start_attempt(self.profile.pk, exam.pk)
        sub = self.new(offline_hidden=True)
        link_submission(attempt.problems.get(), sub)
        self.finish(sub, 40)
        drain_streak_work.run()
        self.assertEqual(self.days(), [])
        finish_attempt(self.profile.pk, attempt.pk, early=True)
        reveal_attempt(self.profile.pk, attempt.pk)
        StreakEvent.objects.update(retry_at=None)
        drain_streak_work.run()
        self.assertEqual(StreakContribution.objects.get().points, 40)
        self.assertEqual(len(self.days()), 1)

    @patch('judge.bridge.judge_handler._ensure_connection')
    def test_bridge_snapshot_preserves_initial_run_metadata(self, ensure_connection):
        from judge.bridge.judge_handler import JudgeHandler
        sub = self.new()
        before = copy.deepcopy(sub.batch_scoring['streak'])
        handler = object.__new__(JudgeHandler)
        handler.get_related_submission_data(sub.pk)
        sub.refresh_from_db()
        self.assertEqual(sub.batch_scoring['streak'], before)
        self.assertEqual(handler._dispatch_id, sub.batch_scoring['dispatch_id'])

    def test_initialize_archives_history_seeds_best_and_cannot_reset_again(self):
        self.earn(100)
        StreakEpoch.objects.all().delete()
        StreakEvent.objects.all().delete()
        StreakProgress.objects.all().delete()
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'archive.json.gz'
            call_command('initialize_streaks', apply=True, archive=str(archive), stdout=io.StringIO())
            self.assertTrue(archive.exists())
            self.assertFalse(StreakDay.objects.exists())
            self.assertEqual(StreakProgress.objects.get().best_points, 100)
            with self.assertRaises(CommandError):
                call_command('initialize_streaks', apply=True, archive=str(archive), stdout=io.StringIO())

    def test_zero_baseline_is_frozen_before_historical_rejudge(self):
        old = Submission.objects.create(user=self.profile, problem=self.problem, language=self.language,
                                        status='CE', points=0)
        StreakEpoch.objects.all().delete()
        with tempfile.TemporaryDirectory() as tmp:
            call_command('initialize_streaks', apply=True, archive=str(Path(tmp) / 'zero.json.gz'), stdout=io.StringIO())
        self.assertEqual(StreakProgress.objects.get().best_points, 0)
        Submission.objects.filter(pk=old.pk).update(status='D', points=100)
        sub = Submission.objects.create(user=self.profile, problem=self.problem, language=self.language)
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=self.problem.pk)
            sub.batch_scoring = {'streak': prepare_streak_run(sub, self.problem, initial=True)}
        self.finish(sub, 20)
        drain_streak_work.run()
        self.assertEqual(StreakContribution.objects.get().previous_points, 0)

    def test_earned_public_contest_result_is_preserved_if_hidden_before_worker(self):
        contest = create_contest(key='public_then_blind', end_time=timezone.now() - timedelta(hours=1))
        self.finish(self.new(contest_object=contest), 40)
        type(contest).objects.filter(pk=contest.pk).update(offline_mode=True)
        drain_streak_work.run()
        self.assertEqual(len(self.days()), 1)

    def test_retired_backfill_cannot_restore_old_days(self):
        with self.assertRaises(CommandError):
            call_command('backfill_streaks', stdout=io.StringIO())


@override_settings(STREAKS_ENABLED=True, ALLOWED_HOSTS=['localhost', 'testserver'])
class OfflineStreakRevealTests(AchievementMixin, CommonDataMixin, TestCase):
    def start_offline(self, problems=None):
        from judge.utils.exam_offline import start_attempt
        exam = ExamTag.objects.create(slug='offline-reveal', name='Offline reveal', duration_minutes=30,
                                      virtual_offline_enabled=True, day_count=1)
        for problem in problems or [self.problem]:
            problem.exam_tags.add(exam)
        with patch('django.utils.timezone.now', return_value=self.base):
            return start_attempt(self.profile.pk, exam.pk)

    def offline_submission(self, attempt, points=None, vector=None, problem=None):
        from judge.utils.exam_offline import link_submission
        sub = self.new(offline_hidden=True, problem=problem)
        link_submission(attempt.problems.get(problem_id=sub.problem_id), sub)
        if points is not None:
            with patch('django.utils.timezone.now', return_value=self.base + timedelta(minutes=5)):
                self.finish(sub, points, vector=vector)
        return sub

    def reveal(self, attempt, when):
        from judge.utils.exam_offline import finish_attempt, reveal_attempt
        with patch('django.utils.timezone.now', return_value=when):
            finish_attempt(self.profile.pk, attempt.pk, early=True)
            return reveal_attempt(self.profile.pk, attempt.pk)

    def drain(self, when):
        with patch('django.utils.timezone.now', return_value=when):
            return drain_streak_work.run()

    def test_hidden_public_problem_waits_then_earns_reveal_day(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        self.drain(opened)
        self.assertEqual(self.days(), [])
        self.assertEqual(StreakProgress.objects.get().best_points, 0)
        self.reveal(attempt, opened)
        event = StreakEvent.objects.get(submission_id=sub.pk)
        self.assertTrue(event.applied)
        self.assertEqual((event.previous_points, event.points), (0, 40))
        self.assertEqual(event.day, streak_day(opened))
        self.assertEqual(StreakProgress.objects.get().best_points, 40)
        self.assertEqual(self.days(), [])
        self.drain(opened)
        self.assertEqual(self.days(), [streak_day(opened)])
        self.assertTrue(public_summary(self.profile, now=opened)['kept_today'])
        response = self.client.get(reverse('user_streaks', args=[self.users['normal'].username]),
                                   {'year': event.day.year, 'day': event.day.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['contributions'][0]['submission'].pk, sub.pk)

    def test_no_longer_improving_at_reveal_does_not_earn(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        with patch('django.utils.timezone.now', return_value=self.base + timedelta(days=1)):
            self.earn(60, 1)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        event = StreakEvent.objects.get(submission_id=sub.pk)
        self.assertTrue(event.applied)
        self.assertFalse(event.pending)
        self.assertEqual((event.previous_points, event.points), (60, 60))
        self.drain(opened)
        self.assertFalse(StreakContribution.objects.filter(submission_id=sub.pk).exists())
        self.assertNotIn(streak_day(opened), self.days())

    def test_later_practice_cannot_change_the_frozen_reveal_decision(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        with patch('django.utils.timezone.now', return_value=opened + timedelta(minutes=1)):
            self.finish(self.new(2), 60)
        self.drain(opened + timedelta(minutes=2))
        award = StreakContribution.objects.get(submission_id=sub.pk)
        self.assertEqual((award.previous_points, award.points), (0, 40))
        self.assertEqual(award.day, streak_day(opened))
        self.assertEqual(StreakProgress.objects.get().best_points, 60)

    def test_cumulative_reveal_compares_subtask_improvements(self):
        self.cumulative()
        with patch('django.utils.timezone.now', return_value=self.base):
            self.finish(self.new(), 40, vector=(40, 0))
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40, vector=(0, 40))
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        self.assertEqual(StreakProgress.objects.get().best_points, 80)
        self.drain(opened)
        award = StreakContribution.objects.get(submission_id=sub.pk)
        self.assertEqual((award.previous_points, award.points), (40, 80))
        self.assertEqual(award.day, streak_day(opened))

    def test_many_submissions_and_problems_award_only_one_reveal_day(self):
        other = create_problem(code='reveal-other', is_public=True, partial=True, points=100)
        attempt = self.start_offline([self.problem, other])
        self.offline_submission(attempt, 40)
        lower = self.offline_submission(attempt, 20)
        self.offline_submission(attempt, 30, problem=other)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        self.drain(opened)
        self.assertEqual(self.days(), [streak_day(opened)])
        self.assertEqual(StreakContribution.objects.count(), 2)
        self.assertFalse(StreakContribution.objects.filter(submission_id=lower.pk).exists())

    def test_midnight_worker_delay_uses_vietnam_reveal_day(self):
        self.base = self.base.replace(hour=16, minute=40)
        self.profile.timezone = 'Pacific/Kiritimati'
        self.profile.save(update_fields=['timezone'])
        attempt = self.start_offline()
        self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2, minutes=19)  # 23:59 Vietnam
        self.reveal(attempt, opened)
        worked = opened + timedelta(minutes=2)
        self.drain(worked)
        self.assertEqual(self.days(), [streak_day(opened)])
        self.assertEqual(streak_day(worked), streak_day(opened) + timedelta(days=1))
        self.assertEqual(public_summary(self.profile, now=worked)['current'], 1)
        self.assertFalse(public_summary(self.profile, now=worked)['kept_today'])

    def test_late_initial_result_uses_reveal_day_and_current_progress(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        with patch('django.utils.timezone.now', return_value=self.base + timedelta(days=3)):
            self.earn(60, 3)
        sub.refresh_from_db()
        finished = self.base + timedelta(days=4)
        with patch('django.utils.timezone.now', return_value=finished):
            self.finish(sub, 80)
        self.drain(finished)
        award = StreakContribution.objects.get(submission_id=sub.pk)
        self.assertEqual((award.previous_points, award.points), (60, 80))
        self.assertEqual(award.day, streak_day(opened))
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 2)

    def test_late_lower_result_does_not_earn_reveal_day(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        with patch('django.utils.timezone.now', return_value=opened + timedelta(minutes=1)):
            self.earn(60, 2)
            sub.refresh_from_db()
            self.finish(sub, 40)
        self.drain(opened + timedelta(minutes=2))
        self.assertFalse(StreakContribution.objects.filter(submission_id=sub.pk).exists())

    def test_stale_hidden_flag_at_completion_reads_committed_reveal(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        sub.status, sub.points, sub.result = 'D', 40, 'WA'
        Submission.objects.filter(pk=sub.pk).update(status='D', points=40, result='WA')
        self.assertTrue(sub.offline_hidden)
        with patch('django.utils.timezone.now', return_value=opened + timedelta(minutes=1)):
            record_completion(sub)
        self.drain(opened + timedelta(minutes=2))
        self.assertEqual(StreakContribution.objects.get(submission_id=sub.pk).day, streak_day(opened))

    def test_repeated_reveal_and_duplicate_worker_keep_first_date(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        again = self.reveal(attempt, opened + timedelta(days=1))
        self.assertEqual(again.revealed_at, opened)
        self.drain(opened + timedelta(days=1))
        self.drain(opened + timedelta(days=1))
        self.assertEqual(StreakContribution.objects.filter(submission_id=sub.pk).count(), 1)
        self.assertEqual(self.days(), [streak_day(opened)])

    def test_reveal_failure_rolls_back_visibility_and_progress(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        with patch('judge.utils.streaks._update_progress', side_effect=RuntimeError('rollback')):
            with self.assertRaises(RuntimeError):
                self.reveal(attempt, opened)
        attempt.refresh_from_db()
        sub.refresh_from_db()
        self.assertIsNone(attempt.revealed_at)
        self.assertTrue(sub.offline_hidden)
        self.assertEqual(StreakProgress.objects.get().best_points, 0)
        self.assertFalse(StreakEvent.objects.get().applied)
        self.reveal(attempt, opened)
        self.drain(opened)
        self.assertEqual(self.days(), [streak_day(opened)])

    def test_pending_legacy_events_adopt_reveal_day_without_history_rebuild(self):
        from judge.models import ExamOfflineAttempt
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        ExamOfflineAttempt.objects.filter(pk=attempt.pk).update(revealed_at=opened, ended_at=opened)
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=False)
        self.drain(opened)
        self.assertEqual(self.days(), [streak_day(opened)])
        self.assertEqual(StreakProgress.objects.get().best_points, 40)

    def test_legacy_applied_pending_event_changes_day_without_reapplying_score(self):
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        event = StreakEvent.objects.get()
        event.snapshot.pop('offline_revealed_at')
        event.day = streak_day(sub.date)
        event.save(update_fields=['snapshot', 'day'])
        self.drain(opened)
        award = StreakContribution.objects.get(submission_id=sub.pk)
        self.assertEqual((award.previous_points, award.points), (0, 40))
        self.assertEqual(award.day, streak_day(opened))
        self.assertEqual(StreakProgress.objects.get().best_points, 40)

    def test_already_earned_legacy_days_stay_unchanged(self):
        from judge.models import ExamOfflineAttempt
        attempt = self.start_offline()
        sub = self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        original_day = streak_day(sub.date)
        StreakEvent.objects.update(applied=True, pending=False)
        StreakContribution.objects.create(user=self.profile, submission_id=sub.pk, problem_id=self.problem.pk,
            day=original_day, previous_points=0, points=40)
        StreakDay.objects.create(user=self.profile, day=original_day)
        StreakRun.objects.create(user=self.profile, start=original_day, end=original_day, length=1)
        StreakSummary.objects.create(user=self.profile, timezone=STREAK_TIMEZONE_NAME,
            last_day=original_day, current_length=1, longest=1)
        ExamOfflineAttempt.objects.filter(pk=attempt.pk).update(revealed_at=opened, ended_at=opened)
        attempt.refresh_from_db()
        self.assertEqual(publish_offline_streaks(attempt), 0)
        self.drain(opened)
        self.assertEqual(self.days(), [original_day])
        self.assertEqual(StreakContribution.objects.get().day, original_day)

    def test_unlisted_user_still_earns_offline_reveal_day(self):
        self.profile.is_unlisted = True
        self.profile.save(update_fields=['is_unlisted'])
        attempt = self.start_offline()
        self.offline_submission(attempt, 40)
        opened = self.base + timedelta(days=2)
        self.reveal(attempt, opened)
        self.drain(opened)
        self.assertEqual(self.days(), [streak_day(opened)])
        self.assertIsNone(public_summary(self.profile))


@override_settings(STREAKS_ENABLED=True)
class StreakConcurrencyTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def offline_fixture(self, completed=True):
        from judge.utils.exam_offline import start_attempt, finish_attempt, link_submission
        profile = create_user(username='streak-reveal-race').profile
        problem = create_problem(code='streak-reveal-race', is_public=True, partial=True, points=100)
        exam = ExamTag.objects.create(slug='streak-reveal-race', name='Reveal race', duration_minutes=30,
                                      virtual_offline_enabled=True)
        problem.exam_tags.add(exam)
        StreakEpoch.objects.create(pk=1, submission_id=0, started_at=timezone.now() - timedelta(days=30))
        attempt = start_attempt(profile.pk, exam.pk)
        sub = Submission.objects.create(user=profile, problem=problem, language=Language.get_python3(),
                                        offline_hidden=True)
        link_submission(attempt.problems.get(), sub)
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=problem.pk)
            sub.batch_scoring = {'dispatch_id': str(uuid.uuid4()),
                                 'streak': prepare_streak_run(sub, problem, initial=True),
                                 'pretests': [], 'full': None, 'default': 'min'}
            sub.status, sub.points, sub.result = ('D', 40, 'WA') if completed else ('G', None, None)
            sub.save()
            if completed:
                record_completion(sub)
        finish_attempt(profile.pk, attempt.pk, early=True)
        return profile, problem, attempt, sub

    def race(self, *operations):
        barrier = Barrier(len(operations))

        def run(operation):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return operation()
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=len(operations)) as pool:
            futures = [pool.submit(run, operation) for operation in operations]
            return [future.result(timeout=25) for future in futures]

    @patch('judge.tasks.exam_offline.refresh_offline_progress.delay')
    def test_reveal_and_practice_completion_serialize_improvement_decisions(self, enqueue):
        from judge.utils.exam_offline import reveal_attempt
        profile, problem, attempt, hidden = self.offline_fixture()
        practice = Submission.objects.create(user=profile, problem=problem, language=Language.get_python3())
        with transaction.atomic():
            Problem.objects.select_for_update().get(pk=problem.pk)
            practice.batch_scoring = {'streak': prepare_streak_run(practice, problem, initial=True)}
            practice.save(update_fields=['batch_scoring'])

        def complete():
            with transaction.atomic():
                locked_problem = Problem.objects.select_for_update().get(pk=problem.pk)
                sub = Submission.objects.select_for_update().get(pk=practice.pk)
                sub.problem = locked_problem
                sub.status, sub.points = 'D', 60
                sub.save()
                record_completion(sub)

        self.race(lambda: reveal_attempt(profile.pk, attempt.pk), complete)
        event = StreakEvent.objects.get(submission_id=hidden.pk)
        self.assertIn((event.previous_points, event.points), [(0, 40), (60, 60)])
        self.assertEqual(StreakProgress.objects.get().best_points, 60)
        drain_streak_work.run()
        self.assertEqual(StreakContribution.objects.filter(submission_id=hidden.pk).exists(),
                         event.points > event.previous_points)
        self.assertEqual(StreakDay.objects.count(), 1)

    @patch('judge.tasks.exam_offline.refresh_offline_progress.delay')
    def test_two_reveals_and_workers_award_exactly_once(self, enqueue):
        from judge.utils.exam_offline import reveal_attempt
        profile, problem, attempt, sub = self.offline_fixture()
        self.race(lambda: reveal_attempt(profile.pk, attempt.pk),
                  lambda: reveal_attempt(profile.pk, attempt.pk), drain_streak_work.run)
        event = StreakEvent.objects.get(submission_id=sub.pk)
        self.race(lambda: process_event(event.pk), lambda: process_event(event.pk))
        self.assertEqual(StreakContribution.objects.count(), 1)
        self.assertEqual(StreakDay.objects.count(), 1)
        self.assertEqual(StreakProgress.objects.get().best_points, 40)

    @patch('judge.tasks.exam_offline.refresh_offline_progress.delay')
    @patch('judge.bridge.judge_handler.event.post')
    @patch('judge.bridge.judge_handler.finished_submission')
    def test_actual_bridge_completion_can_finish_while_reveal_holds_attempt(self, finished, post, enqueue):
        from judge.bridge.judge_handler import JudgeHandler
        from judge.utils.batch_scoring import finalize_batch_results
        from judge.utils.exam_offline import reveal_attempt
        from judge.models import ExamOfflineAttempt
        profile, problem, attempt, sub = self.offline_fixture(completed=False)
        SubmissionTestCase.objects.create(submission=sub, case=1, batch=None,
                                          status='WA', points=40, total=100, time=0, memory=0)
        callback_locked, reveal_locked = Event(), Event()

        def finalize(*args, **kwargs):
            callback_locked.set()
            if not reveal_locked.wait(timeout=10):
                raise RuntimeError('Reveal did not acquire its attempt lock')
            return finalize_batch_results(*args, **kwargs)

        def complete():
            handler = object.__new__(JudgeHandler)
            handler.name, handler._working = 'streak-reveal-race', sub.pk
            handler._dispatch_id = sub.batch_scoring['dispatch_id']
            handler._free_self = Mock()
            handler._make_json_log = Mock(return_value={})
            handler._post_update_submission = Mock()
            handler._update_scoring_stats = Mock()
            handler.on_grading_end({'submission-id': sub.pk})

        def reveal():
            if not callback_locked.wait(timeout=10):
                raise RuntimeError('Callback did not reach finalization')
            with transaction.atomic():
                ExamOfflineAttempt.objects.select_for_update().get(pk=attempt.pk)
                reveal_locked.set()
                reveal_attempt(profile.pk, attempt.pk)

        with patch('judge.bridge.judge_handler.finalize_batch_results', side_effect=finalize):
            self.race(complete, reveal)
        drain_streak_work.run()
        attempt.refresh_from_db()
        award = StreakContribution.objects.get(submission_id=sub.pk)
        self.assertEqual(award.day, streak_day(attempt.revealed_at))
        self.assertEqual(StreakProgress.objects.get().best_points, 40)

    def test_two_workers_different_problems_same_user(self):
        profile = create_user(username='streak_concurrent').profile
        ids = []
        for i in range(2):
            progress = StreakProgress.objects.create(user=profile, problem_id=i + 1, generation='test')
            event = StreakEvent.objects.create(progress=progress, submission_id=i + 1,
                day=timezone.now().date(), previous_points=0, points=10, available_at=timezone.now())
            ids.append(event.pk)
        barrier = Barrier(2)

        def work(pk):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                process_event(pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(work, pk) for pk in ids]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(StreakContribution.objects.count(), 2)
        self.assertEqual(StreakDay.objects.count(), 1)
        self.assertEqual(StreakSummary.objects.get(user=profile).longest, 1)

    def test_concurrent_completion_uses_one_monotone_highwater(self):
        profile = create_user(username='streak_producers').profile
        problem = create_problem(code='streak_producer', is_public=True, partial=True, points=100)
        StreakEpoch.objects.create(pk=1, submission_id=0, started_at=timezone.now() - timedelta(days=1))
        subs = []
        for points in (20, 30):
            sub = Submission.objects.create(user=profile, problem=problem, language=Language.get_python3())
            with transaction.atomic():
                Problem.objects.select_for_update().get(pk=problem.pk)
                meta = prepare_streak_run(sub, problem, initial=True)
            sub.batch_scoring = {'streak': meta}
            sub.status, sub.points = 'D', points
            sub.save()
            subs.append(sub.pk)
        barrier = Barrier(2)

        def finish(pk):
            close_old_connections()
            try:
                sub = Submission.objects.select_related('problem', 'user').get(pk=pk)
                barrier.wait(timeout=10)
                record_completion(sub)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(finish, pk) for pk in subs]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(StreakProgress.objects.get().best_points, 30)
        self.assertEqual(StreakEvent.objects.count(), 2)
        chain = [(event.previous_points, event.points) for event in StreakEvent.objects.order_by('id')]
        self.assertIn(chain, [[(0, 20), (20, 30)], [(0, 30), (30, 30)]])
