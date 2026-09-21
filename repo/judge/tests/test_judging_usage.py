from datetime import datetime
from io import StringIO
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

import pytz
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.db.models import Sum
from django.http import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from judge.bridge.judge_handler import JudgeHandler
from judge.models import ExternalProblem, ExternalSubmission, JudgingUsageDaily, Language, Submission, SubmissionTestCase
from judge.models.tests.util import create_organization, create_problem, create_user
from judge.utils.judging_usage import PendingJudgingUsage
from django.core.management.base import CommandError
from judge.views.judging_usage import judging_usage


@override_settings(JUDGING_USAGE_TIME_ZONE='Asia/Ho_Chi_Minh')
class JudgingUsageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_user('usage-user')
        cls.admin = create_user('usage-admin', is_superuser=True, is_staff=True)
        cls.org = create_organization('usage-org')
        cls.other_org = create_organization('other-org')
        cls.org.admins.add(cls.user.profile)
        cls.problem = create_problem('usage-problem', storage_owner_organization=cls.org)
        cls.language = Language.objects.create(key='USAGE', name='Usage', short_name='Usage', common_name='Usage')

    def submission(self, **kwargs):
        return Submission.objects.create(user=self.user.profile, problem=self.problem, language=self.language, **kwargs)

    def attempt(self, sub, seconds=1):
        usage = PendingJudgingUsage.for_submission(sub.pk)
        usage.begin()
        usage.record([{'position': 1, 'time': seconds}])
        usage.finish()
        return usage

    def test_no_database_writes_until_finish_and_duplicates_ignored(self):
        usage = PendingJudgingUsage.for_submission(self.submission().pk)
        with self.assertNumQueries(0):
            usage.begin()
            usage.record([{'position': 1, 'time': 2}, {'position': 2, 'time': 3}])
            usage.record([{'position': 2, 'time': 3}, {'position': 3, 'time': 4}])
            self.assertFalse(usage.begin())
        self.assertFalse(JudgingUsageDaily.objects.exists())
        usage.finish()
        with self.assertNumQueries(0):
            usage.finish()
        row = JudgingUsageDaily.objects.get()
        self.assertEqual((row.seconds, row.attempts), (9, 1))

    def test_rejudge_and_owner_snapshot(self):
        sub = self.submission()
        first = PendingJudgingUsage.for_submission(sub.pk)
        self.problem.storage_owner_organization = self.other_org
        self.problem.save(update_fields=['storage_owner_organization'])
        first.begin()
        first.record([{'position': 1, 'time': 10}])
        first.finish()
        sub.rejudged_date = datetime.now(pytz.UTC)
        sub.save(update_fields=['rejudged_date'])
        self.attempt(sub, 20)
        self.assertEqual(JudgingUsageDaily.objects.get(organization_key=self.org.pk).seconds, 10)
        other = JudgingUsageDaily.objects.get(organization_key=self.other_org.pk)
        self.assertEqual((other.seconds, other.attempts, other.rejudges), (20, 1, 1))

    def test_multiple_attempts_update_same_daily_row(self):
        self.attempt(self.submission(), 4)
        self.attempt(self.submission(), 6)
        row = JudgingUsageDaily.objects.get()
        self.assertEqual((row.seconds, row.attempts), (10, 2))
        self.assertIsNone(self.context(slug=self.org.slug)['page_obj'])

    def test_backfill_refuses_to_overwrite_and_excludes_external(self):
        self.submission(status='D', time=12)
        self.submission(status='CE')
        self.submission(status='QU')
        external = self.submission(status='D', time=50)
        ExternalSubmission.objects.create(submission=external, pcd_submission_id=uuid4())
        call_command('backfill_judging_usage', stdout=StringIO())
        row = JudgingUsageDaily.objects.get()
        self.assertEqual((row.seconds, row.attempts, row.rejudges), (12, 2, 0))
        with self.assertRaises(CommandError):
            call_command('backfill_judging_usage', stdout=StringIO())
        row.refresh_from_db()
        self.assertEqual(row.seconds, 12)

    @patch('judge.bridge.judge_handler.event.post')
    def test_compile_error_records_zero_seconds(self, post):
        sub = self.submission()
        handler = self.handler(sub)
        handler.on_submission_processing({'submission-id': sub.pk})
        handler.on_compile_error({'submission-id': sub.pk, 'log': 'compile error'})
        row = JudgingUsageDaily.objects.get()
        self.assertEqual((row.seconds, row.attempts), (0, 1))

    def request(self, params=None, slug=None, user=None):
        request = RequestFactory().get('/status/judging-usage/', params or {})
        request.user = user or self.admin
        request.profile = request.user.profile if request.user.is_authenticated else None
        return judging_usage(request, slug=slug)

    def context(self, params=None, slug=None):
        with patch('judge.views.judging_usage.render') as render:
            self.request(params, slug)
        return render.call_args.args[2]

    def test_only_superusers_can_access_either_scope(self):
        for user in (AnonymousUser(), self.user):
            for slug in (None, self.org.slug):
                with self.subTest(user=user, slug=slug), self.assertRaises(Http404):
                    self.request(user=user, slug=slug)

    def test_site_total_equals_orgs_plus_unowned_and_filters_dates(self):
        fixed = pytz.UTC.localize(datetime(2026, 8, 31, 17, 30))
        with patch('judge.utils.judging_usage.timezone.now', return_value=fixed):
            self.attempt(self.submission(), 3600)
            self.problem.storage_owner_organization = None
            self.problem.save(update_fields=['storage_owner_organization'])
            self.attempt(self.submission(), 1800)
        params = {'start': '2026-09-01', 'end': '2026-09-02'}
        site = self.context(params)
        org = self.context(params, self.org.slug)
        self.assertEqual(site['totals']['hours'], 1.5)
        self.assertEqual(org['totals']['hours'], 1)
        self.assertEqual(sum(row['seconds'] for row in site['page_obj']), site['totals']['seconds'])
        self.assertEqual(site['chart']['hours'], [1.5, 0])
        self.assertEqual(self.context({'start': '2026-08-31', 'end': '2026-08-31'})['totals']['seconds'], 0)
        monthly = self.context(dict(params, group='month'))
        self.assertEqual(monthly['chart']['hours'], [1.5])

    def test_bad_dates_and_long_ranges(self):
        for params in ({'start': 'bad'}, {'start': '2026-10-01', 'end': '2026-09-01'}, {'group': 'invalid'}):
            self.assertEqual(self.request(params).status_code, 400)
        self.assertEqual(self.context({'start': '2020-01-01', 'end': '2026-01-01'})['group'], 'month')

    def test_templates_render_for_both_scopes(self):
        self.attempt(self.submission(), 0.001)
        self.client.force_login(self.admin)
        for url in (reverse('status_judging_usage'), reverse('organization_judging_usage', args=[self.org.slug])):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'judging-chart')
            self.assertContains(response, '0.001 s')

    def handler(self, sub):
        handler = object.__new__(JudgeHandler)
        handler._working = sub.pk
        handler._pending_usage = None
        handler.name = 'test-judge'
        handler.judge = None
        handler.judge_address = None
        handler.client_address = ('127.0.0.1', 9999)
        handler.batch_id = None
        handler.in_batch = False
        handler.update_counter = {}
        handler._post_update_submission = Mock()
        handler.judges = Mock()
        handler._stop_ping = Event()
        handler._no_response_job = None
        handler._disconnected = Mock()
        return handler

    @patch('judge.bridge.judge_handler.event.post')
    def test_bridge_partial_usage_survives_disconnect(self, post):
        sub = self.submission()
        handler = self.handler(sub)
        handler.on_submission_processing({'submission-id': sub.pk})
        handler.on_grading_begin({'submission-id': sub.pk, 'pretested': False})
        handler.on_test_case({'submission-id': sub.pk, 'cases': [{
            'position': 1, 'time': 2.5, 'status': 0, 'memory': 100,
            'points': 1, 'total-points': 1, 'output': '',
        }]})
        handler.on_grading_begin({'submission-id': sub.pk, 'pretested': False})
        self.assertEqual(SubmissionTestCase.objects.filter(submission=sub).count(), 1)
        with self.assertLogs('judge.bridge', level='ERROR'):
            handler.on_disconnect()
        usage = JudgingUsageDaily.objects.get()
        self.assertEqual(usage.seconds, 2.5)
        self.assertEqual(usage.attempts, 1)

    @patch('judge.bridge.judge_handler.event.post')
    def test_terminal_accounting_precedes_next_dispatch(self, post):
        sub = self.submission()
        handler = self.handler(sub)
        handler.on_submission_processing({'submission-id': sub.pk})
        handler.on_grading_begin({'submission-id': sub.pk, 'pretested': False})
        handler._pending_usage.record([{'position': 1, 'time': 7}])
        def dispatch_next(*args):
            handler._pending_usage = None
            handler._working = sub.pk + 1
            return True
        handler.judges.on_judge_free.side_effect = dispatch_next
        handler.on_submission_terminated({'name': 'submission-terminated', 'submission-id': sub.pk})
        usage = JudgingUsageDaily.objects.get()
        self.assertEqual(usage.seconds, 7)
        self.assertEqual(usage.attempts, 1)
