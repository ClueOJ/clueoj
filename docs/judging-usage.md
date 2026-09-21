# Judging resource statistics

Only superusers can access `/status/judging-usage/` and
`/organization/<slug>/judging-usage/`. The site-wide page includes organization
totals; each organization page contains summary metrics and a time-series chart.
The chart shows test execution hours and attempt counts on separate axes.

## Accounting

Only `JudgingUsageDaily` is stored: one row per organization and date, containing
test execution seconds, attempt count, and rejudge count. Organization key 0 means
unowned. There is no attempt ledger or per-problem/test history for statistics.
Hours equal test execution seconds / 3,600. There is no billing, quota, or blocking.

When a judge acknowledges an attempt, the bridge snapshots its problem owner and
start date in memory. The rejudge flag comes from `Submission.rejudged_date`, set
by the existing dispatch path. During grading, statistics accumulate only in RAM.
A last-position marker ignores replayed test positions; the TCP judge protocol
reports test cases in order. There are no statistics DB writes per test packet.

On completion, compilation failure, internal error, cancellation, or disconnect,
the bridge adds usage once to the daily row. Compile errors contribute zero test
seconds. DB-side increments and the organization/date unique constraint protect
concurrent updates. A bridge process crash can lose in-flight usage; this is an
accepted tradeoff. Statistics write failures are logged without blocking grading,
and uncertain writes are not retried.

Dates use `JUDGING_USAGE_TIME_ZONE`, falling back to `DEFAULT_USER_TIME_ZONE`
(`Asia/Ho_Chi_Minh` locally). The entire attempt belongs to its start date and its
initial problem owner. External judges are excluded. Existing submission, source,
and test-case result storage is independent of this feature.

## Migrations and deployment

`0232_squashed_0235_judging_usage` replaces development migrations 0232–0235:

- Fresh production databases create only the final daily aggregate table. They
  do not create the old attempt ledger or per-problem columns and then drop them.
- Databases with all four originals applied recognize the replacement as applied
  and preserve their totals.
- Databases with only some originals applied finish the original migration chain.

Keep the original files for compatibility with partially migrated databases.
Future migrations should depend on `0232_squashed_0235_judging_usage`.
Deploy the entire PR together, including the replacement migration.

Stop accepting judging work, allow active attempts to finish, and stop web/bridge
processes. Inspect `python3 manage.py migrate --plan`, then run
`python3 manage.py migrate --noinput`. On fresh production databases, the judging
usage portion of the plan must contain only the replacement migration.

If the daily table is empty, run `python3 manage.py backfill_judging_usage` before
starting the new bridge. It reads old submissions in batches, excludes external
and QU/P/G submissions, and treats each completed submission as one attempt with
zero historical rejudges. It writes aggregates in one transaction and refuses to
run against a nonempty table, protecting accumulated live usage. Skip this step
when daily totals already exist.

Compile Vietnamese translations with `python3 manage.py compilemessages -l vi`,
then start web and bridge processes. There is no rebuild command because there
is no attempt ledger. Backups must preserve the daily aggregate table.

## Tests

Run `judge.tests.test_judging_usage`, `judge.tests.test_bridge_django`, and
`judge.tests.test_bridge` with Django's test runner. Migration replacement paths
must also be checked against fresh and previously migrated databases.
