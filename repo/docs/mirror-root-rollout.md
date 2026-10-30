# Mirror root policy rollout

New mirror judging/rejudging inherits the root's complete Min/Sum policy.
Mirrors cannot edit that policy, including through forged superuser POSTs.
Existing legacy metadata and completed-run snapshots are retained. No rejudge
or legacy-data deletion runs as part of migration or deployment.

This rollout also includes the test-data Locked/Unlocked gate and the mirror
cleanup used by the policy path: direct roots only, preserved source grants,
fresh root dispatch, inactive-file isolation, and eight known second-level
relationships flattened to their existing roots.

## Upgrade from current main

The production-main schema already contains migrations 0251 and 0252. Their
dependencies and operations remain unchanged. Migration 0253 depends directly
on 0252 and adds the test-data lock; 0254 updates help text and 0255 only rewrites
the eight known mirror relationships. Streak initialization is not repeated.
A regression test upgrades from exactly 0252 and verifies that
legacy scoring values, submitted points and snapshot-null history are preserved.

1. Update shared-volume judges with the worker changes documented in
   [test-data-lock.md](test-data-lock.md) before using Unlock. The supplied
   `judge_20261029` already supports Min/Sum metadata, including Min early exit;
   it needs the shared file lock and dispatch-ID patch for the full edit gate.
2. Run `python3 manage.py migrate --noinput` in the application environment.
3. Compile Vietnamese messages and restart the web, bridge and Celery processes
   using the deployment's normal commands. Include any separate workers using
   the same application code.
4. Verify a mirror's test-data page has no separate batch editor and a newly
   judged run captures the root policy. Unlock should abort queued/running
   submissions for the root and its mirrors; Apply must leave those runs AB.

Root Min/Sum changes affect future judging/rejudging. Historical mirror results
without snapshots still use their retained legacy value until explicitly
rejudged. Judges without the patch can continue ordinary scoring, but must be
stopped and their workers allowed to exit before applying dataset changes.
