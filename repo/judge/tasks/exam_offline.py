from celery import shared_task
from django.utils import timezone

from judge.models import ExamOfflineAttempt


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def refresh_offline_progress(attempt_id):
    from judge.caching import finished_submission
    from judge.tasks.exams import sync_exam_progress_for_user_problem
    attempt = ExamOfflineAttempt.objects.get(pk=attempt_id)
    if not attempt.revealed_at:
        return
    attempt.user.calculate_points()
    for row in attempt.problems.select_related('problem', 'final_submission'):
        row.problem.update_stats()
        sync_exam_progress_for_user_problem(attempt.user_id, row.problem_id)
        if row.final_submission:
            finished_submission(row.final_submission)


@shared_task
def expire_offline_attempts():
    from judge.utils.exam_offline import finish_attempt
    expired = ExamOfflineAttempt.objects.filter(active_user__isnull=False, deadline__lte=timezone.now())
    for attempt_id, user_id in expired.values_list('id', 'user_id').iterator():
        finish_attempt(user_id, attempt_id)
