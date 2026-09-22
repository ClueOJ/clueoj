# Offline practice load probe — 2026-09-22

Environment: local Docker MariaDB/Django on the developer machine. Read probes
used the local snapshot with 821,417 submissions and 17,067 profiles. Write
probes used Django's separate test database, 24 synthetic users, three shared
problems, 14,400 historical submissions and 360 attempt submissions.

## Results

| Operation | Concurrency | p95 |
|---|---:|---:|
| Active attempt lookup (1 query) | 24 | 14.43 ms |
| Realtime hidden flag check (1 query) | 24 | 11.51 ms |
| Start (120 attempts total) | 24 | 98.55 ms |
| Finish (120 attempts total) | 24 | 42.17 ms |
| Reveal (120 attempts total) | 24 | 113.93 ms |
| Refresh progress, sequential | 1 | 62.89 ms |
| Refresh progress burst, 24 tasks | 24 | 879.36 ms |

All probes completed without errors/deadlocks. Race/visibility/lifecycle regression
suite: 27 tests passed before adding the final concurrent refresh probe; the
updated load test also passed. All 120 attempts ended/revealed; no hidden
submission was left behind.

Submission page had an existing N+1 loading external submission/problem metadata.
Prefetching those two relations reduced warm page queries from 111 to 13.
At eight concurrent Django client requests, throughput increased from 19.2 to
37.8 requests/s and p95 fell from 545.76 to 306.48 ms. At 24, p95 fell from
1701.62 to 1232.98 ms. These are short in-process threaded measurements, not
production capacity promises; Python GIL/host scheduling contribute to latency.
The 50-row ORM query with/without the hidden flag filter took ~3 ms median
sequentially. EXPLAIN selected PRIMARY index scanning for latest visible rows;
active_user is a unique indexed lookup (the sampled user had no active attempt).

## Scope and limits

Read probes: 240 operations per operation type and concurrency (1/8/24), plus
48 Django page requests at each concurrency. No judge execution. Realtime
transport mocked: only visibility-check overhead measured, not network delivery.
Celery dispatch mocked; refresh task body ran synchronously on real test DB,
including 24 concurrent refresh tasks. No production writes or real submissions
were made. Caches are the local environment's caches. This is a bounded smoke
stress test, not a sustained soak, authenticated HTTP load-generator test,
CPU/RAM capacity study, or production topology simulation. Refresh was not run
against the full local snapshot, and a much larger per-user history can cost more.

Reproduce reads: `python3 manage.py shell -c 'exec(open("benchmarks/offline_read_load.py").read())'`.
Reproduce writes: `python3 manage.py test judge.models.tests.test_exam_offline_load --keepdb --noinput`.

Recommendation: measured offline bookkeeping is small at this scale. Retain
monitoring for query latency, realtime event frequency, Celery queue length and
refresh duration at rollout. The websocket path still adds one DB query/event;
no caching change was made because stale visibility can leak results.
