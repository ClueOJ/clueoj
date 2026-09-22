"""Read-only load probe on the local dataset. Run via manage.py shell."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from django.db import connection, close_old_connections
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from judge.models import Profile, Submission
from judge.utils.exam_offline import active_attempt
from judge import event_poster

uid = Profile.objects.order_by('-id').values_list('id', flat=True).first()
sid = Submission.objects.order_by('-id').values_list('id', flat=True).first()
print(json.dumps({'submissions': Submission.objects.count(), 'profiles': Profile.objects.count()}), flush=True)

def measure(label, operation, workers, count):
    def worker(n):
        close_old_connections()
        times, errors = [], []
        try:
            for i in range(n):
                begin = time.perf_counter()
                try:
                    operation()
                except Exception as exc:
                    errors.append(type(exc).__name__ + ': ' + str(exc)[:120])
                times.append((time.perf_counter() - begin) * 1000)
        finally:
            connection.close()
        return times, errors
    begin = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(worker, [count // workers] * workers))
    elapsed = time.perf_counter() - begin
    times = sorted(t for r, _ in results for t in r)
    errors = [e for _, es in results for e in es]
    print(json.dumps(dict(label=label, workers=workers, operations=len(times),
        rps=round(len(times)/elapsed, 1), p50_ms=round(times[len(times)//2],2),
        p95_ms=round(times[int(len(times)*.95)],2), max_ms=round(max(times),2),
        errors=len(errors), examples=errors[:2])), flush=True)

ops = {
    'active_attempt_lookup': lambda: active_attempt(uid),
    'realtime_visibility_check': lambda: event_poster.post('sub_' + Submission.get_id_secret(sid), {'type':'grading-end'}),
    'submission_page_query_50': lambda: list(Submission.objects.order_by('-id').select_related('problem','user','language')[:50]),
    'visible_submission_page_query_50': lambda: list(Submission.visible.order_by('-id').select_related('problem','user','language')[:50]),
}
with patch('judge.event_poster._transport_post', return_value=0):
    for label, op in ops.items():
        with CaptureQueriesContext(connection) as queries:
            op()
        print(json.dumps({'label':label, 'queries_per_op':len(queries)}), flush=True)
        for workers in (1,8,24):
            measure(label, op, workers, 240)

with override_settings(ALLOWED_HOSTS=['testserver'], VNOJ_IGNORED_ORGANIZATION_SUBDOMAINS=['testserver']):
    def page():
        response = Client().get('/submissions/')
        if response.status_code != 200:
            raise RuntimeError('HTTP %s' % response.status_code)
    page()
    with CaptureQueriesContext(connection) as queries:
        page()
    print(json.dumps({'label':'anonymous_submissions_django', 'queries_per_op':len(queries)}), flush=True)
    for workers in (1,8,24):
        measure('anonymous_submissions_django', page, workers, 48)

for label, qs in [('active_attempt', __import__('judge.models',fromlist=['ExamOfflineAttempt']).ExamOfflineAttempt.objects.filter(active_user_id=uid)),
                  ('visible_latest', Submission.visible.order_by('-id')[:50])]:
    print(label + ' EXPLAIN ' + qs.explain(), flush=True)
