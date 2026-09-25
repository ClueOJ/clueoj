from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from judge.models import (Language, Submission, StreakContribution, StreakDay, StreakRun,
                          StreakProblemState, StreakSummary, StreakRebuildRequest)
from judge.models.tests.util import CommonDataMixin, create_problem
from judge.utils.streaks import process_pair, queue_pair, expand_request, public_summary


@override_settings(STREAKS_ENABLED=True, ALLOWED_HOSTS=['localhost', 'testserver'])
class StreakTests(CommonDataMixin, TestCase):
    def setUp(self):
        self.client.defaults['HTTP_HOST'] = 'localhost'
        self.profile = self.users['normal'].profile
        self.profile.timezone = 'Asia/Ho_Chi_Minh'
        self.profile.save(update_fields=['timezone'])
        self.problem = create_problem(code='streak_a', is_public=True, partial=True, points=100)
        self.language = Language.get_python3()
        self.base = datetime(2026, 9, 1, 16, 59, tzinfo=pytz.UTC)

    def submit(self, score, day=0, problem=None, **kwargs):
        sub = Submission.objects.create(user=self.profile, problem=problem or self.problem,
                                        language=self.language, status=kwargs.pop('status', 'D'),
                                        result=kwargs.pop('result', 'WA'), points=score,
                                        case_points=score, case_total=100, **kwargs)
        sub.date = self.base + timedelta(days=day)
        Submission.objects.filter(pk=sub.pk).update(date=sub.date)
        if not sub.offline_hidden:
            queue_pair(sub.user_id, sub.problem_id, sub)
        return sub

    def drain(self):
        for sid in list(StreakProblemState.objects.filter(pending=True).values_list('id', flat=True)):
            process_pair(sid)

    def days(self):
        return list(StreakDay.objects.filter(user=self.profile).order_by('day').values_list('day', flat=True))

    def test_only_new_personal_best_and_no_repeat_ac(self):
        for day, score in enumerate([20, 0, 20, 30, 100, 0, 100]):
            self.submit(score, day, result='AC' if score == 100 else 'WA')
            self.drain()
        self.assertEqual([d.day for d in self.days()], [1, 4, 5])
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 2)

    def test_same_day_multiple_problems_is_one_day(self):
        self.submit(20)
        self.submit(30)
        self.submit(50, problem=create_problem(code='streak_b', is_public=True))
        self.drain()
        self.assertEqual(len(self.days()), 1)
        self.assertEqual(StreakContribution.objects.count(), 3)

    def test_private_and_any_org_are_excluded(self):
        private = create_problem(code='private_streak', is_public=False)
        org = create_problem(code='org_streak', is_public=True, organizations=('open',))
        flag = create_problem(code='flag_streak', is_public=True, is_organization_private=True)
        for problem in [private, org, flag]:
            self.submit(100, problem=problem)
        self.drain()
        self.assertEqual(self.days(), [])

    def test_offline_reveal_counts_original_date(self):
        sub = self.submit(50, offline_hidden=True)
        queue_pair(sub.user_id, sub.problem_id)
        self.drain()
        self.assertEqual(self.days(), [])
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=False)
        queue_pair(sub.user_id, sub.problem_id)
        self.drain()
        self.assertEqual(self.days(), [self.base.astimezone(pytz.timezone(self.profile.timezone)).date()])

    def test_late_earlier_result_repairs_later_high_water_mark(self):
        earlier = self.submit(80, status='G')
        self.submit(30, 1)
        self.drain()
        self.assertEqual([d.day for d in self.days()], [2])
        earlier.status = 'D'
        earlier.save(update_fields=['status'])
        self.drain()
        self.assertEqual([d.day for d in self.days()], [1])

    def test_retry_and_rebuild_are_idempotent(self):
        self.submit(20)
        self.submit(40, 1)
        self.drain()
        before = list(StreakContribution.objects.values_list('submission_id', 'day', 'points'))
        queue_pair(self.profile.pk, self.problem.pk)
        self.drain()
        self.assertEqual(before, list(StreakContribution.objects.values_list('submission_id', 'day', 'points')))
        self.assertEqual(StreakRun.objects.get().length, 2)

    def test_rejudge_retracts_day_but_preserves_other_problem(self):
        a = self.submit(50)
        self.submit(40, problem=create_problem(code='other_streak', is_public=True))
        self.drain()
        a.points = 0
        a.save(update_fields=['points'])
        self.drain()
        self.assertEqual(len(self.days()), 1)
        self.assertEqual(StreakContribution.objects.count(), 1)

    def test_midnight_uses_owner_timezone(self):
        self.submit(20)
        self.base += timedelta(minutes=2)
        self.submit(30)
        self.drain()
        self.assertEqual([d.day for d in self.days()], [1, 2])

    def test_dst_fall_back_is_one_local_day(self):
        self.profile.timezone = 'America/New_York'
        self.profile.save(update_fields=['timezone'])
        self.base = datetime(2026, 11, 1, 5, 30, tzinfo=pytz.UTC)
        self.submit(20)
        self.base += timedelta(hours=1)
        self.submit(30)
        self.drain()
        self.assertEqual(len(self.days()), 1)

    def test_yesterday_stays_active_until_today_is_missed(self):
        self.submit(20)
        self.drain()
        self.assertEqual(public_summary(self.profile, self.base + timedelta(days=1))['current'], 1)
        self.assertEqual(public_summary(self.profile, self.base + timedelta(days=2))['current'], 0)
        self.assertEqual(public_summary(self.profile, self.base + timedelta(days=2))['longest'], 1)

    def test_pretest_and_zero_score_never_count(self):
        self.submit(100, is_pretested=True, result='AC')
        self.submit(0, 1)
        self.drain()
        self.assertEqual(self.days(), [])

    def test_org_change_enqueues_repair_and_removes_history(self):
        self.submit(20)
        self.drain()
        self.problem.organizations.add(self.organizations['open'])
        request = StreakRebuildRequest.objects.get(kind='problem', object_id=self.problem.pk)
        expand_request(request.pk)
        self.drain()
        self.assertEqual(self.days(), [])

    def test_public_read_does_not_query_submissions(self):
        self.submit(20)
        self.drain()
        with CaptureQueriesContext(connection) as queries:
            public_summary(self.profile)
        self.assertEqual(len(queries), 1)
        self.assertNotIn('judge_submission', queries[0]['sql'])

    def test_incremental_query_only_reads_suffix(self):
        self.submit(20)
        self.drain()
        self.submit(30, 1)
        with CaptureQueriesContext(connection) as queries:
            self.drain()
        sql = [q['sql'] for q in queries if 'FROM `judge_submission`' in q['sql']]
        self.assertEqual(len(sql), 1)
        self.assertIn('`date` >', sql[0])

    @patch('statici18n.templatetags.statici18n.staticfiles_storage.open')
    def test_private_page_checks_owner_and_renders(self, static_open):
        static_open.return_value.read.return_value = b''
        self.submit(20)
        self.drain()
        url = reverse('user_streaks', args=['normal'])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.users['superuser'])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.users['normal'])
        response = self.client.get(url, {'year': 2026, 'day': '2026-09-01'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(response, '20.000')

    def test_problem_deletion_reconciles_days(self):
        self.submit(20)
        self.drain()
        with patch('judge.signals.sync_exam_progress_for_user_problem.delay'):
            self.problem.delete()
        self.drain()
        self.assertEqual(self.days(), [])

    def test_backfill_is_dry_run_unless_explicit(self):
        from django.core.management import call_command
        from io import StringIO
        StreakRebuildRequest.objects.all().delete()
        output = StringIO()
        call_command('backfill_streaks', user=self.profile.pk, stdout=output)
        self.assertFalse(StreakRebuildRequest.objects.exists())
        call_command('backfill_streaks', user=self.profile.pk, apply=True, stdout=output)
        self.assertTrue(StreakRebuildRequest.objects.filter(kind='user', object_id=self.profile.pk).exists())

    def test_scope_batches_and_repeated_metadata_edits(self):
        self.submit(20)
        self.submit(40, problem=create_problem(code='scope_second', is_public=True))
        from judge.utils.streaks import queue_scope
        queue_scope('user', self.profile.pk)
        request = StreakRebuildRequest.objects.get(kind='user', object_id=self.profile.pk)
        expand_request(request.pk, batch_size=1)
        request.refresh_from_db()
        self.assertGreater(request.cursor, 0)
        queue_scope('user', self.profile.pk)
        request.refresh_from_db()
        self.assertEqual(request.cursor, 0)
        for _ in range(3):
            expand_request(request.pk, batch_size=1)
        self.assertFalse(StreakRebuildRequest.objects.filter(pk=request.pk).exists())
        self.drain()
        self.assertEqual(len(self.days()), 1)

    def test_terminal_bulk_error_retracts_previous_credit(self):
        from judge.utils.streaks import record_terminal_update
        sub = self.submit(50)
        self.drain()
        record_terminal_update(sub.pk, status='CE', result='CE', points=0)
        self.drain()
        self.assertEqual(self.days(), [])

    def test_failure_rolls_back_and_retains_durable_work(self):
        self.submit(50)
        sid = StreakProblemState.objects.get(user=self.profile, problem_id=self.problem.pk).pk
        with patch('judge.utils.streaks._refresh_days_and_runs', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                process_pair(sid)
        self.assertTrue(StreakProblemState.objects.get(pk=sid).pending)
        self.assertFalse(StreakContribution.objects.exists())
        process_pair(sid)
        self.assertEqual(len(self.days()), 1)

    def test_timezone_change_rebuilds_history(self):
        self.submit(50)
        self.drain()
        self.profile.timezone = 'Asia/Tokyo'
        self.profile.save(update_fields=['timezone'])
        request = StreakRebuildRequest.objects.get(kind='user', object_id=self.profile.pk)
        expand_request(request.pk)
        self.drain()
        self.assertEqual([d.day for d in self.days()], [2])

    def test_hidden_evidence_never_leaks_from_stale_contribution(self):
        sub = self.submit(50)
        self.drain()
        Submission.objects.filter(pk=sub.pk).update(offline_hidden=True)
        from django.test import RequestFactory
        from judge.views.streaks import UserStreakPage
        request = RequestFactory().get('/', {'day': '2026-09-01'})
        request.user = self.users['normal']
        request.profile = self.profile
        view = UserStreakPage()
        view.request = request
        view.object = self.profile
        view.hide_solved = False
        self.assertEqual(view.get_context_data()['contributions'], [])

    def test_run_repair_preserves_unrelated_runs(self):
        for day in (0, 1):
            self.submit(20 + day, day)
        far = create_problem(code='streak_far', is_public=True)
        for day in (10, 11):
            self.submit(20 + day, day, problem=far)
        self.drain()
        runs = StreakRun.objects.filter(user=self.profile).order_by('start')
        self.assertEqual([(r.start.day, r.end.day, r.length) for r in runs], [(1, 2, 2), (11, 12, 2)])
        untouched_pk = runs[0].pk
        late = Submission.objects.filter(problem=far).order_by('-id').first()
        late.points = 0
        late.save(update_fields=['points'])
        self.drain()
        runs = StreakRun.objects.filter(user=self.profile).order_by('start')
        self.assertEqual([(r.start.day, r.end.day, r.length) for r in runs], [(1, 2, 2), (11, 11, 1)])
        self.assertEqual(runs[0].pk, untouched_pk)
        self.assertEqual(StreakSummary.objects.get(user=self.profile).longest, 2)

    def test_public_summary_map_is_one_query_for_many_profiles(self):
        from judge.models.tests.util import create_user
        from judge.utils.streaks import public_summary_map
        others = [create_user(username='streak_map%d' % i).profile for i in range(3)]
        self.submit(20)
        self.drain()
        with CaptureQueriesContext(connection) as queries:
            summaries = public_summary_map([self.profile] + others, now=self.base + timedelta(hours=1))
        self.assertEqual(len(queries), 1)
        self.assertEqual(summaries[self.profile.pk]['current'], 1)
        self.assertEqual(summaries[others[0].pk]['current'], 0)
        self.assertEqual(summaries[others[0].pk]['tier'], 'muted')

    @patch('statici18n.templatetags.statici18n.staticfiles_storage.open')
    def test_submission_list_shows_owner_streak_badge(self, static_open):
        from django.utils import timezone as dj_timezone
        static_open.return_value.read.return_value = b''
        sub = self.submit(20)
        Submission.objects.filter(pk=sub.pk).update(date=dj_timezone.now())
        queue_pair(sub.user_id, sub.problem_id)
        self.drain()
        response = self.client.get(reverse('all_submissions'), HTTP_HOST='localhost')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'streak-badge')
        self.assertContains(response, 'streak-flame')





    def test_streak_page_renders_year_heatmap(self):
        self.client.force_login(self.users['normal'])
        response = self.client.get(reverse('user_streaks', args=[self.users['normal'].username]))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertContains(response, 'streak-heat-grid')
        self.assertEqual(body.count('streak-month-grid'), 12)
        self.assertNotIn('Tháng 1Tháng', body)
        self.assertContains(response, 'fa-fire')

    def test_calendar_rejects_future_and_out_of_year_day(self):
        self.client.force_login(self.users['normal'])
        url = reverse('user_streaks', args=[self.users['normal'].username])
        for day in ['2025-09-01', '9999-12-31', 'invalid']:
            response = self.client.get(url, {'year': 2026, 'day': day})
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, 'id="streak-day"')

    def test_leaderboard_sorts_live_current_and_longest(self):
        from django.utils import timezone as dj_timezone
        from judge.models.tests.util import create_user
        other = create_user(username='streak_board').profile
        other.timezone = 'Asia/Ho_Chi_Minh'
        other.is_unlisted = False
        other.save(update_fields=['timezone', 'is_unlisted'])
        self.profile.is_unlisted = False
        self.profile.save(update_fields=['is_unlisted'])
        local = dj_timezone.now().astimezone(pytz.timezone('Asia/Ho_Chi_Minh')).date()
        StreakSummary.objects.create(user=self.profile, timezone='Asia/Ho_Chi_Minh',
                                     last_day=local, current_length=3, longest=9)
        StreakSummary.objects.create(user=other, timezone='Asia/Ho_Chi_Minh',
                                     last_day=local - timedelta(days=10), current_length=40, longest=40)
        current = self.client.get(reverse('user_list'), {'order': '-streak_current'})
        longest = self.client.get(reverse('user_list'), {'order': '-streak_longest'})
        self.assertEqual(current.status_code, 200)
        current_html, longest_html = current.content.decode(), longest.content.decode()
        mine, theirs = 'id="user-%s"' % self.profile.user.username, 'id="user-streak_board"'
        self.assertLess(current_html.find(mine), current_html.find(theirs))
        self.assertLess(longest_html.find(theirs), longest_html.find(mine))
        self.assertContains(current, 'Current streak')
        self.assertContains(longest, 'Record')

@override_settings(STREAKS_ENABLED=True)
class StreakConcurrencyTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def test_two_workers_same_user_different_problems_preserve_both(self):
        from judge.models.tests.util import create_user
        from threading import Barrier
        profile = create_user(username='streak_concurrent').profile
        for code in ['concurrent_a', 'concurrent_b']:
            problem = create_problem(code=code, is_public=True)
            Submission.objects.create(user=profile, problem=problem, language=Language.get_python3(),
                                      status='D', result='WA', points=1, case_points=1, case_total=10)
        ids = list(StreakProblemState.objects.filter(user=profile).values_list('id', flat=True))
        barrier = Barrier(2)

        def work(pk):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                process_pair(pk)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(work, pk) for pk in ids]
            for future in futures:
                future.result(timeout=20)
        self.assertEqual(StreakContribution.objects.filter(user=profile).count(), 2)
        self.assertEqual(StreakDay.objects.filter(user=profile).count(), 1)
        self.assertEqual(StreakSummary.objects.get(user=profile).longest, 1)

    def test_event_arriving_during_worker_is_not_lost(self):
        from judge.models.tests.util import create_user
        from threading import Event
        from judge.utils import streaks
        profile = create_user(username='streak_race').profile
        problem = create_problem(code='race_problem', is_public=True)
        Submission.objects.create(user=profile, problem=problem, language=Language.get_python3(),
                                  status='D', result='WA', points=1, case_points=1, case_total=10)
        state_id = StreakProblemState.objects.get(user=profile, problem_id=problem.pk).pk
        inside_worker, producer_started, release_worker = Event(), Event(), Event()
        original = streaks._refresh_days_and_runs

        def pause_worker(*args):
            inside_worker.set()
            if not release_worker.wait(10):
                raise RuntimeError('Worker barrier timeout')
            return original(*args)

        def consume():
            close_old_connections()
            try:
                process_pair(state_id)
            finally:
                close_old_connections()

        def produce():
            close_old_connections()
            try:
                producer_started.set()
                Submission.objects.create(user_id=profile.pk, problem_id=problem.pk,
                                          language_id=Language.get_python3().pk, status='D', result='WA',
                                          points=2, case_points=2, case_total=10)
            finally:
                close_old_connections()

        with patch('judge.utils.streaks._refresh_days_and_runs', side_effect=pause_worker):
            with ThreadPoolExecutor(max_workers=2) as executor:
                consumer = executor.submit(consume)
                self.assertTrue(inside_worker.wait(10))
                producer = executor.submit(produce)
                self.assertTrue(producer_started.wait(10))
                release_worker.set()
                consumer.result(timeout=20)
                producer.result(timeout=20)
        self.assertTrue(StreakProblemState.objects.get(pk=state_id).pending)
        process_pair(state_id)
        self.assertEqual(StreakContribution.objects.filter(user=profile).count(), 2)
