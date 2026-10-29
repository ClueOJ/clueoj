"""Read static manual datasets without executing judge configuration or graders.

The web and judges must use the same published, read-only problem directory.
This fingerprint detects local changes; it is not an attestation from a remote judge.
"""
import hashlib
import json
import os
import stat
from functools import lru_cache
from math import fsum, isfinite
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

import yaml
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError(_('Manual test points and weights must be finite, non-negative numbers.'))
    return value


def _static(value, ancestors=()):
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise ValueError(_('Recursive manual test configuration is not supported.'))
        ancestors = (*ancestors, id(value))
        if len(ancestors) > 30:
            raise ValueError(_('Manual test configuration is too deeply nested.'))
        if isinstance(value, dict):
            if any(not isinstance(key, str) or key.endswith('+') or key == 'generator' for key in value):
                raise ValueError(_(
                    'Cumulative scoring requires static manual tests without dynamic keys or generators.'))
            value = value.values()
        for child in value:
            _static(child, ancestors)


def _path(name):
    if (not isinstance(name, str) or not name or '\\' in name or '\x00' in name or
            PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts):
        raise ValueError(_('Manual test file paths must stay inside the problem directory.'))
    return name


def _signature(path):
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(_('Manual test data must contain only regular files and directories, without symlinks.'))
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@lru_cache(maxsize=4096)
def _file_hash(path, signature):
    # Reuse content hashes while inode, size, mtime AND ctime remain unchanged.
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    if _signature(path) != signature:
        raise ValueError(_('Manual test files changed while being checked. Try again after publishing finishes.'))
    return digest.hexdigest()


def _files(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(_('The manual problem directory is missing or is a symlink.'))
    files = {}

    def on_error(error):
        raise error

    for directory, dirs, names in os.walk(root, followlinks=False, onerror=on_error):
        for name in dirs:
            if (Path(directory) / name).is_symlink():
                raise ValueError(_(
                    'Manual test data must contain only regular files and directories, without symlinks.'))
        for name in names:
            path = Path(directory) / name
            files[path.relative_to(root).as_posix()] = _signature(path)
    return files


def manual_fingerprint(problem):
    from judge.models import problem_data_storage

    root = Path(problem_data_storage.path(problem.code))
    files = _files(root)
    digest = hashlib.sha256()
    for name, signature in sorted(files.items()):
        digest.update(json.dumps([name, _file_hash(root / name, signature)], ensure_ascii=True).encode())
        digest.update(b'\n')
    if _files(root) != files:
        raise ValueError(_('Manual test files changed while being checked. Try again after publishing finishes.'))
    return {'version': 1, 'sha256': digest.hexdigest()}


def manual_configuration(problem, snapshot):
    """Use declared batches as the source of truth, never a historical submission."""
    from judge.models import problem_data_storage

    try:
        root = Path(problem_data_storage.path(problem.code))
        before = manual_fingerprint(problem)
        with (root / 'init.yml').open('rb') as source:
            config = yaml.safe_load(source)
        if not isinstance(config, dict):
            raise ValueError(_('Manual init.yml must contain a configuration mapping.'))
        _static(config)
        if (snapshot['default'] not in ('min', 'sum') or not isinstance(snapshot['overrides'], dict) or
                any(not isinstance(key, str) or mode not in ('min', 'sum')
                    for key, mode in snapshot['overrides'].items())):
            raise ValueError(_('Unsupported subtask scoring mode.'))
        archive_name = config.get('archive')
        archive = ZipFile(root / _path(archive_name)) if archive_name is not None else None
        try:
            def require_file(name, allow_archive=False):
                name = _path(name)
                if (root / name).is_file():
                    return
                if allow_archive and archive is not None:
                    info = archive.getinfo(name)
                    if not info.is_dir():
                        return
                raise ValueError(_('Missing manual test file: %(file)s') % {'file': name})

            def helpers(node):
                # Custom grader/checker modules and auxiliary checker/interactor sources
                # must be real files: the judge's loaders do not extract them from ZIPs.
                for key in ('custom_judge', 'custom_checker'):
                    if key in node:
                        require_file(node[key])
                for key in ('checker', 'interactor'):
                    if key not in node:
                        continue
                    helper = node.get(key)
                    if isinstance(helper, str):
                        if helper.endswith('.py'):
                            require_file(helper)
                    elif isinstance(helper, dict):
                        if isinstance(helper.get('name'), str) and helper['name'].endswith('.py'):
                            require_file(helper['name'])
                        args = helper.get('args', {})
                        if not isinstance(args, dict):
                            raise ValueError(_('Manual checker arguments must be a mapping.'))
                        sources = args.get('files', [])
                        if isinstance(sources, str):
                            sources = [sources]
                        if not isinstance(sources, list):
                            raise ValueError(_('Manual checker files must be a filename or a list of filenames.'))
                        for name in sources:
                            require_file(name)
                    else:
                        raise ValueError(_('Manual checker arguments must be a mapping.'))

            helpers(config)
            for dataset, key in (('full', 'test_cases'), ('pretests', 'pretest_test_cases')):
                groups = config.get(key, [])
                if not isinstance(groups, list):
                    raise ValueError(_('Cumulative scoring requires explicitly listed manual test batches.'))
                layout = []
                for number, group in enumerate(groups, 1):
                    if not isinstance(group, dict) or 'batched' not in group:
                        raise ValueError(_(
                            'Every test, including zero-point samples and pretests, must belong to a subtask.'))
                    children = group['batched']
                    if not isinstance(children, list) or not children:
                        raise ValueError(_('Every subtask must contain tests and have non-negative points.'))
                    maximum = _number(group.get('points', config.get('points', 1)))
                    weights = []
                    helpers(group)
                    for child in children:
                        if not isinstance(child, dict) or 'batched' in child:
                            raise ValueError(_('Nested manual test batches are not supported.'))
                        weights.append(_number(child.get('points', maximum)))
                        for field in ('in', 'out'):
                            require_file(child.get(field, group.get(field, config.get(field))), allow_archive=True)
                        helpers(child)
                    if not isfinite(fsum(weights)) or fsum(weights) <= 0:
                        raise ValueError(_('A batch needs at least one positive test weight.'))
                    if group.get('dependencies', config.get('dependencies')):
                        raise ValueError(_(
                            'Manual subtask dependencies are not supported for independent cumulative scoring.'))
                    mode = snapshot['overrides'].get('%s:%s' % (dataset, number), snapshot['default'])
                    layout.append({'batch': number, 'count': len(children), 'total': maximum, 'mode': mode})
                _number(fsum(row['total'] for row in layout))
                snapshot[dataset] = layout
            valid_keys = {'%s:%s' % (dataset, row['batch'])
                          for dataset in ('full', 'pretests') for row in snapshot[dataset]}
            if set(snapshot['overrides']) - valid_keys:
                raise ValueError(_('Manual batch scoring overrides do not match the current test batches.'))
        finally:
            if archive is not None:
                archive.close()
        if manual_fingerprint(problem) != before:
            raise ValueError(_('Manual test files changed while being checked. Try again after publishing finishes.'))
        snapshot['manual_files'] = before
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, OverflowError, BadZipFile, yaml.YAMLError) as exc:
        raise ValidationError(_('Manual test data could not be validated: %(error)s'),
                              params={'error': str(exc)}) from exc


def validate_manual_files(problem):
    """Do not silently switch datasets within a locked cumulative generation."""
    if not problem.is_manually_managed or not problem.subtask_tests_locked:
        return
    try:
        expected = problem.subtask_score_config.get('manual_files')
        if not expected or manual_fingerprint(problem) != expected:
            raise ValueError(_(
                'Manual test data changed. Turn cumulative scoring off, wait for OFF, then enable it again.'))
    except (OSError, ValueError) as exc:
        raise ValidationError(_('Manual test data could not be validated: %(error)s'),
                              params={'error': str(exc)}) from exc
