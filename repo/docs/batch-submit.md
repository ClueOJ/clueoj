# Batch submit

`/batch-submit/` is restricted to active superusers on every endpoint. Migrate
`judge.0243_submission_batch` and restart the web and Celery workers to enable it.
No production credentials or remote services are needed for the local deployment.

## Workflow

1. Enter the contest code and click Verify (or press Enter). The server checks
   existence, start/end time and that it contains problems, then shows the contest
   name and problem count. Editing the code invalidates the previous verification.
   Choose a local directory with the shape
   `sources/<existing username>/<filename.cpp or filename.py>`.
2. Filenames are lowercased before grouping and mapping to a contest problem.
   `gift.cpp`, `GIFT.cpp` and `GIFT.CPP` share one mapping. Username directories
   retain their case. Colliding filenames within one user directory must be
   resolved before upload; no source is silently overwritten. The browser suggests a unique exact
   code or suffix match (`tet.cpp` → `voi26_tet`); review before continuing.
   An unmapped file is skipped. Other extensions and nested directories are skipped
   with a count shown in the UI. Missing files are shown as a user coverage count.
3. Upload in small chunks and inspect the persistent preview. No user is joined,
   stopped or submitted during preview. Duplicate chunk delivery is idempotent.
4. Run the valid rows. Invalid rows remain visible and do not block valid users.
   Recheck access, contest/personal deadline and submission limits at admission.
5. Reopen the batch to see submission links and live results. Resume only processes
   pending rows; submissions that already have an ID are never recreated.

## Admission rules

- `.cpp` uses `CPP20`; `.py` uses `PYPY3`. Runtime records must exist and the problem
  must allow the language. Actual grading requires a judge supporting that runtime.
- Only local-judge problems are supported; external judge mappings are rejected.
- Reject missing/inactive users, inaccessible contests, bans, disqualifications,
  expired personal participation and closed registration without prior registration.
  Contest editors/testers cannot be converted into official participants.
- A superuser's batch join bypasses the contest access-code prompt, but does not
  bypass the target user's contest visibility/membership or bans.
- Under the same Profile lock as ordinary submit/contest/offline transitions:
  finish any active offline attempt without revealing its results, switch away
  from another contest, and create/reuse the official participation. Existing
  submissions and scores are preserved. A preregistration begins at admission.
- Use actual submission timestamps; never backdate. If the contest or participation
  expires during admission, roll back the submission and all transitions.
- User queue limits delay pending rows rather than flooding the judge. Celery works
  in groups of up to 25 admissions and reschedules remaining rows after 3 seconds.
  Different users can progress while a user's judge queue is full.

## Persistence and limits

The batch records its initiating superuser, contest and expected file count. Each
item records username, path, problem, runtime, transition summary, state and unique
submission reference. After admission, source lives in `SubmissionSource` and the
extra draft copy is cleared. Preview rows and errors remain for auditing.

Maximum: 2000 files per batch; 65536 Unicode characters per source, UTF-8 input.
The browser sends five files per chunk, with a server request cap of 2 MiB.
Only the creator can upload/start/resume; all superusers can inspect batches.
An incomplete upload cannot be started; create a fresh preview to recover.

If Celery stops before admission, resume pending rows. If a worker or judge bridge
fails after creating a submission, its ID is retained; use the existing submission's
rejudge action. There is deliberately no automatic re-submission of ambiguous rows.
Re-uploading the same folder as a *new batch* intentionally creates new submissions.

## Verification

Run against the transactional test database:

```sh
python3 manage.py test judge.models.tests.test_batch_submit \
  judge.models.tests.test_batch_submit_concurrency \
  judge.models.tests.test_exam_offline \
  judge.models.tests.test_exam_offline_concurrency --noinput --keepdb
```

Tests cover admission, attribution, private access, bans, active users, runtime and
problem selection, personal/contest deadlines, rollback, preregistration,
registration and submission limits, offline preservation, duplicate delivery,
competing batches, queue delay, dispatch failure, superuser-only routes, CSRF,
preview and rendered templates.

Local browser smoke: select directory, mapping, preview and submit via Celery and
the real local judge. Two C++20 submissions returned AC while missing and denied
users were rejected; the other contest/offline transitions were verified.
The local judge image does not include PyPy3, so Python grading was not smoke-tested.
