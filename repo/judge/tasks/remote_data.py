"""Readiness is a comparison against OJ's current manifest, never a bare READY flag."""
import logging

from celery import shared_task
from django.conf import settings
from django.db.models import F

from judge.models import ProblemDataManifest
from judge.utils import storage_client
from judge.utils.remote_data import manifest_digest, storage_matches_manifest

logger = logging.getLogger(__name__)


def check_remote_data(problem_id):
    row = ProblemDataManifest.objects.select_related('problem').filter(problem_id=problem_id).first()
    if (not row or not row.sha256 or not row.problem.is_locked or
            row.problem.is_manually_managed or row.problem.is_mirror):
        return False
    if manifest_digest(row.manifest) != row.sha256:
        return False
    pages, cursors, cursor = [], set(), None
    for _ in range(100):
        page = storage_client.get_problem_files(problem_id, cursor=cursor, limit=500)
        if not page:
            return False
        pages.append(page)
        if not page.get('has_more'):
            break
        cursor = page.get('next_cursor')
        if not cursor or cursor in cursors:
            return False
        cursors.add(cursor)
    else:
        return False
    if not storage_matches_manifest(row.manifest, pages):
        return False
    # A late poll for M1 may never acknowledge M2 or an unlocked problem.
    from judge.utils.test_data_lock import locked_problem
    with locked_problem(problem_id) as problem:
        if not problem.is_locked or problem.is_manually_managed or problem.is_mirror or problem.has_external_problem:
            return False
        return bool(ProblemDataManifest.objects.filter(
            problem_id=problem_id, sha256=row.sha256).update(ready_sha256=row.sha256))


@shared_task(name='sync_remote_data')
def sync_remote_data():
    if not getattr(settings, 'REMOTE_JUDGING_ENABLED', False):
        return
    pending = ProblemDataManifest.objects.filter(
        problem__is_locked=True, problem__is_manually_managed=False, problem__mirror_of__isnull=True,
    ).exclude(sha256='').exclude(ready_sha256=F('sha256')).values_list('problem_id', flat=True)
    for problem_id in pending.iterator(chunk_size=100):
        try:
            check_remote_data(problem_id)
        except Exception:
            logger.exception('Failed to compare R2 grading data for problem %s', problem_id)
    # Also wakes queued retries after a judge's temporary preparation cooldown.
    try:
        from judge.judgeapi import judge_request
        judge_request({'name': 'retry-queued-submissions'})
    except Exception:
        logger.exception('Could not wake remote-ready queued submissions')
