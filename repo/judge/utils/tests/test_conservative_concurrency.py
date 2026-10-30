from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase

from judge.models import Contest, ContestSubmission, Language, ProblemTestCase, Submission, SubmissionTestCase
from judge.models.tests.util import (create_contest, create_contest_participation, create_contest_problem,
                                     create_problem, create_user)
from judge.utils.batch_scoring import scoring_snapshot
from judge.utils.conservative import calculate, refresh_one, snapshot_problem, stop


class ConservativeConcurrencyTests(TransactionTestCase):
    fixtures = ['language_all.json']

    def test_disable_during_calculation_prevents_late_publication(self):
        user = create_user(username='conservative_concurrency')
        problem = create_problem(code='cons_concurrency', partial=True, points=100)
        for order, kind in enumerate(('S', 'C', 'C', 'E')):
            ProblemTestCase.objects.create(dataset=problem, order=order, type=kind,
                points=100 if kind == 'S' else None, is_pretest=False)
        contest = create_contest(key='cons_concurrency')
        cp = create_contest_problem(contest=contest, problem=problem, partial=True)
        part = create_contest_participation(contest=contest, user=user.profile)
        sub = Submission.objects.create(problem=problem, user=user.profile, language=Language.get_python3(),
            status='D', result='AC', case_points=100, case_total=100, batch_scoring=scoring_snapshot(problem))
        cs = ContestSubmission.objects.create(submission=sub, participation=part, problem=cp)
        for number in (1, 2):
            SubmissionTestCase.objects.create(submission=sub, case=number, batch=1, points=50, total=50, status='AC')
        Contest.objects.filter(pk=contest.pk).update(conservative_state='ON', conservative_revision=1,
            conservative_config={str(cp.pk): snapshot_problem(problem)})
        entered, release = Event(), Event()

        def paused(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError('Timed out waiting for disable')
            return calculate(*args)

        def worker():
            close_old_connections()
            try:
                return refresh_one(cs.pk, 1)
            finally:
                connection.close()

        with patch('judge.utils.conservative.calculate', side_effect=paused), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(worker)
            try:
                self.assertTrue(entered.wait(5))
                stop(contest.pk)
            finally:
                release.set()
            result.result(timeout=5)
        contest.refresh_from_db()
        cs.refresh_from_db()
        self.assertEqual(contest.conservative_state, 'OFF')
        self.assertEqual(cs.conservative_data, {})
        self.assertIsNone(cs.conservative_points)
