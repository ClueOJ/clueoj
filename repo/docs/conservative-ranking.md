# Conservative Ranking

An optional, live alternative scoreboard at `/contest/<key>/conservative/`.
Each submission is rescored from its stored full-test results with MIN inside
every batch and its transitive dependency tests. A participant's problem
score is the best **single submission** total. Subtasks from different programs
are never combined. Every testcase must belong to a subtask, including zero-point
testcases; standalone testcases are not supported by this view.

Normal submission scores, testcase results, judge policies, the primary contest
scoreboard, Users/PP and contest rating are unchanged. Enabling the feature never
dispatches a rejudge.

## Permissions and setup

Only **superusers** can configure dependencies or enable, disable and rebuild
Conservative Ranking. Contest authors and other test editors do not receive these
controls; forged POST requests are also rejected.

The superuser panel includes **Show Conservative Ranking**, independently of
enabling/building the feature:

- **Yes** (default): access and own-only/full-contest scope follow the normal
  Rankings page, including contest access and offline restrictions.
- **No**: only the contest's authors, curators and superusers can view it. Global
  contest editors, testers and users granted normal scoreboard access are not
  automatically included. Organizers can preview it before the contest starts or
  while offline; turning the feature OFF still hides it for everyone.

The tab, full page, live `?raw` responses and public ranking links all enforce this
policy before reading cached content. A public link alone does not bypass the
**No** setting. Changing visibility takes effect without rebuilding or rejudging;
existing enabled contests retain **Yes** on migration. The policy does not change
normal Rankings visibility, and authors/curators cannot change Conservative
settings unless they are also superusers.

1. Open the problem's **Test data** page (`/problem/<code>/test_data`). A separate
   **Conservative dependencies** table appears below the testcase form. For each
   subtask, select the other subtasks whose tests should also enter its MIN calculation. Save
   with the table's own button. This works for problems outside contests too.
2. Open **Edit contest**, then the **Conservative Ranking** panel. Enable it.
   Celery walks existing contest submissions, with persistent progress, and
   publishes the scoreboard when the build is complete.
3. Open the new contest tab. Its explanation is available in Vietnamese and
   English. The table refreshes every ten seconds, including after the contest
   ends, so late judging/rejudging and historical builds can be displayed.

Mirrors inherit their root's Conservative dependencies (including native
dependencies for manually managed roots). Configure them on the root; mirror
forms and backend writes are disabled even for superusers. A contest snapshot
keeps the submitted mirror's identity. Existing mirror-local defaults are ignored
for new builds, without deleting historical configuration.

Defaults are pinned per contest at enable/rebuild time. Updating the problem's
defaults does not silently change running contests. Use **Apply current
dependencies and rebuild** to adopt them. Save structural testcase changes first;
changed subtask numbers or maximum points require reviewing the dependency table
again. Changing testcase counts, positions, files or IDs alone does not invalidate
the dependency configuration.

Self-dependencies, missing subtasks and cycles are rejected. Dependencies are
transitive and evaluated within the same submission, before totals are compared.
For example, if S6 includes S5, S5 has minimum test ratio 60%, and S6 has own
minimum ratio 80%, S6 receives 60% of its original maximum. S5 need not reach full
credit. Dependency tests neither add to nor replace S6's maximum; repeated paths
in a dependency graph do not double-count tests or points.

Zero-point dependency groups are evaluated from stored binary test outcomes,
not from the equality `0 == 0`.

The dependency form remains separately available to superusers when cumulative
practice scoring locks the actual testcase editor. Saving it does not bypass
that test-data lock, compile files, change `init.yml`, or affect the cumulative
practice score.

## Build and live results

`OFF → BUILDING → ON`; unresolved historical results lead to `FAILED` with a
superuser-only diagnostic list. Retry via the rebuild button after resolving the
data issue. Disable immediately hides the tab and retires the build generation.
Late deliveries cannot publish into another generation. Builds checkpoint every
100 submissions and retry transient worker failures; exhausted build failures
are reported instead of remaining indefinitely in BUILDING. Dirty scores are marked pending before delivery; queue failures cannot leave an old judged run marked valid or prevent primary judge dispatch. A submission committing across rebuild generations is queued for the current generation.

New judging, rejudging, CE/IE/AB, deletion of a best submission and contest problem
changes update the derived view. A CE is a valid zero; queued/running submissions
are pending. Aborted submissions (`status=AB` or `result=AB`) are ignored entirely:
they contribute neither points nor submission counts, do not make the ranking
provisional, and do not fail the build. This applies even when an aborted run has
partial testcase records or obsolete derived points. The superuser progress panel
reports the ignored count. IE, incomplete testcase records and real pretests
remain unresolved. Valid earlier submissions remain usable while a new result is
being processed; the live table marks unresolved results as provisional.

Each `ContestSubmission` stores its derived score and batch breakdown. Page
requests never replay raw testcase records. Public viewers share rendered output
with a five-second refresh window and a refresh lock; score arrivals do not
continually evict the hot cache. Visibility policy changes invalidate immediately.
Cache keys distinguish build generation/state, language, participation scope,
hidden-subtask policy and freeze cutoff. Admin CSRF cells are not shared.

Scoreboard visibility, private contest access, offline publication, frozen
results, hidden subtasks and disqualification continue to apply. Existing public
ranking links expose an additional `/public-ranking/<token>/conservative/` view,
with the same expiry/status checks. Public links never contain management errors,
even when first visited by a superuser.

## Historical data and dependencies

MIN is based on the lowest earned/maximum ratio among positive-weight tests.
Batch maximums and contest point scales are preserved. Partial checkers remain
supported. MIN ratios propagate before rounding and are multiplied by each
dependent subtask's own maximum. Full-score checks for the problem's partial-point
setting still use unrounded values with floating-point tolerance.

Polygon complete-group imports already include prerequisite tests in the batch;
MIN naturally evaluates those tests. Manually managed native `init.yml`
dependencies are captured separately when available. Custom conservative
dependencies include the selected groups' tests in the MIN calculation without
changing judge execution.

Historical runs need the same number of subtasks and the same maximum points for
each corresponding subtask as the contest's captured configuration. Every stored
testcase must belong to a subtask. Testcase counts, IDs, positions and files may
differ; matching only the overall problem maximum is insufficient. The maximum
of each historical subtask is the sum of its stored testcase maximums, compared
with floating-point tolerance. Legacy runs without scoring snapshots can reuse
their raw results under these same conditions.

A dependency with a known zero test ratio proves the
dependent group's zero even if that group's own tests were skipped. A positive
partial dependency ratio does not determine the result of missing own tests;
those runs remain unresolved. Removing a conservative dependency cannot recover tests
previously omitted by a real judge dependency, or split an already flattened
Polygon batch. Changing the underlying test data may require the matching old
dataset/results; a rejudge against different tests cannot reconstruct history.

Only the affected submissions need investigation when data is missing. Rejudging
is always a separate administrator action that must respect existing locks.

## Deployment and validation

Merge and deploy PR #25 (mirror and test-data Locked/Unlocked) first. Only then
merge and deploy this Conservative Ranking change. The review PR is based on
`clue-feat` so its diff contains only the feature added on top of #25; after #25
merges, retarget this PR to `main` and rebase if needed to retain that scope.

Apply migration `0256_conservative_ranking`, which depends on #25's
`0255_flatten_mirrors`, compile Vietnamese translations and restart web, Celery
and bridge. Migrations 0253–0255 and streak initialization are not repeated.
All existing/new contests default **OFF** and all
problem dependency defaults start empty. The migration has no data rebuild or
rejudge operation. The judge image itself does not need an update for this view.

Scoring version 2 uses MIN over dependency tests, including partial ratios. After
upgrading an already-enabled contest from the former full-credit gating rule,
use the superuser rebuild action to recalculate its derived scores. The view
requires a rebuild rather than displaying results from the old rule; this does
not trigger rejudging or change primary judging results.

Tests: `judge.utils.tests.test_conservative`, `test_conservative_concurrency`, and
`test_conservative_migration`, together with existing batch/Polygon/weighted,
cumulative, bridge, submission and offline suites. MariaDB is needed for the real
concurrent disable/publication test. Existing suites that assert English text
must run with `LANGUAGE_CODE='en'` and the English translation context.

Local verification on 27 October 2026 used a private demo problem and an inactive,
non-privileged participant. Python judge produced SUM 90 and Conservative 50
after a prerequisite failed. Two anonymous browser tabs automatically changed
50 → 100 after a new accepted submission, then returned to the earlier best when
the accepted submission was rejudged as CE. This initial check used only the demo;
the user subsequently enabled `voi26`, which was also used to verify historical
layout compatibility and abort exclusion without rejudging.
