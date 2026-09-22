"""Bounded lifecycle load test; writes only to Django's disposable test DB."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from django.db import connection, close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from judge.models import Submission, Language, ExamTag, ExamOfflineAttempt, ExamOfflineSubmission
from judge.models.tests.util import create_user, create_problem
from judge.utils.exam_offline import start_attempt, finish_attempt, reveal_attempt
from judge.tasks.exam_offline import refresh_offline_progress

@skipUnlessDBFeature('has_select_for_update')
class OfflineLoadTest(TransactionTestCase):
    def test_lifecycle_load(self):
        users=[create_user(username='loaduser%d'%i).profile.pk for i in range(24)]
        exam=ExamTag.objects.create(slug='loadexam',name='Load',duration_minutes=180,virtual_offline_enabled=True)
        problems=[create_problem(code='loadproblem%d'%i,is_public=True,partial=True) for i in range(3)]
        for problem in problems:
            problem.exam_tags.add(exam)
        lang=Language.objects.create(key='CPPLOAD',name='Load C++',short_name='C++',common_name='C++')
        Submission.objects.bulk_create([Submission(user_id=uid,problem=problem,language=lang,
            status='D',result='AC',points=1,case_points=1,case_total=1)
            for uid in users for problem in problems for _ in range(200)],batch_size=1000)
        stages={key:[] for key in ['start','finish','reveal']}
        def run(uid):
            close_old_connections()
            data=[]
            try:
                for _ in range(5):
                    t=time.perf_counter();attempt=start_attempt(uid,exam.pk);data.append(('start',(time.perf_counter()-t)*1000))
                    for row in attempt.problems.all():
                        sub=Submission.objects.create(user_id=uid,problem_id=row.problem_id,language=lang,
                            status='D',result='AC',points=1,case_points=1,case_total=1,offline_hidden=True)
                        ExamOfflineSubmission.objects.create(attempt_problem=row,submission=sub)
                        row.final_submission=sub
                        row.save(update_fields=['final_submission'])
                    t=time.perf_counter();finish_attempt(uid,attempt.pk,early=True);data.append(('finish',(time.perf_counter()-t)*1000))
                    t=time.perf_counter();reveal_attempt(uid,attempt.pk);data.append(('reveal',(time.perf_counter()-t)*1000))
                return data,attempt.pk
            finally:
                connection.close()
        with patch('judge.tasks.exam_offline.refresh_offline_progress.delay'):
            with ThreadPoolExecutor(max_workers=24) as pool:
                results=list(pool.map(run, users))
        for rows,_ in results:
            for key,elapsed in rows:stages[key].append(elapsed)
        for key,values in stages.items():
            values.sort();print(json.dumps(dict(stage=key,concurrency=24,operations=len(values),
                p50_ms=round(values[len(values)//2],2),p95_ms=round(values[int(len(values)*.95)],2),max_ms=round(max(values),2))),flush=True)
        self.assertFalse(Submission.objects.filter(offline_hidden=True).exists())
        self.assertEqual(ExamOfflineAttempt.objects.filter(active_user__isnull=False).count(),0)
        self.assertEqual(ExamOfflineAttempt.objects.filter(revealed_at__isnull=False).count(),120)
        # Run refresh synchronously with Celery submission mocked; avoid external broker side effects.
        times=[]
        with patch('celery.app.task.Task.delay'):
            for _,attempt_id in results:
                t=time.perf_counter()
                with CaptureQueriesContext(connection) as queries:
                    refresh_offline_progress.run(attempt_id)
                times.append((time.perf_counter()-t)*1000)
        times.sort()
        print(json.dumps(dict(stage='refresh',historical_submissions=14400,problems=3,
            users=24,queries_last=len(queries),p50_ms=round(times[12],2),p95_ms=round(times[22],2),max_ms=round(max(times),2))),flush=True)

        def refresh(attempt_id):
            close_old_connections()
            try:
                begin=time.perf_counter()
                refresh_offline_progress.run(attempt_id)
                return (time.perf_counter()-begin)*1000
            finally:
                connection.close()
        with patch('celery.app.task.Task.delay'):
            with ThreadPoolExecutor(max_workers=24) as pool:
                burst=sorted(pool.map(refresh,[attempt_id for _,attempt_id in results]))
        print(json.dumps(dict(stage='refresh_burst',concurrency=24,operations=24,
            p50_ms=round(burst[12],2),p95_ms=round(burst[22],2),max_ms=round(max(burst),2))),flush=True)
