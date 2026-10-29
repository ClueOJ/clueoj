# MIN testcase display

Testcase rows in a MIN batch display their score ratio on a maximum of 1.
A full result of `5/5` displays as `1/1`; `2.5/5` displays as `0.500/1`
(or `0,500/1`, depending on locale).
SUM batches and standalone cases retain their allocated points. A testcase
with maximum 0 retains its original display, avoiding division by zero.

The template uses the existing batch scoring mode and `case.points / case.total`.
It formats the ratio with the same three-decimal convention as other case scores.
Both the full page and AJAX use this template. Raw testcase fields, verdicts,
batch/submission scores and rankings are untouched.

There is no extra metadata, DB change, view/bridge hook or judge change.
Existing results also use this display without rejudging. The existing batch
mode resolution preserves the run's saved policy when available; legacy runs
keep the existing fallback for resolving their mode. Historical non-unit test
weights do not require special handling because only the ratio is displayed.

The change is independent of cumulative scoring and alternative scoreboards.
Tests are in `judge/utils/tests/test_case_display.py`.
