"""Invocation admission and source configuration; judging remains owned by Submission."""
import logging
import os
import uuid
from copy import deepcopy
from zoneinfo import ZoneInfo

import yaml
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import (ProblemInvocation, ProblemInvocationItem,
                          ProblemInvocationSource, Profile, Submission, SubmissionSource, problem_data_storage)
from judge.models.invocation import INVOCATION_EXPECTATIONS
from judge.utils.batch_scoring import scoring_snapshot
from judge.utils.invocation_storage import decode_source, delete_source, read_source, write_source
from judge.utils.test_data_lock import locked_problem, require_judging_allowed

logger = logging.getLogger(__name__)
EXPECTATIONS = dict(INVOCATION_EXPECTATIONS)
DEFAULT_LANGUAGES = {'.cpp': 'CPP20', '.cc': 'CPP20', '.cxx': 'CPP20', '.py': 'PYPY3', '.pas': 'PAS'}
LINE_COMMENTS = {
    'c_cpp': '//', 'java': '//', 'javascript': '//', 'typescript': '//', 'csharp': '//',
    'golang': '//', 'rust': '//', 'kotlin': '//', 'swift': '//', 'scala': '//', 'php': '//',
    'python': '#', 'ruby': '#', 'sh': '#', 'perl': '#', 'r': '#', 'julia': '#',
    'haskell': '--', 'lua': '--', 'sql': '--', 'lisp': ';', 'scheme': ';',
}


def can_invoke(user, problem):
    return (user.is_authenticated and user.is_active and user.has_perm('judge.spam_submission') and
            problem.is_editable_by(user) and not problem.is_mirror and not problem.has_external_problem and
            not problem.is_blocked_by_free_organization_plan(user))


def require_invocations(user, problem):
    if not can_invoke(user, problem):
        raise Http404()


def invocation_languages(problem):
    return problem.allowed_languages.filter(
        file_only=False, ace__in=[*LINE_COMMENTS, 'pascal', 'ocaml']).order_by('key')


def default_language(problem, filename):
    key = DEFAULT_LANGUAGES.get(os.path.splitext(filename)[1].lower())
    return invocation_languages(problem).filter(key=key).first()


def full_layout(problem):
    """Read structure only, never execute a generator or a custom grader on the web."""
    snapshot = scoring_snapshot(problem)
    if snapshot['full'] is not None:
        return snapshot['full']
    try:
        with problem_data_storage.open('%s/init.yml' % problem.code, 'rb') as config_file:
            config = yaml.safe_load(config_file)
        groups = config.get('test_cases')
        if not isinstance(groups, list):
            return None
        layout, batch = [], 0
        for group in groups:
            if not isinstance(group, dict):
                return None
            children = group.get('batched')
            if children is not None:
                if not isinstance(children, list):
                    return None
                batch += 1
                layout.append({'batch': batch, 'count': len(children),
                               'mode': snapshot['overrides'].get('full:%s' % batch, snapshot['default']),
                               'total': group.get('points', config.get('points', 1))})
            else:
                layout.append({'batch': 0, 'count': 1, 'mode': 'sum',
                               'total': group.get('points', config.get('points', 1))})
        return layout
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return None


def validate_expectations(overall, subtasks, layout):
    if overall and overall not in EXPECTATIONS:
        raise ValidationError(_('Invalid overall expectation.'))
    if not isinstance(subtasks, dict):
        raise ValidationError(_('Subtask expectations must be a mapping.'))
    groups = {str(row['batch']) for row in layout or [] if row['batch']}
    if any(key not in groups or value not in EXPECTATIONS for key, value in subtasks.items()):
        raise ValidationError(_(
            'Subtask expectations do not match the current test groups. Reload and configure them again.'))


def upload_sources(user, problem, uploads):
    require_invocations(user, problem)
    if not uploads:
        raise ValidationError(_('Select source files to upload.'))
    paths = []
    try:
        with locked_problem(problem.pk) as problem:
            require_invocations(user, problem)
            for upload in uploads:
                if upload.size > 262144:
                    raise ValidationError(_('Source file is too large.'))
                filename = os.path.basename(upload.name.replace('\\', '/'))
                if not filename or len(filename) > 255:
                    raise ValidationError(_('Invalid source filename.'))
                # The problem lock serializes uploads. This query also sees files
                # already inserted in this batch, using the DB's filename equality.
                if problem.invocation_sources.filter(filename=filename).exists():
                    raise ValidationError(_('A source file named "%(name)s" already exists in this problem.'),
                                          params={'name': filename})
                source = decode_source(upload.read(262145))
                item = ProblemInvocationSource(problem=problem, filename=filename, uploaded_by=user,
                                               language=default_language(problem, filename))
                item.path = write_source(problem.pk, item.pk, filename, source)
                paths.append(item.path)
                item.save()
    except Exception:
        for path in paths:
            delete_source(path)
        raise


def update_source(user, problem, source_id, language_id, overall, subtasks, upload=None):
    require_invocations(user, problem)
    new_path = None
    try:
        with transaction.atomic():
            with locked_problem(problem.pk) as problem:
                require_invocations(user, problem)
                item = ProblemInvocationSource.objects.select_for_update().filter(pk=source_id, problem=problem).first()
                if item is None:
                    raise Http404()
                try:
                    language = invocation_languages(problem).filter(pk=language_id).first() if language_id else None
                except (TypeError, ValueError) as exc:
                    raise ValidationError(_('Select a supported language allowed for this problem.')) from exc
                if language_id and language is None:
                    raise ValidationError(_('Select a supported language allowed for this problem.'))
                layout = full_layout(problem)
                validate_expectations(overall, subtasks, layout)
                item.language = language
                item.expected_overall = overall
                item.expected_subtasks = subtasks
                item.expected_layout = layout if subtasks else []
                if upload:
                    if upload.size > 262144:
                        raise ValidationError(_('Source file is too large.'))
                    content = decode_source(upload.read(262145))
                    new_path = write_source(problem.pk, item.pk, item.filename, content)
                    old_path, item.path = item.path, new_path
                    transaction.on_commit(lambda: delete_source(old_path))
                item.save()
                return item
    except Exception:
        if new_path:
            delete_source(new_path)
        raise


def save_source_settings(user, problem, settings):
    """Save the displayed table atomically, preserving files and unseen sources."""
    require_invocations(user, problem)
    try:
        source_ids = [uuid.UUID(str(row['source'])) for row in settings]
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise ValidationError(_('Invalid source settings. Reload and try again.')) from exc
    if not source_ids or len(source_ids) != len(set(source_ids)):
        raise ValidationError(_('Invalid source settings. Reload and try again.'))
    with locked_problem(problem.pk) as problem:
        require_invocations(user, problem)
        sources = {item.pk: item for item in ProblemInvocationSource.objects.select_for_update().filter(
            problem=problem, pk__in=source_ids)}
        if len(sources) != len(source_ids):
            raise ValidationError(_('One or more sources were deleted. Reload and try again.'))
        languages = {str(language.pk): language for language in invocation_languages(problem)}
        layout = full_layout(problem)
        for source_id, values in zip(source_ids, settings):
            language_id = str(values.get('language') or '')
            if language_id and language_id not in languages:
                raise ValidationError(_('Select a supported language allowed for this problem.'))
            overall, subtasks = values.get('overall', ''), values.get('subtasks', {})
            validate_expectations(overall, subtasks, layout)
            item = sources[source_id]
            item.language = languages.get(language_id)
            item.expected_overall = overall
            item.expected_subtasks = subtasks
            item.expected_layout = layout if subtasks else []
            item.save(update_fields=['language', 'expected_overall', 'expected_subtasks',
                                     'expected_layout', 'updated_at'])


def delete_sources(user, problem, source_ids):
    require_invocations(user, problem)
    try:
        source_ids = {uuid.UUID(str(source_id)) for source_id in source_ids}
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError(_('Invalid source selection. Reload and try again.')) from exc
    if not source_ids:
        raise ValidationError(_('Select at least one source to delete.'))
    with locked_problem(problem.pk) as problem:
        require_invocations(user, problem)
        sources = list(problem.invocation_sources.select_for_update().filter(pk__in=source_ids))
        if len(sources) != len(source_ids):
            raise ValidationError(_('One or more selected sources no longer exist. Reload and try again.'))
        # Post-delete hooks remove files only after the entire transaction commits.
        # Invocation items keep their snapshots and submitted code, with source=NULL.
        problem.invocation_sources.filter(pk__in=source_ids).delete()
        return len(sources)


def submission_source(source, language, username, created_at):
    message = 'Invocations by user %s at %s' % (
        username.replace('\r', '').replace('\n', ''),
        timezone.localtime(created_at, ZoneInfo('Asia/Ho_Chi_Minh')).strftime('%Y-%m-%d %H:%M:%S +07:00'),
    )
    if language.ace == 'pascal':
        header = '{ %s }' % message.replace('}', '')
    elif language.ace == 'ocaml':
        header = '(* %s *)' % message.replace('*)', '')
    elif language.ace in LINE_COMMENTS:
        header = '%s %s' % (LINE_COMMENTS[language.ace], message)
    else:
        raise ValidationError(_('This language does not support an invocation header.'))
    result = header + '\n' + source
    if len(result) > 65536:
        raise ValidationError(_('Source including the invocation header exceeds 65536 characters.'))
    return result


def enqueue_invocation(invocation_id):
    from judge.tasks.invocations import dispatch_invocation
    try:
        dispatch_invocation.delay(str(invocation_id))
    except Exception:
        logger.exception('Could not enqueue invocation %s', invocation_id)
        ProblemInvocationItem.objects.filter(invocation_id=invocation_id).update(
            dispatch_error=_('Could not send to the judging queue. Use Retry dispatch.'))


def create_invocation(user, problem, source_ids, request_key):
    require_invocations(user, problem)
    try:
        request_key = uuid.UUID(str(request_key))
        source_ids = {uuid.UUID(str(value)) for value in source_ids}
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(_('Invalid run request. Reload and try again.')) from exc
    if not source_ids:
        raise ValidationError(_('Select at least one source to run.'))
    with transaction.atomic():
        # Match ordinary submission admission: Profile before Problem.
        profile = Profile.objects.select_for_update().get(user=user)
        with locked_problem(problem.pk) as problem:
            require_invocations(user, problem)
            existing = ProblemInvocation.objects.filter(request_key=request_key).first()
            if existing:
                if existing.created_by_id != user.pk or existing.problem_id != problem.pk:
                    raise Http404()
                return existing
            require_judging_allowed(problem.pk)
            from judge.utils.subtask_scoring import require_submission_admission
            require_submission_admission(problem.pk)
            if not user.is_superuser and problem.banned_users.filter(pk=profile.pk).exists():
                raise ValidationError(_('You are banned from submitting to this problem.'))
            layout = full_layout(problem)
            sources = list(ProblemInvocationSource.objects.select_for_update().filter(
                problem=problem, pk__in=source_ids).select_related('language'))
            if len(sources) != len(source_ids):
                raise ValidationError(_('One or more selected sources no longer exist.'))
            allowed = set(invocation_languages(problem).values_list('pk', flat=True))
            prepared, created_at = [], timezone.now()
            for item in sources:
                if item.language_id not in allowed or not item.expected_overall:
                    raise ValidationError(_('%(name)s: select a language and an overall expectation.'),
                                          params={'name': item.filename})
                validate_expectations(item.expected_overall, item.expected_subtasks, layout)
                if item.expected_subtasks and item.expected_layout != layout:
                    raise ValidationError(_('%(name)s: test groups changed; save the expectations again.'),
                                          params={'name': item.filename})
                prepared.append((item, submission_source(
                    read_source(item.path), item.language, user.username, created_at)))
            invocation = ProblemInvocation.objects.create(
                problem=problem, created_by=user, username=user.username, request_key=request_key, layout=layout)
            # Use the same exact timestamp in the header and invocation record.
            ProblemInvocation.objects.filter(pk=invocation.pk).update(created_at=created_at)
            invocation.created_at = created_at
            for item, source in prepared:
                submission = Submission.objects.create(problem=problem, user=profile, language=item.language)
                SubmissionSource.objects.create(submission=submission, source=source)
                ProblemInvocationItem.objects.create(
                    invocation=invocation, source=item, filename=item.filename,
                    language=item.language, expected_overall=item.expected_overall,
                    expected_subtasks=deepcopy(item.expected_subtasks), submission=submission)
            transaction.on_commit(lambda: enqueue_invocation(invocation.pk))
            return invocation
