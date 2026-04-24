import unittest
import zipfile
from unittest import mock

from judge.utils import zipfiles


class ZipFilesTestCase(unittest.TestCase):
    def test_get_zipfile_write_kwargs_with_deflated(self):
        with mock.patch.object(zipfiles.settings, 'DMOJ_ZIPFILE_COMPRESSION', zipfile.ZIP_DEFLATED, create=True), \
                mock.patch.object(zipfiles.settings, 'DMOJ_ZIPFILE_COMPRESSLEVEL', 9, create=True):
            self.assertEqual(
                zipfiles.get_zipfile_write_kwargs(),
                {
                    'mode': 'w',
                    'compression': zipfile.ZIP_DEFLATED,
                    'compresslevel': 9,
                },
            )

    def test_get_zipfile_write_kwargs_with_stored(self):
        with mock.patch.object(zipfiles.settings, 'DMOJ_ZIPFILE_COMPRESSION', zipfile.ZIP_STORED, create=True), \
                mock.patch.object(zipfiles.settings, 'DMOJ_ZIPFILE_COMPRESSLEVEL', 9, create=True):
            self.assertEqual(
                zipfiles.get_zipfile_write_kwargs(),
                {
                    'mode': 'w',
                    'compression': zipfile.ZIP_STORED,
                },
            )

    def test_open_zipfile_for_write_typeerror_fallback(self):
        with mock.patch('judge.utils.zipfiles.get_zipfile_write_kwargs', return_value={
            'mode': 'w',
            'compression': zipfile.ZIP_DEFLATED,
            'compresslevel': 7,
        }), mock.patch('judge.utils.zipfiles.zipfile.ZipFile') as zipfile_mock:
            fallback_obj = object()
            zipfile_mock.side_effect = [TypeError('no compresslevel'), fallback_obj]

            result = zipfiles.open_zipfile_for_write('/tmp/file.zip')

            self.assertIs(result, fallback_obj)
            self.assertEqual(
                zipfile_mock.call_args_list,
                [
                    mock.call('/tmp/file.zip', mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=7),
                    mock.call('/tmp/file.zip', mode='w', compression=zipfile.ZIP_DEFLATED),
                ],
            )

    def test_open_zipfile_for_write_runtimeerror_fallback(self):
        with mock.patch('judge.utils.zipfiles.get_zipfile_write_kwargs', return_value={
            'mode': 'w',
            'compression': zipfile.ZIP_DEFLATED,
            'compresslevel': 7,
        }), mock.patch('judge.utils.zipfiles.zipfile.ZipFile') as zipfile_mock:
            fallback_obj = object()
            zipfile_mock.side_effect = [RuntimeError('backend unavailable'), fallback_obj]

            result = zipfiles.open_zipfile_for_write('/tmp/file.zip')

            self.assertIs(result, fallback_obj)
            self.assertEqual(
                zipfile_mock.call_args_list,
                [
                    mock.call('/tmp/file.zip', mode='w', compression=zipfile.ZIP_DEFLATED, compresslevel=7),
                    mock.call('/tmp/file.zip', mode='w', compression=zipfile.ZIP_STORED),
                ],
            )
