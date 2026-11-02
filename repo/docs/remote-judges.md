# Local and remote judges

Each Judge has an `is_remote` flag, managed in Django admin. Existing and new
judges default to local (`False`). Local means the judge uses the canonical
problem-data filesystem shared with the OJ; remote means it uses independent
problem data.

Disconnect a judge before changing its type. The judge list also offers **Mark
selected offline judges as local** and **Mark selected offline judges as remote**;
online judges are skipped. Reconnect after changing the setting.

Remote judges cannot receive manually managed problems, including mirrors whose
root is manually managed. A manually managed flag on the submitted problem also
blocks remote judging. The bridge checks this for automatic scheduling, queued
jobs, explicitly selected judges, and before sending a submission. If no eligible
judge is available, the submission remains queued under the existing scheduler.

## Verified remote judging

Remote judging is disabled by default (`REMOTE_JUDGING_ENABLED=False`). Existing
local judges keep their protocol and grading behavior and need no upgrade.
Remote judges must run the updated shared judge client and advertise
`problem-data-manifest-v1`. Marking an old client remote cannot bypass this gate.

The implementation changes only OJ and the judge client. It uses the existing
external-storage file-list and per-file download APIs without changing that
service, its PostgreSQL schema, uploader, R2 layout, or dashboard.

### Publication and readiness

Unlock keeps the existing AB/cancellation behavior for the root and its mirrors,
and clears remote readiness. Apply hashes the completed local dataset under the
existing DB/filesystem locks, persists the manifest, and only then locks the
tests. The archive is one SHA-256 object; init.yml and every other file in the
problem directory are hashed separately. Internal file symlinks are recorded by
target bytes and validated; links outside the dataset are rejected.

`ProblemDataManifest` stores only the current manifest, its SHA-256, and the
SHA-256 confirmed available on R2. It is not a dataset lifecycle/history table.
The manifest includes paths, content hashes, sizes, file kinds, permission bits,
and link targets; timestamps, inodes and storage generations do not affect its
identity. Mirrors always use the root's publication.

Local judging can continue after Apply. The background `sync_remote_data` task
compares OJ's manifest against the complete latest READY file listing from
storage. Every page must have the same snapshot ID and every expected object
must be uploaded/verified with matching path, size and SHA-256. A late response
can acknowledge only the same still-current manifest on a still-locked root.
The task wakes idle bridge queues, including retries after temporary failures.

Readiness polling defaults to 300 seconds. Upload, full-object verification,
polling, cold download, hashing, backlog and retries contribute to delay; five
to ten minutes is not a guaranteed deadline. No fixed delay authorizes grading.
Filesystem mutations and source/manual-management transitions invalidate the
publication. Apply or explicit backfill is needed before remote eligibility
can return. Changes to time/memory limits alone do not invalidate file hashes.

### Scoped downloads and worker cache

Dispatch includes the expected manifest hash and a signed task ticket. The OJ
endpoint `/judge/remote-data/` checks assignment, dispatch token, root, locked
state, manual-management exclusions and current readiness before returning the
manifest or issuing existing storage download URLs. URL requests are restricted
to paths in that manifest; storage HTTP runs outside DB/scheduler locks. The
endpoint rechecks eligibility after storage responds. Tickets/URLs are private,
short-lived and must not be logged. R2/storage/DB credentials stay off the judge.

Storage download URLs currently select latest READY, which can change after the
readiness check. The worker always verifies downloaded bytes against **OJ's
requested manifest**, never against a replacement manifest from storage. A
mismatch defers grading; it cannot select an older or newer dataset as fallback.

The judge prepares a clean staging directory, reuses unchanged regular files
from protected cache, and downloads only missing/changed files. A changed ZIP is
downloaded whole. Cache state and dataset are published together under one root
directory rename sequence protected by an exclusive per-root flock. Grading
reacquires a shared flock, rechecks the requested manifest under that lock, pins
all root lookups, and retains the lock through worker cleanup. Lock conversion
is not assumed atomic. All judge processes sharing a cache must use this lock;
cache files are managed exclusively by this implementation.

The cache survives restart. Persisted bytes are reverified once per controller
session before reuse; subsequent submissions for the same manifest need no
download or full rehash. Crash recovery keeps manifest/data paired; incomplete
staging directories are never accepted. Remove abandoned `.staging-*` folders
only with all processes using that cache stopped.

This session-level optimization requires an immutable cache between publications.
Do not edit, rsync over, or externally clean cached datasets. Direct changes to
cached bytes within an already-verified controller session are not detected by
the warm-cache path; a new controller session verifies persisted bytes again.

Preparation runs after immediate ACK in the grading worker, keeping the packet
receiver/cancellation listener responsive. Preparation has a separate timeout
and is outside contestant execution time. Network/checksum/disk preparation
failures return the active run to QU with a new dispatch token and a temporary
remote cooldown; a suitable local judge can take it. A disconnected preparing
judge follows the same retry path. AB stays AB, late packets are ignored, and
actual grading errors keep their existing behavior. Bridge restart retains the
existing global recovery behavior (in-flight runs become IE).
After `problem-data-prepared`, a disconnect also follows the existing IE path,
even if `grading-begin` has not arrived yet.

Historical submissions/scores are unchanged. Explicit rejudge creates a new
run using the current root dataset; historical datasets are not reconstructed.
Problem-controlled assets must be contained in the root dataset. System
headers, compiler/runtime versions and built-in checker libraries come from the
judge image; deploy matching images/configuration on VDS and remote workers.

## Known issues

The initial rollout retains these two availability issues. Neither relaxes
manifest validation or authorizes grading with a different dataset version.

1. **The first queued run after connect/reconnect can be deferred.** The bridge
   registers the client in its scheduling pool before `_connected()` persists
   `online=True`. A sufficiently early manifest request can receive HTTP 409;
   preparation then returns the run to QU with a fresh dispatch token and the
   default 60-second remote cooldown. A later attempt can proceed normally.
2. **An evicted VDS dataset still goes through local restore admission.** With
   `STORAGE_ENSURE_READY_ENABLED`, a missing canonical VDS directory triggers
   ensure-ready/restore before dispatch, including explicitly selected remote
   clients whose manifest is already READY. The run waits in QU and may become
   IE if local restore exhausts its retries. Keep this dependency in mind when
   enabling eviction alongside remote judging.

## Deployment

Launch both local and remote clients with `--no-watchdog`. Shared-filesystem
judges receive inventory refresh requests from the existing storage notifier;
remote caches are prepared per submission. Do not add `--skip-first-scan` by
default. See [judge-deployment.md](judge-deployment.md) for commands, notifier
configuration, and manual refresh when storage is absent.

1. Keep remote judging off; apply migrations through `0260_problem_data_manifest`
   and restart OJ web, bridge and Celery workers. Migration numbering here is for
   the current source branch and must follow the final preceding PR migrations.
2. With local data available, run `python manage.py backfill_remote_manifests`
   (or `--problem CODE`). Locked managed roots without a manifest are scanned;
   missing/local-invalid data is skipped and remains local-only. Existing valid
   manifests and submission results are preserved.
3. Build an updated shared judge image. From a judge source checkout that already
   has MIN/SUM support, apply the existing
   [test-data lock](patches/judge-test-data-lock.patch) and
   [Invocations](patches/judge-invocations.patch) patches if they are not already
   included, then apply [verified remote data](patches/judge-verified-remote-data.patch).
   The remote patch includes its client-side tests and documentation. Check
   applicability before applying; do not apply an already-included patch twice:

   ```sh
   git apply --check /path/to/clueoj/repo/docs/patches/judge-verified-remote-data.patch
   git apply /path/to/clueoj/repo/docs/patches/judge-verified-remote-data.patch
   python3 -m unittest dmoj.tests.test_remote_data dmoj.tests.test_remote_worker dmoj.tests.test_packet_identity
   ```

   Rebuild the image with the deployment's existing compilers, runtimes and
   built-in checker libraries. The OJ repository ships a deployment patch;
   publishing/releasing the separate judge repository/image remains a separate
   operation. Existing local clients can stay unchanged while remote mode is off.

   Configure the new remote judge, keeping its existing runtime configuration:

   ```yaml
   remote_data_cache: /var/cache/clueoj/problem-data
   remote_data_timeout: 1800
   # Development-only HTTP fixture support; production defaults to false.
   remote_data_allow_http: false
   ```

   Give the judge user a writable persistent cache and enough free space for a
   current dataset plus staging. Multiple processes can share the same cache.
   Use the existing bridge authentication over TLS or a private/VPN connection.
4. Set OJ `REMOTE_JUDGE_DATA_URL=https://OJ_HOST/judge/remote-data/` and
   `REMOTE_JUDGING_ENABLED=true`. Restart web/bridge/Celery and run a Celery Beat
   instance using the tracked `CELERY_BEAT_SCHEDULE` (the local streak-only beat
   does not run this task). Optional settings/environment variables:
   `REMOTE_DATA_POLL_SECONDS=300`, `REMOTE_DATA_RETRY_SECONDS=60`,
   `REMOTE_DATA_TICKET_SECONDS=3600`. Existing storage client authentication and
   catalog/dirty synchronization must already be configured and enabled.
5. Disconnect the selected judge, mark it remote in admin, reconnect, and verify
   cold download, cache reuse, checker-only updates, AB and rejudge on test data.

For rollback, disable `REMOTE_JUDGING_ENABLED` and restart OJ processes. Local
judging continues. Remote-only queued submissions remain queued until an
eligible judge becomes available; no dataset verification is bypassed.
