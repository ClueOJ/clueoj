A mirror executes one root problem's complete dataset. Only one mirror level is
allowed: a source must have no mirror_of, and a root with mirrors cannot itself
become a mirror. mirror_root is retained for compatibility and equals mirror_of.
Judge requests carry the current root code; ZIP, checkers, testcase layout and
init.yml are read from that root. Results belong to the submitted problem.
Batch Min/Sum policies are inherited from the root and cannot be edited on a
mirror, including by superusers. Legacy mirror values and completed-run snapshots are
preserved for historical display. New judging/rejudging captures the root policy.
There is no file copying or per-mirror init.yml generation.

The general /edit form does not accept mirror_of. Source configuration lives in
the test-data editor and admin. A superuser can assign any structurally valid
root, including private and cross-organization roots. The saved relationship is
the exception: ordinary metadata edits and applying an unchanged source preserve
it. A different source must pass the current user's normal selection rules.
There is no override flag or organization allowlist.

New source choices require edit permission on the source problem
(Problem.is_editable_by / Problem.get_editable_problems), regardless of whether
it is public, private, or in another organization. Editing the target is still
required by the editor view. Public visibility or organization-admin status alone
does not grant source selection. Superusers can choose any structurally valid root.
Active Virtual Judge sources, cumulative-scoring sources, mirrors, and the target
itself are excluded.

If A edits both source and target, A may assign the source. Another target editor
B can preserve that saved relationship even if B cannot view or edit the source:
/edit leaves it untouched, and /test_data includes the existing source in its
field and accepts an unchanged selection. B cannot select a different source
without edit permission on that new source. After deliberately replacing/removing
a source, B needs source edit permission (or another authorized editor) to select
it again. The relationship grants use for judging, not source access.

Unlock before changing a source or publishing test files. Unlock aborts queued
and running submissions for the problem and its direct mirrors, including score
and cache refreshes. An unlocked root pauses admission for its mirrors. Apply
locks the edited problem again; aborted submissions are not automatically rejudged.
Admission locks and rechecks the current root; dispatch and file-lock metadata
must refer to that same source.

Switching to local test data validates a fresh dataset and removes the mirror's
inactive local files before publication under its exclusive file lock. A fresh
archive path prevents selecting a stale foreign R2 archive. Managed downloads
only expose files declared by the current dataset; remote downloads use its
explicit archive path and require that archive in the snapshot before auxiliary
files can be served. Local current files take precedence over old projections.
Polygon updates likewise detach the mirror and replace its own data; failed
imports roll back DB/source changes and leave the problem unlocked. Filesystem
writes are not journaled; failed local publishing may need another upload.
An existing root's files are never changed by importing into its mirror.

Downloads and testcase reads resolve the same direct root. Using a root to judge
does not grant permission to edit or download its tests. Root download permission
and testcase visibility remain separate checks. R2 requests use the root's ID.
Legacy mirror-local rows/files remain inactive while the problem is a mirror;
GET never repairs them. Migration 0255 flattens eight known second-level mirrors
to their existing roots, without changing test files, policies or past results.
