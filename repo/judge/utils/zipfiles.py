import zipfile

from django.conf import settings


def get_zipfile_write_kwargs():
    compression = getattr(settings, 'DMOJ_ZIPFILE_COMPRESSION', zipfile.ZIP_DEFLATED)
    compresslevel = getattr(settings, 'DMOJ_ZIPFILE_COMPRESSLEVEL', 6)

    kwargs = {
        'mode': 'w',
        'compression': compression,
    }
    if compression != zipfile.ZIP_STORED:
        kwargs['compresslevel'] = compresslevel
    return kwargs


def open_zipfile_for_write(path):
    kwargs = get_zipfile_write_kwargs()
    try:
        return zipfile.ZipFile(path, **kwargs)
    except TypeError:
        # Python without `compresslevel` support.
        kwargs.pop('compresslevel', None)
        return zipfile.ZipFile(path, **kwargs)
    except RuntimeError:
        # Compression backend unavailable in runtime.
        return zipfile.ZipFile(path, mode='w', compression=zipfile.ZIP_STORED)
