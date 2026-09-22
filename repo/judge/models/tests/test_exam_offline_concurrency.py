"""Real independent connections; run against MariaDB/PostgreSQL, not SQLite."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from judge.models import ExamOfflineAttempt, ExamTag, Profile
from judge.models.tests.util import create_problem, create_user
from judge.utils.exam_offline import start_attempt, finish_attempt, locked_contest_transition


@skipUnlessDBFeature('has_select_for_update')
class OfflineRaceTests(TransactionTestCase):
    def setUp(self):
        self.user = create_user(username='offline-race')
        self.exam = ExamTag.objects.create(slug='offline-race', name='Race', duration_minutes=60,
                                         virtual_offline_enabled=True)
        create_problem(code='offlinerace', is_public=True).exam_tags.add(self.exam)
        self.enqueue = patch('judge.tasks.exam_offline.refresh_offline_progress.delay')
        self.enqueue.start()
        self.addCleanup(self.enqueue.stop)

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
            return list(pool.map(run, operations))

    def start(self):
        try:
            return start_attempt(self.user.profile.pk, self.exam.pk).id
        except ValidationError:
            return None

    def test_two_tabs_start_only_one_session(self):
        results = self.race(self.start, self.start)
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(ExamOfflineAttempt.objects.filter(active_user=self.user.profile).count(), 1)

    def test_finish_and_start_do_not_leave_two_active(self):
        attempt = start_attempt(self.user.profile.pk, self.exam.pk)
        self.race(lambda: finish_attempt(self.user.profile.pk, attempt.pk, early=True), self.start)
        self.assertLessEqual(ExamOfflineAttempt.objects.filter(active_user=self.user.profile).count(), 1)
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.ended_at)

    def test_contest_and_exam_start_share_lock(self):
        from types import SimpleNamespace
        from judge.models.tests.util import create_contest, create_contest_participation
        contest = create_contest(key='racecontest', is_visible=True)
        participation = create_contest_participation(contest=contest.key, user=self.user.username)

        @locked_contest_transition
        def join(view, request):
            request.profile.current_contest = participation
            request.profile.save(update_fields=['current_contest'])
            return True

        def enter_contest():
            request = SimpleNamespace(profile=Profile.objects.get(pk=self.user.profile.pk), user=self.user)
            return join(None, request)

        with patch('judge.utils.views.generic_message', return_value=False):
            self.race(enter_contest, self.start)
        profile = Profile.objects.get(pk=self.user.profile.pk)
        active = ExamOfflineAttempt.objects.filter(active_user=profile).exists()
        self.assertNotEqual(bool(profile.current_contest_id), active)

    def test_submission_and_finish_have_consistent_order(self):
        from judge.models import Language, Submission
        from judge.utils.exam_offline import admit_submission, link_submission
        attempt = start_attempt(self.user.profile.pk, self.exam.pk)
        problem = attempt.problems.get().problem
        language = Language.objects.create(key='CPPTEST', name='C++ test', short_name='C++', common_name='C++')

        def submit():
            try:
                with transaction.atomic():
                    profile = Profile.objects.select_for_update().get(pk=self.user.profile.pk)
                    row = admit_submission(profile, problem, attempt.id)
                    sub = Submission.objects.create(user=profile, problem=problem, language=language,
                                                    offline_hidden=True)
                    link_submission(row, sub)
                    return sub.id
            except ValidationError:
                return None

        results = self.race(submit, lambda: finish_attempt(self.user.profile.pk, attempt.pk, early=True))
        attempt.refresh_from_db()
        self.assertIsNotNone(attempt.ended_at)
        row = attempt.problems.get()
        self.assertEqual(row.final_submission_id, results[0])
        self.assertEqual(Submission.objects.filter(offline_hidden=True).count(), int(results[0] is not None))
