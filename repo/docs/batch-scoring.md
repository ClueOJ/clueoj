# Per-batch scoring

Batch scoring is web-owned. `ProblemTestCase.batch_type` on a Batch start row
selects `min` or `sum`; new batches default to `sum`. A problem can mix both.

- `min`: sum of testcase maximum points multiplied by the smallest earned/max
  ratio among positive-weight tests. Zero-weight tests do not cause division by
  zero. A one-test batch and a failure on the first test follow the same formula.
- `sum`: the batch maximum multiplied by the sum of earned weighted ratios,
  divided by the sum of test weights. Each test defaults to weight 1. For a
  90-point batch, twenty equal tests contribute up to 4.5 points each. Weights
  `1 1 1 1 1 2 2 2 2 2` and results `AC WA WA WA WA AC AC AC WA WA` earn
  `90 * 7 / 15 = 42` points.
- Standalone tests remain additive. Problem/contest partial-point settings still
  apply when converting the raw score to the problem or contest scale.

The bridge sends the saved policy in optional `meta.batch-scoring` submission
metadata. Existing judges ignore this field, keep their normal testcase loop,
and send the same result packets. No scoring fields are added to `init.yml`, no
R2 snapshots need conversion, and the storage API is unchanged.

## Optional min-batch early exit

Judges supporting version 1 of `meta.batch-scoring` skip the remaining tests of
a `min` batch after a positive-weight test returns a zero-score WA, TLE, MLE,
RTE, OLE, or IR. The remaining tests still produce normal SC results with their
original positions and maximum points, preserving the full layout expected by
web-side scoring. Other batches continue normally, subject to existing batch
dependencies. This batch-local optimization also applies to partial-credit
problems, whose problem-wide `short-circuit` setting is false.

AC and partial checker results continue, including AC with zero points and WA
with positive partial points. Sum batches, standalone tests, and legacy
short-circuit rules retain their previous behavior. The judge validates the
selected full/pretest layout before enabling the optimization; missing,
unsupported, or mismatched metadata leaves the legacy loop in use. Manually
managed and mirror datasets use their saved default and numbered overrides when
the snapshot has no explicit layout.

This protocol addition has no schema migration and supports a rolling judge
upgrade: a new OJ works with old judges, and a new judge works with an old OJ
without early-exit optimization. Upgrade/restart the bridge to send metadata;
build and recreate judge containers from the modified judge source to enable it.
Changing source files or restarting a container built from an older image does
not update its embedded judge code.

## Editing and persistence

The test editor shows the selector on Batch start rows. Dragging a row keeps its
policy. Creating a batch manually, filling a dataset, and grouping the current
tests all start batches in `sum` mode, including reused group markers. A mode-only save updates DB
rows without regenerating files or sending a storage dirty notification. Changes
to test weights, structure, archives, or checkers use the existing compiler flow.
The editor exposes individual weights only in Sum batches; blank weights become
1. Min batches retain existing stored weights so switching scoring mode remains
DB-only, but their weights cannot be edited. Non-negative integer weights are
accepted; every batch needs at least one positive weight, even when its maximum
points are zero. The editor and compiler both reject an all-zero set of weights.

Polygon import preserves `each-test` groups as `sum` batches and
`complete-group` groups as `min` batches. Sum batches use the sum of individual
test points as their maximum and retain those points as testcase weights,
including after fractional-point normalization. The compiler emits these weights
using the existing testcase `points` field understood by existing judges; scoring
mode remains in the DB. Complete-group batches keep the group's maximum and
equal test weights. Ungrouped tests stay standalone. Reimport sets batch policies
from the package. The zero-point testcase filter also applies within each-test
groups; complete-group dependency flattening follows the existing importer rules.
Missing per-test points in each-test groups default to weight 1; an explicit
zero remains zero and follows the zero-point filter option.
Missing points on standalone tests mean zero, including packages with no groups
or point declarations. The zero-point testcase filter removes these tests only
when enabled; otherwise they are imported with points 0. If the entire package
has zero points, partial credit remains disabled. A retained each-test group
with all-zero weights is rejected before replacing the existing dataset.
If the selected zero-point filters remove every test, import stops with an
error before replacing the existing dataset.
After import, the batch maximum can be edited independently of its test weights.

The web consumes earned/maximum testcase points from the judge without another
percentage conversion. The updated judge honors
`treat_checker_points_as_percentage=True/False`: true interprets testlib scores
as 0–100 percent. For SUM, false uses absolute points on the allocated
testcase maximum. Updated judges run MIN checkers on a maximum of 1, then
rescale the earned points back to the allocated maximum before sending
results to the web. Absolute MIN scores therefore use [0, 1]; SUM keeps its
existing scale. Both flags survive web forms and init
generation; non-boolean values are rejected. Polygon custom checker imports
write either true or false explicitly, including false when percent scoring is
not selected. Existing files without a flag retain the judge's legacy true
default. Both modes are verified with a real C++/testlib checker. Malformed or
out-of-range testlib scores report IE rather than contestant WA; PAC, including
zero-point PAC, continues through Min batches. Historical scores are not
rewritten automatically, and old judge images retain their previous behavior.

Archived and mirror datasets have a separate scoring-only table in the test
editor, so editing policies does not require restoring an archive. Mirror
policies do not alter the root's test files or scoring. Manually managed datasets
can use this table once a completed run has exposed their batch numbers; edits
to their file-based structure remain outside the web editor.

Before the bridge dispatches a run, it snapshots the policy for full tests and
pretests in `Submission.batch_scoring`. Grading-begin selects the actual dataset
reported by the judge. Batch order, case counts, and maximum points are validated
against known DB structure before finalization. A mismatch is reported as IE
with instructions to synchronize the data and rejudge. This check does not verify
test file contents; file consistency remains the existing storage system's job.

The web stores finalized raw scores in `SubmissionBatchResult` (batch 0 contains
standalone tests). Submission totals, result-page batch headers, and API batch
scores use the same policy/results. Raw testcase scores remain available. Editing
the current problem policy does not reinterpret completed new runs. Rejudge
captures a fresh policy and replaces the old summary.

## IOI and historical data

IOI scores each submission's batch first, chooses the highest score for each
batch across submissions, then adds those scores in contest units. It cannot
combine passing individual tests from different submissions inside a min batch.
Equal-score ties use the earliest submission time. Hidden subtasks remain
excluded from the displayed participation score. Ultimate's batch result reader
uses the same summaries for its latest completed submission.

Migration `0244_per_batch_scoring` copies each problem's old policy onto its
existing Batch start rows before dropping `Problem.batch_type`. It also records
the migration-time value in non-editable `ProblemData.legacy_batch_type`, including
problems with no DB testcase rows. This compatibility metadata preserves old
result-page display and supplies defaults for manual/mirror datasets.
Migration `0245_default_sum_batches` changes the default for new rows and clears
legacy child point values inside existing batches. The old compiler ignored
these values but left them in the DB; clearing them preserves equal weights when
files are regenerated. Standalone points, batch maxima, policies, and historical
scores remain unchanged. This cleanup runs on the first migration to 0245, before
weighted test edits are enabled. Polygon decimal points are normalized exactly,
without truncating floating-point sums or small positive test weights.

Historical submission totals and testcase scores are not rewritten or rejudged.
Until rejudge, IOI reads historical batches with its previous SUM policy;
historical and new raw batch scores are both converted to the contest scale so
they can be compared consistently. Cached participation scores are not rebuilt
by the migration; they are recalculated on the next normal recomputation.

## Deployment and checks

The focused regression command below passes all 204 tests. The rendered editor's
nine inline scripts pass Node syntax checks. An HTTP edit on the local smoke
problem changed min/sum and restored it without changing the `init.yml` checksum
or the saved scores of prior submissions.

Stop web/Celery/bridge processes, apply migrations through `0245_default_sum_batches`, then
restart those processes together. The schema drops a column used by old web
processes, so this is not a rolling-schema deployment. Judge images stay as-is.
Rollback restores the migration-time problem-wide policy; the old schema cannot
represent new mixed batch policies. Back up before a rollback.

```sh
python3 manage.py test judge.utils.tests.test_weighted_batches \
  judge.utils.tests.test_polygon_batch_scoring judge.utils.tests.test_polygon_import \
  judge.utils.tests.test_batch_scoring \
  judge.utils.tests.test_batch_scoring_migration judge.tests.test_bridge_django \
  judge.models.tests.test_submission judge.models.tests.test_contest \
  judge.utils.tests.test_checker_percentage judge.models.tests.test_batch_submit \
  judge.models.tests.test_batch_submit_concurrency judge.models.tests.test_exam_offline \
  judge.models.tests.test_exam_offline_concurrency judge.models.tests.test_exam_progress \
  --noinput --keepdb
```

The twelve Node test entries (including weight-editor and resolver cases) also pass:

```sh
node --test tests/batch-weight-editor.test.cjs tests/resolver-scoring.test.cjs tests/testcase-groups.test.cjs \
  tests/testcase-group-order.test.cjs
```

Local end-to-end verification on the unchanged C++20 judge: submissions
821691/821692/821693 earned 20/80/40 out of 100 with mixed min/sum batches; IOI
combined the first two into 200/200 contest points. All four tests ran on each
submission and the `init.yml` checksum stayed unchanged. These smoke submissions
are hidden and use a disabled local test account/private contest.

Polygon batch import was verified with twenty regression tests covering mixed
policies, unequal/fractional weights, zero-point filtering, dependencies,
standalone tests, and reimport. The combination matrix covers all seven nonempty
combinations of standalone tests, each-test groups, and complete-group groups,
including both group declaration orders when both policies are present: nine
layouts and twenty-seven scoring outcomes, with full, failed, and fractional
checker ratios. It also covers multiple groups per policy, packages without
group or point declarations, all four zero-point filter combinations, and
reimport transitions between ungrouped, sum, min, and mixed datasets.
Private local submissions 821699/821700/821701
earned 12.5/87.5/50 out of 100 after importing a package with an each-test group
(test weights 5 and 15, sum maximum 20) and a complete-group group (min maximum
20). The unchanged judge reported testcase maxima 5/15/10/10, and IOI combined
the best batch results into 200/200 contest points. This fixture skipped statement
conversion and bypassed only the Pandoc version guard because local Pandoc was
below the importer's minimum at the time; package test parsing, DB persistence, init
generation, dispatch, and judging ran normally.

Weighted editor smoke verification used private local submissions
821702/821703/821704/821705. A Polygon each-test group with weights
`1 1 1 1 1 2 2 2 2 2` was imported, then its batch maximum was changed from
15 to 90 through the real test editor POST. The unchanged local Testlib judge
awarded 42 points for the example AC/WA pattern and 39 when the first AC was
replaced with 50% checker credit. Editing the first weight from 1 to 2 through
the editor produced 45 points while the prior submission stayed at 42. Switching
to Min produced 0 for the same failing pattern; switching modes in either
direction left the init checksum unchanged. Absolute checker-point mode was
verified at the web packet boundary under the production-judge assumption, not
against the local adapter that forces percentages.

## Decimal checker audit

An actual C++ Testlib checker was run on private local problem
`local_decimal_scoring_261019`, with three tests in each of two batches: maximum
1 in min mode and maximum 2 in sum mode. Testcase maxima were fractional (1/3
and 2/3). Submission 821695 used checker percentages 33.333333, 66.666667,
99.9999, 12.5, 37.5, and 100: its saved batches earned 0.33333333/1 and 1/2,
giving 44.444/100. Submission 821698 retained raw batch points of 1e-8 and 2e-8
even though the final display rounds to three decimal places. IOI combined best
whole-batch results into 300/300 contest points. No init fields were added or
changed by scoring.

The audit fixed these issues with regression coverage:

- Full-credit checks now use the raw earned/maximum ratio before display
  rounding, allowing only floating-point representation noise. A checker giving
  99.9999% no longer earns full marks when partial points are disabled. Bridge,
  contest, IOI, rescore, and exam progress follow this rule.
- Rescore now rounds after conversion to the problem scale, matching initial
  grading; it no longer changes 33.333 to 33.3.
- Resolver combines raw subtask scores before rounding, respects contest point
  precision in JavaScript, handles standalone tests and noncontiguous problem
  orders, and indexes hidden subtasks by problem ID. Hidden-subtask sentinel 0
  is ignored. Automatic reveal compares problem IDs with matching types.
- Rejudge archives both raw testcase results and batch summaries correctly.
  A structure mismatch clears stale totals and contest scores, recomputes score
  statistics, and finishes the live update with an internal-error event.

Submission 821697 was rejudged through the unchanged local judge after these
fixes: raw score 2.9999969999999996/3 (99.9999%) earned 0 problem and contest
points with partial scoring disabled. All six tests ran, the init checksum was
unchanged, and the result page rendered successfully in Vietnamese.

One separate milestone-view regression fails identically on the pre-change
source: `MilestoneTests.test_list_and_detail_share_progress_and_guest_default`.
The list requires `compare_milestones=1` while the detail automatically compares;
this is outside batch scoring and was not changed.

Existing judge limitation, verified independently of this web change: the local
Testlib adapter always interprets `quitp` values as percentages, even with
`treat_checker_points_as_percentage=False` (5.4 on a 10-point test becomes 0.54).
The decimal integration checks explicitly used percentage mode. Fixing absolute
checker-point mode requires a separate judge change; the web cannot recover the
checker value after the judge has converted it.

The broader problem/Polygon/storage suite has 17 existing failures (9 failures,
8 errors), reproduced with identical test names on the pre-change source and
the refactor: legacy mirror-sync assumptions, an overlength fixture code, local
host/timezone assumptions, and local file-response expectations. They are outside
this scoring change. The two pre-existing storage model ID migration differences
were not included in this migration.
