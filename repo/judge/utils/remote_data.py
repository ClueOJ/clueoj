"""OJ-owned grading manifests, independent of storage's snapshot generations."""
import hashlib
import json
import logging
import os
import stat
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction


CAPABILITY = 'problem-data-manifest-v1'
TICKET_SALT = 'judge.remote-data.v1'
logger = logging.getLogger(__name__)


def manifest_digest(manifest):
    content = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
    return hashlib.sha256(content).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_error(error):
    raise error


def build_manifest(problem):
    """Caller holds the root's DB and filesystem locks throughout publication."""
    from judge.models import problem_data_storage

    folder = Path(problem_data_storage.path(problem.code))
    if folder.is_symlink() or not (folder / 'init.yml').is_file():
        raise ValidationError('Cannot publish a grading manifest without local init.yml.')
    files = []
    for directory, dirs, names in os.walk(folder, followlinks=False, onerror=_walk_error):
        # os.walk puts symlinks to directories in dirs. Record links, never follow them.
        links = [name for name in dirs if (Path(directory) / name).is_symlink()]
        dirs[:] = sorted(set(dirs) - set(links))
        for name in sorted(names + links):
            path = Path(directory) / name
            relative = path.relative_to(folder).as_posix()
            if (len(relative) > 512 or '\\' in relative or
                    any(part in ('', '.', '..') for part in PurePosixPath(relative).parts)):
                raise ValidationError('Invalid grading-data path: %s' % relative)
            info = path.lstat()
            entry = {'path': relative, 'mode': stat.S_IMODE(info.st_mode) & 0o777}
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(path)
                try:
                    resolved = (path.parent / target).resolve()
                except RuntimeError as exc:
                    raise ValidationError('Grading-data links must not contain cycles.') from exc
                if (os.path.isabs(target) or os.path.commonpath([str(folder.resolve()), str(resolved)]) !=
                        str(folder.resolve()) or not resolved.is_file()):
                    raise ValidationError('Grading-data links must refer to files inside the problem folder.')
                content = target.encode('utf-8')
                entry.update(kind='symlink', target=target, size=len(content),
                             sha256=hashlib.sha256(content).hexdigest())
            elif stat.S_ISREG(info.st_mode):
                entry.update(kind='file', size=info.st_size, sha256=_file_hash(path))
                after = path.stat()
                if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                        after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValidationError('Grading data changed while computing its manifest.')
            else:
                raise ValidationError('Unsupported grading-data file: %s' % relative)
            files.append(entry)
    return {'version': 1, 'problem_id': problem.pk, 'files': sorted(files, key=lambda item: item['path'])}


def publish_manifest(problem):
    """Publish before locking Apply; no remote HTTP inside the publication locks."""
    from judge.models import ProblemDataManifest

    if problem.is_mirror or problem.is_manually_managed or problem.has_external_problem:
        ProblemDataManifest.objects.filter(problem=problem).delete()
        return
    from judge.utils.problem_data import problem_data_lock, problem_data_lock_key_for_problem
    with problem_data_lock(problem_data_lock_key_for_problem(problem)):
        manifest = build_manifest(problem)
        ProblemDataManifest.objects.update_or_create(
            problem=problem, defaults={'manifest': manifest, 'sha256': manifest_digest(manifest), 'ready_sha256': ''})
    if getattr(settings, 'REMOTE_JUDGING_ENABLED', False):
        transaction.on_commit(schedule_sync)


def schedule_sync():
    try:
        from judge.tasks.remote_data import sync_remote_data
        sync_remote_data.delay()
    except Exception:
        # Periodic polling retries; a broker outage must not undo a successful Apply.
        logger.exception('Could not schedule remote grading-data sync')


def invalidate_manifest(problem_id=None, code=None):
    from judge.models import ProblemDataManifest

    rows = ProblemDataManifest.objects.all()
    rows = rows.filter(problem_id=problem_id) if problem_id is not None else rows.filter(problem__code=code)
    rows.update(sha256='', ready_sha256='')


def storage_matches_manifest(manifest, pages):
    """All pages must describe one READY snapshot and exactly the expected files."""
    expected = {item['path']: (item['sha256'], item['size']) for item in manifest['files']}
    actual = {}
    snapshot_id = None
    for page in pages:
        if not page or not page.get('snapshot_id') or page.get('snapshot_state') != 'ready':
            return False
        if snapshot_id is None:
            snapshot_id = page['snapshot_id']
        if page['snapshot_id'] != snapshot_id or page.get('file_count') != len(expected):
            return False
        for item in page['items']:
            path = item['rel_path']
            if (path in actual or item.get('snapshot_id') != snapshot_id or
                    not item.get('uploaded') or not item.get('verified')):
                return False
            actual[path] = (item['sha256'], item['size_bytes'])
    return actual == expected


def remote_ready(root, include_manifest=False):
    from judge.models import ProblemDataManifest

    if not root.is_locked or root.is_manually_managed or root.is_mirror or root.has_external_problem:
        return None
    rows = ProblemDataManifest.objects.filter(problem=root)
    if not include_manifest:
        rows = rows.defer('manifest')
    row = rows.first()
    return row if row and row.sha256 and row.sha256 == row.ready_sha256 else None
