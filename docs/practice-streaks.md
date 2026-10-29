# Practice streaks: immutable achievements

Everyone starts with current and record streaks of zero at the explicit cutover.
Historical scores seed progress baselines; historical submissions never earn days.
A new submission earns a day when its **initial full result** strictly improves
that user's streak progress on an eligible public practice problem. A day is
counted once, even if several submissions/problems improve. Missing a day expires
the current streak; the record remains.

Normal scoring tracks the maximum submission score. Cumulative scoring tracks
maximum points **per subtask**, then sums and scales through the same validated
helpers as `cumulative_breakdown`. For two 50-point subtasks, `[40,0]` followed by
`[0,40]` improves 40 → 80 even though both submission totals are 40. `[30,20]`
afterwards earns nothing. Scores use the site's three-decimal precision.
Full-test fallback with no pretests qualifies; actual pretests do not.

All streak dates use **Asia/Ho_Chi_Minh (UTC+7)**, regardless of profile or
server timezone. A streak day runs from 00:00 to the next 00:00 in Vietnam.
For ordinary practice, grading later can add the original submission day and merge runs. Changing the
profile timezone cannot alter day attribution, today status or leaderboard values.
Results are compared in **completion order**: if a newer
submission has already achieved 60, an older submission finishing with 50 is
not an improvement.

## Judging and publication

Dispatch metadata explicitly distinguishes initial grading from rejudge/rebuild.
Bridge snapshot creation preserves this metadata. A final result writes an outbox
`StreakEvent` and advances its `StreakProgress` high-water mark in the same
transaction as the final score and batch results. Duplicate callbacks/work
are idempotent by submission ID. Dispatch IDs are rechecked under the Problem
and Submission locks; stale internal/external callbacks cannot award a new run.
The outbox retains its result snapshot independently of mutable submissions.

Rejudge, rescore, deleting submissions/problems, public/private changes,
organization changes, and timezone edits never add or revoke earned days, nor
recalculate an existing streak progress baseline. Score mode/layout revisions
have separate progress generations, seeded from existing scores before accepting
new work. Enabling/disabling/rebuilding cumulative scoring itself earns nothing.
The OJ's normal scores, PP and contest standings keep their existing rules.

Only globally public problems without organization memberships qualify. Offline
exam results wait for manual reveal; contest results wait until the contest ends
and its offline/hidden-subtask flags are cleared. Withheld scores do not influence
the public progress ledger. On legitimate publication the original snapshot is
compared once against the then-current ledger. Publication of pre-cutover results
or submissions made on private problems never creates an achievement.

Offline exams use **the first result reveal date**, from `ExamOfflineAttempt.revealed_at`,
instead of the submission date. Ready results are compared against the current
progress ledger within the reveal transaction, using the immutable initial-result
snapshots (including cumulative subtask maxima). The decision and reveal day are
saved before publication commits. Later practice results or a delayed worker
cannot change that decision. Reopening the results never changes the reveal date.
An initial result still grading at reveal is compared when it first becomes
available, but always uses the original reveal day. Revealing several improving
results on one day only earns one day; it does not fill the days between submitting
and revealing. Rejudging still cannot create or revoke an achievement.

Once earned, a day survives hiding/deletion. Evidence rechecks current problem,
offline and contest publication policy before showing the saved score delta.
The shared orange accent uses `#fb8b3d` in both themes for current/history
numbers, heading icons, the summary border and record badges. Kept calendar days match
activity level 3 in the profile's "Submissions in the last year" heatmap:
`#2f9c4c` in light mode and `#26a641` in dark mode, with dark text. Empty cells
and controls/selection retain ClueOJ's existing activity/link palette. Blog and
Comments tabs omit the streak badge in their page title.
Profiles with a streak card leave a 24px gap between the fixed-width sidebar
and main content on desktop, and below the sidebar on mobile.
The Streak tab and calendar are public for listed profiles, including anonymous
visitors. Profiles marked `is_unlisted` keep earning days and updating their
score baselines normally, including cumulative subtasks. Their streak tab,
sidebar card and badges are hidden for every viewer, including the owner and
administrators; direct calendar URLs return 404. Clearing the flag immediately
reveals the existing history and current/record values without a rebuild.
The requested username determines whose history is shown. Calendar
responses retain no-store headers because evidence rechecks current publication
policy; private problems and withheld results remain excluded.
The member leaderboard omits current/record streak columns and streak sorting.

## Storage and worker

Migration 0251 adds `StreakEpoch`, `StreakProgress`, and `StreakEvent`; it does not
change scores or automatically reset anything. The old problem-state/rebuild
request tables are retained for rollback, but have no active producer/consumer.
The generic model invalidation signals and historical reconciliation are retired.
`queue_pair`/`queue_scope` are inert compatibility shims for old imported callers.
`backfill_streaks` refuses to run, including on fresh installations.
Migration 0252 only adds an index for pending offline-attempt events. Existing
earned days are never moved. Pending unapplied events of an already-revealed
attempt are settled once against the ledger at first processing after deployment;
their historical reveal-time ledger is not reconstructed. Legacy applied but
unawarded events retain their saved decision and adopt the reveal day. No history
backfill or new tables are required.

The dedicated `streaks` worker consumes durable events, adds days and only
extends/merges runs. A per-user summary lock protects concurrent awards across
problems. Offline reveal locks its ended attempt, updates submission visibility,
then locks all pending events before any progress rows. It avoids an exclusive
Profile lock, which would conflict with grading's shared FK checks while waiting
for submission rows. Failed transactions roll back and retry with bounded backoff; withheld
events retry at most once a minute. Beat dispatches every 15 seconds. The
`STREAKS_BATCH_SIZE` and `STREAKS_WORK_SECONDS` settings bound each drain task.
No historical submission scan happens in normal processing. A new scoring
configuration may read that pair's existing scores once to seed a new baseline.

## Cutover

1. Stop admission, bridge/judging, the old backfill, streak Beat and workers.
   Back up source/configuration and the database. Keep unrelated data intact.
2. Apply migrations through 0251 with `manage.py migrate --noinput`.
3. Preview `manage.py initialize_streaks`. Apply once with
   `manage.py initialize_streaks --apply --archive /backup/streak-v1.json.gz`.
   The command archives all six old streak tables, seeds historical score
   baselines, clears old earned days/summaries/runs, disables old pending work,
   and records the submission ID/time cutoff in one database transaction.
   The archive is mode 0600. Keep it outside ephemeral containers.
4. Compile translations, collect static assets, enable `STREAKS_ENABLED` across
   web/bridge/workers, then restart them with the same source. Start the dedicated
   worker and Beat. Do not restart historical backfill.
5. Check zero days/current/record, baseline counts, cutoff and no pending
   migrations. Test new improvements, duplicate processing, rejudge and cumulative
   improvements in an isolated DB. Monitor pending events/failures/retry dates.

The initialization command refuses to run a second time. In-flight submissions
admitted before cutover cannot award days. Rollback means stopping these workers
and disabling the flag; retain additive tables and the archive. Restoring v1
history requires the matching old source and an explicit archive/database restore;
unapplying 0251 alone does not restore old earned days.

## Verification

Run `manage.py test judge.models.tests.test_streaks` using a dedicated MariaDB
database and isolated broker/cache. Also run cumulative, batch scoring, manual
subtask, offline-exam and conservative-ranking regressions. Coverage includes
raw-score/cumulative-score differences, fractional scaling, revisions, snapshots
surviving rejudge/deletion, publication privacy, retry/rollback, duplicate callbacks,
original dates, monotone records, public calendar access/publication filters and
concurrent producers/workers.
