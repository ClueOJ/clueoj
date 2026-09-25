# Practice streaks

A day qualifies when a completed, published submission strictly exceeds the
user's previous best `Submission.points` for that problem (rounded to the site's
three decimal places). Scores start at zero. Once a full AC (`result=AC`, positive
case_total and case_points >= case_total) is encountered, further submissions for
that problem do not qualify. Pretests do not qualify. Only site-public problems
with is_organization_private=False AND no organization memberships qualify.
Offline practice counts after manual reveal, attributed to the original submission
date, not reveal/grading date. Mirrors remain distinct problem IDs.

Historical reconstruction uses current results, problem visibility/memberships,
and the account's current IANA timezone. The old schema has no complete history
of those settings. Rejudge, rescore, deletion, visibility/ORG changes and timezone
changes reconcile affected history; they never award the administrator's action
date. A timezone change rebases all history, rather than mixing calendars.

## Storage and concurrency

0243 only creates six derived tables and their indexes/constraints. It does not
alter Submission/Profile, rewrite scores, or run a data backfill. It deliberately
omits unrelated pre-existing migration drift in storage IDs/timezone choices.

Completed Submission.save, terminal bridge updates, external final results and
offline publication enqueue durable user/problem work in the same transaction.
Celery Beat asks a dedicated `streaks` queue to drain every 15 seconds. No broker
call is required by the submission request. Repeated work is idempotent. Workers
lock the user summary then the problem state; producers only lock problem state.
A producer blocked by a worker marks the row pending after that worker commits.
Failed processing rolls back and retries with bounded backoff; other work proceeds.
Metadata requests expand in pages with a persisted cursor and are reset on edits.

New chronological results read only the suffix after the stored (date,id) cursor.
Out-of-order completion/rejudge/deletion rebuilds only the affected user/problem.
Only contribution dates affected by that pair are repaired. Run repair touches
only segments adjacent to changed days (windows expand one day and overlap-merge);
summary fields come from indexed last-day/max-length lookups, so steady-state
processing never rescans a user's whole history. Rejudge resets that destroy a
previous score, judgeapi terminal errors/aborts and bridge-start IE recovery all
enqueue durable invalidation in the same commit. Submission list rows show the
owner's current streak badge; public_summary_map fetches one summary query per
page, never one query per row. All streak UI strings are gettext-translated
(Vietnamese ships in locale/vi). The private calendar reads at most a year,
evidence at most 100 rows and run history 20 rows per page. Public summary is
one indexed lookup; it expires by local date in application logic, without one
cron job per user. Pending results can retroactively repair yesterday.
Authoritative permission checks and no-store headers protect the private route,
including from other admins.

Bulk SQL bypasses signals: use queue_scope('problem', id), queue_scope('user', id)
or queue_pair(user_id, problem_id) in the mutation transaction. Deleting an ORG
itself should queue its former problem IDs before deletion (see signals).

## Safe rollout

1. Verify the deployed commit, pending migrations, broker/cache configuration,
   available disk and backups. Test the full migration chain in an isolated DB
   before upgrading installations older than 0242. Never run tests against the
   live database or let test Celery tasks reach the live broker.
2. Keep STREAKS_ENABLED=false while applying migrations. Inspect migrate --plan and the 0243 CreateModel/index operations.
   Back up the DB before production migration. Rollback of the feature means
   disabling its flag/worker, retaining additive tables; do not unapply migrations
   or drop historical data to disable it.
3. Run `python manage.py streak_submission_index` (dry run), inspect EXPLAIN and
   disk space, then `... streak_submission_index --apply` before large backfills.
   This independent, idempotent operation requests ALGORITHM=INPLACE, LOCK=NONE,
   with a five-second metadata lock wait. Unsupported online DDL fails; it never
   falls back to blocking COPY. Building an index still consumes I/O/disk. The
   optional operational index is deliberately outside automatic migration DDL.
4. Enable STREAKS_ENABLED on site, bridge, ordinary Celery and Beat; start a single
   streak worker. `docker-compose.streaks.yml` is an optional overlay, layered
   after the existing storage overlay. It limits the worker to one process,
   prefetch 1, 0.5 CPU and 512 MB. Share the production Django settings file.
5. collectstatic to publish resources/streaks.css and resources/streaks.js, then restart web/bridge/workers
   on the same code. Existing users begin with no derived streak until backfill.
6. Preview: `python manage.py backfill_streaks --after-user 0 --limit 100`.
   Apply: append `--apply`. Persist the printed next --after-user checkpoint and
   repeat until Profiles: 0. Repeating a page is safe. This schedules history;
   wait for both request and pending-pair counts to reach zero before claiming
   backfill complete. For controlled standalone execution add `--drain
   --max-batches 20 --pause 1`; it drains bounded work, not an unlimited loop.
7. Monitor pending count/oldest work, failures/retry_at, task duration and database
   CPU/I/O. Batch count/time budget bounds task scheduling between pairs; a single
   unusually large user/problem history can take longer. Pause by stopping the
   dedicated worker, preserving DB requests; lower batch size before resuming.

Reconcile one user with `backfill_streaks --user PROFILE_ID --apply --drain`.
The IDs here are Profile IDs, not auth.User IDs. Check histories against ordered
raw submissions including offline_hidden/is_pretested and full-AC semantics.

## UI

Public profile: current and longest streak only, using fa-fire and six color
levels (0, 1, 7, 30, 100, 365). Private owner tab: today's status, account timezone,
yearly/monthly calendar with checkmarks, day evidence and paginated streak ranges
with the first missed date. Colors are supplementary; keyboard links, labels and
44px mobile day cells support access without hovering. No continuous animation.

## Verification

Run `manage.py test judge.models.tests.test_streaks` on a dedicated MariaDB test
DB. Tests cover higher-best-only scoring, repeated full AC, midnight/DST, offline
publication, reversed completion order, retry/transaction rollback, metadata
reconciliation, query shape, owner-only/no-store responses, safe backfill preview,
two concurrent workers, and a result arriving while a worker holds its lock.
Also run the existing offline, submission and exam-progress regression suites.

The legacy migration graph contains replaced initial migrations whose individual
files are absent. Django 3.2 sqlmigrate loads with replace_migrations=False and
can fail on missing 0084 before reaching 0243. For SQL audit use MigrationLoader
with replacement resolution enabled and schema_editor(collect_sql=True); this
was used to confirm 0243 does not alter existing tables. Normal migrate and tests
resolve the squashed migration graph.

Local validation (2026-09-23): 63 targeted/regression tests passed on MariaDB
11.4, including transaction concurrency; Django check passed. Collected migration
SQL contained 18 statements and no alterations to existing tables. Online index
creation succeeded. Chromium checked 1280px light and 375px light/dark: no page
overflow, 12 qualifying days and >=44px mobile day targets. Existing migration
drift remains for Profile timezone choices and two storage ID fields; excluded
from this feature.
