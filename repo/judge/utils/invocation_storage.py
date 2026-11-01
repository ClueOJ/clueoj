"""Private source files, independent of problem test-data eviction and replacement."""
import logging
import os
import tempfile
import uuid
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


def source_storage():
    root = settings.DMOJ_INVOCATION_SOURCE_ROOT
    if not root:
        raise ValidationError(_('The source storage directory has not been configured.'))
    return FileSystemStorage(location=root)


def delete_source(path):
    # A cleanup failure must not invalidate a committed replacement or delete
    # its new file. An orphan can be cleaned up after storage is available.
    try:
        source_storage().delete(path)
    except (OSError, ValidationError):
        logger.exception('Could not remove old invocation source %s', path)


def read_source(path):
    try:
        with source_storage().open(path, 'rb') as source:
            return decode_source(source.read(262145))
    except OSError as exc:
        raise ValidationError(_('The source file is unavailable. Upload it again.')) from exc


def decode_source(content):
    try:
        source = content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValidationError(_('Source files must use UTF-8 encoding.')) from exc
    if not source.strip() or '\x00' in source or len(source) > 65536:
        raise ValidationError(_('Source must be non-empty, without NUL bytes, and at most 65536 characters.'))
    return source


def write_source(problem_id, source_id, filename, source):
    storage = source_storage()
    extension = Path(filename).suffix.lower()
    if not extension[1:].isalnum() or len(extension) > 16:
        extension = '.txt'
    name = '%s/%s/%s%s' % (problem_id, source_id, uuid.uuid4().hex, extension)
    target = storage.path(name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(target))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as output:
            output.write(source)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return name
