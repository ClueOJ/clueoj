# Mirror root policy rollout

New mirror judging/rejudging inherits the root's complete Min/Sum policy.
Conservative dependencies also come from the root; mirrors cannot edit either
policy, including through forged superuser POSTs. Existing legacy metadata and
completed-run/contest snapshots are retained. No rejudge or legacy drop runs as
part of migration or deployment.

This rollout also includes the test-data Locked/Unlocked gate and the mirror
cleanup used by the policy path: direct roots only, preserved source grants,
fresh root dispatch, inactive-file isolation, and eight known second-level
relationships flattened to their existing roots.

## Upgrade from current main

The production-main schema already contains migrations 0251 and 0252. Their
dependencies and operations remain unchanged. New migration 0253 joins that
mainline with the Conservative schema branch; 0254 updates help text and 0255
only rewrites the eight known mirror relationships. Streak initialization is
not repeated. A regression test upgrades from exactly 0252 and verifies that
legacy scoring values, submitted points and snapshot-null history are preserved.

1. Update shared-volume judges with the worker changes documented in
   [test-data-lock.md](test-data-lock.md) before using Unlock. Root scoring
   metadata itself remains compatible with older judges, which simply lack
   the Min early-exit optimization.
2. Run `python3 manage.py migrate --noinput` in the application environment.
3. Compile Vietnamese messages and restart the web, bridge and Celery processes
   using the deployment's normal commands. Include any separate workers using
   the same application code.
4. Verify a mirror's test-data page has no separate batch/Conservative editor,
   and a newly judged run captures the root policy. Existing Conservative
   contests start OFF; enable/rebuild is an explicit superuser action.

Root Min/Sum changes affect future judging/rejudging. Conservative defaults are
pinned by contests at enable/rebuild; changing root defaults does not rewrite
an existing contest snapshot. Historical mirror results without snapshots still
use their retained legacy value until individually rejudged or migrated later.
