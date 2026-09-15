import json
import os
import re
import tempfile
import time
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar

try:
    import fcntl
except ImportError:
    fcntl = None

import yaml
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.urls import reverse
from django.utils.translation import gettext as _


_active_problem_data_lock = ContextVar('active_problem_data_lock', default=None)


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


@contextmanager
def problem_data_lock(lock_key, timeout=None):
    lock_key = re.sub(r'[^A-Za-z0-9_.-]', '_', str(lock_key or 'unknown'))
    if _active_problem_data_lock.get() == lock_key:
        yield
        return
    if timeout is None:
        timeout = getattr(settings, 'STORAGE_FILE_LOCK_TIMEOUT', 30)
    lock_root = os.path.join(settings.DMOJ_PROBLEM_DATA_ROOT, '.locks')
    os.makedirs(lock_root, exist_ok=True)
    lock_path = os.path.join(lock_root, '%s.lock' % lock_key)
    with open(lock_path, 'a+') as lock_file:
        if fcntl is not None:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Timed out acquiring problem data lock %s' % lock_key)
                    time.sleep(0.05)
        token = _active_problem_data_lock.set(lock_key)
        try:
            yield
        finally:
            _active_problem_data_lock.reset(token)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def problem_data_multi_lock(*lock_keys):
    codes = sorted({str(lock_key) for lock_key in lock_keys if lock_key})
    exits = []
    try:
        for code in codes:
            cm = problem_data_lock(code)
            cm.__enter__()
            exits.append(cm)
        yield
    finally:
        for cm in reversed(exits):
            cm.__exit__(None, None, None)


def _atomic_write(target_path, content_bytes):
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix='.tmp_', suffix='.atomic')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
        _fsync_dir(target_dir)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write_file(target_path, content):
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix='.tmp_', suffix='.atomic')
    try:
        with os.fdopen(fd, 'wb') as f:
            if hasattr(content, 'chunks'):
                chunks = content.chunks()
            elif hasattr(content, 'read'):
                chunks = iter(lambda: content.read(1024 * 1024), b'')
            else:
                chunks = (bytes(content),)
            for chunk in chunks:
                if isinstance(chunk, str):
                    chunk = chunk.encode()
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        return tmp_path
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _validate_zip(path):
    with zipfile.ZipFile(path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ProblemDataError(_('Corrupted entry in zip: %s') % bad)


if os.altsep:
    def split_path_first(path, repath=re.compile('[%s]' % re.escape(os.sep + os.altsep))):
        return repath.split(path, 1)
else:
    def split_path_first(path):
        return path.split(os.sep, 1)


class ProblemDataStorage(FileSystemStorage):
    def __init__(self):
        super(ProblemDataStorage, self).__init__(settings.DMOJ_PROBLEM_DATA_ROOT)

    def url(self, name):
        path = split_path_first(name)
        if len(path) != 2:
            raise ValueError('This file is not accessible via a URL.')
        return reverse('problem_data_file', args=path)

    def _save(self, name, content):
        # Atomic save: stream to temp file, validate temp, then publish with os.replace.
        # No delete-before-save, and invalid archives never replace the previous valid file.
        full_path = self.path(name)
        lock_key = _active_problem_data_lock.get() or 'code:%s' % split_path_first(name)[0]
        if not getattr(settings, 'STORAGE_ATOMIC_WRITES_ENABLED', True):
            # The legacy delete-before-save path is intentionally not restored because it can lose data.
            # Disabling the flag keeps the same validate-before-publish writer as the safe rollback path.
            pass
        with problem_data_lock(lock_key):
            tmp_path = _atomic_write_file(full_path, content)
            try:
                if name.lower().endswith('.zip'):
                    _validate_zip(tmp_path)
                os.replace(tmp_path, full_path)
                _fsync_dir(os.path.dirname(full_path))
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        return name

    def get_available_name(self, name, max_length=None):
        return name

    def rename(self, old, new, lock_key=None):
        with problem_data_multi_lock(lock_key or 'rename:%s:%s' % (old, new)):
            os.makedirs(os.path.dirname(self.path(new)), exist_ok=True)
            result = os.rename(self.path(old), self.path(new))
            _fsync_dir(os.path.dirname(self.path(new)))
            return result


class ProblemDataError(Exception):
    def __init__(self, message):
        super(ProblemDataError, self).__init__(message)
        self.message = message


def get_visible_content(archive, filename):
    if archive.getinfo(filename).file_size <= settings.VNOJ_TESTCASE_VISIBLE_LENGTH:
        data = archive.read(filename)
    else:
        data = archive.open(filename).read(settings.VNOJ_TESTCASE_VISIBLE_LENGTH) + b'...'
    return data.decode('utf-8', errors='ignore')


def get_testcase_data(archive, case):
    return {
        'input': get_visible_content(archive, case.input_file),
        'answer': get_visible_content(archive, case.output_file),
    }


def get_problem_testcases_data(problem):
    """ Read test data of a problem and store
    result in a dictionary.

    If an error occurs, this method will return an empty dict.
    """
    from judge.models import problem_data_storage

    init_path = '%s/init.yml' % problem.code
    if not problem_data_storage.exists(init_path):
        return {}

    init_content = yaml.safe_load(problem_data_storage.open(init_path).read())
    archive_path = init_content.get('archive', None)
    if not archive_path:
        return {}

    archive_path = '%s/%s' % (problem.code, archive_path)
    if not problem_data_storage.exists(archive_path):
        return {}

    try:
        archive = zipfile.ZipFile(problem_data_storage.open(archive_path))
    except zipfile.BadZipfile:
        return {}

    testcases_data = {}

    # TODO:
    # - Support manually managed problems
    # - Support pretest
    order = 0
    for case in problem.cases.all().order_by('order'):
        try:
            if not case.input_file:
                continue
            order += 1
            testcases_data[order] = get_testcase_data(archive, case)
        except Exception:
            return {}

    return testcases_data


class ProblemDataCompiler(object):
    def __init__(self, problem, data, cases, files):
        self.problem = problem
        self.data = data
        self.cases = cases
        self.files = files

        self.generator = data.generator

    def make_init(self):
        # The judge server has an ability to find the testcase
        # even if we don't specify it.
        # That is a good behavior, however, the zip file
        # could contain a very large number of testcases
        # and that is not what we want. So in case of user
        # did not specify testcases, we will not create the init
        if self.cases.count() == 0:
            return {}

        cases = []
        batch = None

        def end_batch():
            if not batch['batched']:
                raise ProblemDataError(_('Empty batches not allowed.'))
            cases.append(batch)

        def make_checker(case):
            if (case.checker == 'bridged'):
                custom_checker_path = split_path_first(case.custom_checker.name)
                if len(custom_checker_path) != 2:
                    raise ProblemDataError(_('How did you corrupt the custom checker path?'))
                try:
                    checker_ext = custom_checker_path[1].split('.')[-1]
                except Exception as e:
                    raise ProblemDataError(e)

                if checker_ext not in ['cpp', 'pas', 'java']:
                    raise ProblemDataError(_('Only C++, Pascal, or Java checkers are supported.'))

            if case.checker_args:
                return {
                    'name': case.checker,
                    'args': json.loads(case.checker_args),
                }
            return case.checker

        def get_file_name_and_ext(file):
            file_path = split_path_first(file)
            if len(file_path) != 2:
                raise ProblemDataError(_('How did you corrupt the custom grader path?'))
            try:
                file_ext = file_path[1].split('.')[-1]
            except Exception as e:
                raise ProblemDataError(e)
            return file_path[1], file_ext

        def make_grader(init, case):
            if case.grader == 'output_only':
                init['output_only'] = True
                return

            grader_args = {}
            if case.grader_args:
                grader_args = json.loads(case.grader_args)

            if case.grader == 'standard':
                if grader_args.get('io_method') == 'file':
                    if grader_args.get('io_input_file', '') == '' or grader_args.get('io_output_file', '') == '':
                        raise ProblemDataError(_('You must specify both input and output files.'))

                    if not isinstance(grader_args['io_input_file'], str) or \
                            not isinstance(grader_args['io_output_file'], str):
                        raise ProblemDataError(_('Input/Output file must be a string.'))

                    init['file_io'] = {}
                    init['file_io']['input'] = grader_args['io_input_file']
                    init['file_io']['output'] = grader_args['io_output_file']

                return

            if case.grader == 'interactive':
                file_name, file_ext = get_file_name_and_ext(case.custom_grader.name)
                if file_ext != 'cpp':
                    raise ProblemDataError(_('Only accept `.cpp` interactor'))

                init['interactive'] = {
                    'files': file_name,
                    'type': 'testlib',  # Assume that we only use testlib interactor
                    'lang': 'CPP17',
                }
                return

            if case.grader == 'signature':
                file_name, file_ext = get_file_name_and_ext(case.custom_grader.name)
                if file_ext != 'cpp':
                    raise ProblemDataError(_('Only accept `.cpp` entry'))
                header_name, file_ext = get_file_name_and_ext(case.custom_header.name)
                if file_ext != 'h':
                    raise ProblemDataError(_('Only accept `.h` header'))
                init['signature_grader'] = {
                    'entry': file_name,
                    'header': header_name,
                }
                # Most of the time, we don't want user to write their own main function
                # However, some problem require user to write the main function themself
                # *cough* *cough* olympic super cup 2020 MXOR *cough* *cough*
                # Check: https://github.com/DMOJ/judge-server/issues/730
                if grader_args.get('allow_main', False):
                    init['signature_grader']['allow_main'] = True
                return

        for i, case in enumerate(self.cases, 1):
            if case.type == 'C':
                data = {}
                if batch:
                    case.points = None
                    case.is_pretest = batch['is_pretest']
                else:
                    if case.points is None:
                        raise ProblemDataError(_('Points must be defined for non-batch case #%d.') % i)
                    data['is_pretest'] = case.is_pretest

                if not self.generator:
                    if case.input_file not in self.files:
                        raise ProblemDataError(_('Input file for case %(case)d does not exist: %(file)s') %
                                               ({'case': i, 'file': case.input_file}))
                    if case.output_file not in self.files:
                        raise ProblemDataError(_('Output file for case %(case)d does not exist: %(file)s') %
                                               ({'case': i, 'file': case.output_file}))

                if case.input_file:
                    data['in'] = case.input_file
                if case.output_file:
                    data['out'] = case.output_file
                if case.points is not None:
                    data['points'] = case.points
                if case.generator_args:
                    data['generator_args'] = case.generator_args.splitlines()
                if case.output_limit is not None:
                    data['output_limit_length'] = case.output_limit
                if case.output_prefix is not None:
                    data['output_prefix_length'] = case.output_prefix
                if case.checker:
                    data['checker'] = make_checker(case)
                else:
                    case.checker_args = ''
                case.save(update_fields=('checker_args', 'is_pretest'))
                (batch['batched'] if batch else cases).append(data)
            elif case.type == 'S':
                if batch:
                    end_batch()
                if case.points is None:
                    raise ProblemDataError(_('Batch start case #%d requires points.') % i)
                batch = {
                    'points': case.points,
                    'batched': [],
                    'is_pretest': case.is_pretest,
                }
                if case.generator_args:
                    batch['generator_args'] = case.generator_args.splitlines()
                if case.output_limit is not None:
                    batch['output_limit_length'] = case.output_limit
                if case.output_prefix is not None:
                    batch['output_prefix_length'] = case.output_prefix
                if case.checker:
                    batch['checker'] = make_checker(case)
                else:
                    case.checker_args = ''
                case.input_file = ''
                case.output_file = ''
                case.save(update_fields=('checker_args', 'input_file', 'output_file'))
            elif case.type == 'E':
                if not batch:
                    raise ProblemDataError(_('Attempt to end batch outside of one in case #%d.') % i)
                case.is_pretest = batch['is_pretest']
                case.input_file = ''
                case.output_file = ''
                case.generator_args = ''
                case.checker = ''
                case.checker_args = ''
                case.save()
                end_batch()
                batch = None
        if batch:
            end_batch()

        init = {}

        if self.data.zipfile:
            zippath = split_path_first(self.data.zipfile.name)
            if len(zippath) != 2:
                raise ProblemDataError(_('How did you corrupt the zip path?'))
            init['archive'] = zippath[1]

        if self.generator:
            generator_path = split_path_first(self.generator.name)
            if len(generator_path) != 2:
                raise ProblemDataError(_('How did you corrupt the generator path?'))
            init['generator'] = generator_path[1]

        pretest_test_cases = []
        test_cases = []
        hints = []

        for case in cases:
            if case['is_pretest']:
                pretest_test_cases.append(case)
            else:
                test_cases.append(case)

            del case['is_pretest']

        if pretest_test_cases:
            init['pretest_test_cases'] = pretest_test_cases
        if test_cases:
            init['test_cases'] = test_cases
        if self.data.output_limit is not None:
            init['output_limit_length'] = self.data.output_limit
        if self.data.output_prefix is not None:
            init['output_prefix_length'] = self.data.output_prefix
        if self.data.unicode:
            hints.append('unicode')
        if self.data.nobigmath:
            hints.append('nobigmath')
        if self.data.checker:
            init['checker'] = make_checker(self.data)
        else:
            self.data.checker_args = ''
        if self.data.grader:
            make_grader(init, self.data)

        if hints:
            init['hints'] = hints

        return init

    def compile(self):
        from judge.models import problem_data_storage

        yml_file = '%s/init.yml' % self.problem.code
        with problem_data_lock(problem_data_lock_key_for_problem(self.problem)):
            try:
                init = self.make_init()
                if init:
                    init = yaml.safe_dump(init)
            except ProblemDataError as e:
                self.data.feedback = e.message
                self.data.save()
                problem_data_storage.delete(yml_file)
            else:
                self.data.feedback = ''
                self.data.save()
                if init:
                    # init.yml is published last, after archive/checker deps are already present.
                    full_path = problem_data_storage.path(yml_file)
                    _atomic_write(full_path, init.encode('utf-8'))
                else:
                    problem_data_storage.delete(yml_file)
            from judge.utils.storage_client import notify_problem_dirty_on_commit
            notify_problem_dirty_on_commit(self.problem)

    @classmethod
    def generate(cls, *args, **kwargs):
        self = cls(*args, **kwargs)
        self.compile()


def problem_data_lock_key_for_problem(problem):
    root_id = getattr(problem, 'mirror_root_id', None) or getattr(problem, 'pk', None)
    return 'problem:%s' % (root_id or getattr(problem, 'code', 'unknown'))
