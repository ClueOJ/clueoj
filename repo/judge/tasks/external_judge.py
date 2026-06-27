import logging

from celery import shared_task
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.external_judge import (
    external_result_is_processing,
    finalize_external_submission,
    get_external_score_text,
    get_external_status_canonical,
    perform_external_submission,
    raw_response_with_selected_language,
    selected_external_language_mapping,
    set_external_submission_error,
)
from judge.models import ExternalSubmission, Submission
from judge.utils.external_judge_client import (
    ExternalJudgeAuthError,
    ExternalJudgeBadRequestError,
    ExternalJudgeClient,
    ExternalJudgeConfigurationError,
    ExternalJudgeError,
    user_safe_message,
)


logger = logging.getLogger('judge.external_judge')

EXTERNAL_POLL_HARD_TIMEOUT_SECONDS = 30 * 60

NON_RETRYABLE_POLL_ERRORS = (
    ExternalJudgeAuthError,
    ExternalJudgeBadRequestError,
    ExternalJudgeConfigurationError,
)

LOCAL_TERMINAL_STATUSES = {'AB', 'CE', 'IE', 'D'}
LOCAL_TERMINAL_RESULTS = {'AB', 'CE', 'IE', 'D'}
EXTERNAL_TERMINAL_SYSTEM_STATUSES = {
    'config_error',
    'poll_timeout',
    'submit_failed',
}


def _mark_external_poll_error(ext_sub, submission, message):
    set_external_submission_error(submission, message)
    ExternalSubmission.objects.filter(id=ext_sub.id).update(
        pcd_system_status='config_error',
        last_polled_at=timezone.now(),
        updated_at=timezone.now(),
    )


def _local_submission_is_terminal(submission):
    return submission.status in LOCAL_TERMINAL_STATUSES or submission.result in LOCAL_TERMINAL_RESULTS


def _external_submission_is_terminal(ext_sub):
    system_status = str(ext_sub.pcd_system_status or '').strip().lower()
    return system_status in EXTERNAL_TERMINAL_SYSTEM_STATUSES


@shared_task(bind=True, max_retries=None)
def submit_external_submission(
    self, submission_id, rejudge=False, batch_rejudge=False, expected_submission_id=None,
):
    try:
        submission = Submission.objects.select_related(
            'problem',
            'problem__external_problem',
            'language',
            'source',
        ).get(id=submission_id)
    except Submission.DoesNotExist:
        return

    return perform_external_submission(
        submission,
        rejudge=rejudge,
        batch_rejudge=batch_rejudge,
        expected_submission_id=expected_submission_id,
    )


@shared_task(bind=True, max_retries=None)
def poll_external_submission(self, submission_id, expected_submission_id=None):
    try:
        ext_sub = ExternalSubmission.objects.select_related(
            'submission',
            'submission__problem',
            'submission__problem__external_problem',
            'submission__user',
            'submission__language',
            'config',
        ).get(submission_id=submission_id)
    except ExternalSubmission.DoesNotExist:
        return

    if expected_submission_id and str(ext_sub.pcd_submission_id) != str(expected_submission_id):
        logger.info('Ignoring stale external poll task: submission_id=%s', submission_id)
        return

    submission = ext_sub.submission
    if _local_submission_is_terminal(submission) or _external_submission_is_terminal(ext_sub):
        return

    config = ext_sub.config
    if not config or not config.is_active:
        _mark_external_poll_error(ext_sub, submission, _('This problem is temporarily not accepting submissions.'))
        return

    if ext_sub.created_at and (
        (timezone.now() - ext_sub.created_at).total_seconds() > EXTERNAL_POLL_HARD_TIMEOUT_SECONDS
    ):
        set_external_submission_error(
            submission,
            _('External judge did not return a result before the 30-minute timeout.'),
        )
        ext_sub.pcd_system_status = 'poll_timeout'
        ext_sub.save(update_fields=['pcd_system_status', 'updated_at'])
        return

    ext_sub.poll_attempts += 1
    ext_sub.last_polled_at = timezone.now()
    ext_sub.save(update_fields=['poll_attempts', 'last_polled_at', 'updated_at'])

    if ext_sub.poll_attempts > config.max_poll_attempts:
        set_external_submission_error(submission, _('External judge did not return a result before the timeout.'))
        ext_sub.pcd_system_status = 'poll_timeout'
        ext_sub.save(update_fields=['pcd_system_status', 'updated_at'])
        return

    try:
        client = ExternalJudgeClient(config, timeout=config.poll_timeout_seconds)
        data = client.get_submission_status(str(ext_sub.pcd_submission_id))
    except NON_RETRYABLE_POLL_ERRORS as exc:
        logger.warning(
            'External poll failed with non-retryable error: config=%s submission_id=%s status=%s attempt=%s error=%s',
            getattr(config, 'name', '<unknown>'),
            submission.id,
            getattr(exc, 'status_code', None),
            ext_sub.poll_attempts,
            exc,
        )
        _mark_external_poll_error(ext_sub, submission, user_safe_message(exc))
        return
    except ExternalJudgeError as exc:
        logger.warning(
            'External poll failed: config=%s submission_id=%s status=%s attempt=%s error=%s',
            getattr(config, 'name', '<unknown>'),
            submission.id,
            getattr(exc, 'status_code', None),
            ext_sub.poll_attempts,
            exc,
        )
        delay = min(config.poll_interval_seconds * (1.5 ** min(ext_sub.poll_attempts, 5)), 30)
        raise self.retry(countdown=delay)

    submission.refresh_from_db(fields=['status', 'result'])
    ext_sub.refresh_from_db(fields=['pcd_system_status'])
    if _local_submission_is_terminal(submission) or _external_submission_is_terminal(ext_sub):
        return

    ext_sub.pcd_system_status = data.get('systemStatus', ext_sub.pcd_system_status)
    ext_sub.pcd_status_canonical = get_external_status_canonical(data) or None
    ext_sub.pcd_runtime_ms = data.get('runtimeMs')
    ext_sub.pcd_memory_kb = data.get('memoryKb')
    ext_sub.pcd_score_text = get_external_score_text(data) or ext_sub.pcd_score_text
    ext_sub.pcd_remote_url = data.get('remoteUrl') or None
    ext_sub.pcd_vjudge_run_id = data.get('vjudgeRunId') or ext_sub.pcd_vjudge_run_id
    ext_sub.raw_response = raw_response_with_selected_language(
        data,
        selected_external_language_mapping(submission, submission.problem.external_problem, ext_sub),
    )
    ext_sub.save()

    system_status = (data.get('systemStatus') or '').lower()
    still_processing = data.get('processing') is not False
    if (
        still_processing or
        system_status in ('', 'queued', 'polling', 'submitting') or
        external_result_is_processing(data)
    ):
        raise self.retry(countdown=config.poll_interval_seconds)

    if not finalize_external_submission(submission, ext_sub, data):
        raise self.retry(countdown=config.poll_interval_seconds)
