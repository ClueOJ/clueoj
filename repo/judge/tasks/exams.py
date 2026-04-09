from celery import shared_task
from django.core.cache import cache

from judge.models import ExamTagProblemPoint, ExamUserProgress, Problem, Submission
from judge.utils.celery import Progress
from judge.utils.exams import build_exam_snapshots

__all__ = (
    'rebuild_exams_snapshots',
    'sync_exam_progress_for_submission',
    'sync_exam_progress_for_user_problem',
    'rebuild_exam_progress_for_exam',
)


def _clamp_percent(value):
    return max(0.0, min(100.0, value))


def _load_exam_problem_configs(exam_tag_id):
    rows = list(
        ExamTagProblemPoint.objects
        .filter(
            exam_tag_id=exam_tag_id,
            problem__is_public=True,
            problem__is_organization_private=False,
        )
        .values_list('problem_id', 'points', 'problem__partial'),
    )
    problem_configs = {
        problem_id: {
            'points': float(points or 0),
            'partial': bool(is_partial),
        }
        for problem_id, points, is_partial in rows
    }
    total_points = round(sum(config['points'] for config in problem_configs.values()), 3)
    return problem_configs, total_points


def _compute_progress_points(case_points, case_total, exam_problem_points, is_partial):
    earned_points = round((case_points / case_total) * exam_problem_points if case_total > 0 else 0, 3)
    earned_points = min(earned_points, exam_problem_points)
    if not is_partial and earned_points != exam_problem_points:
        return 0
    return earned_points


def _sync_exam_tag_relations_for_problem(problem_id):
    through_model = Problem.exam_tags.through
    through_exam_tag_ids = set(
        through_model.objects
        .filter(problem_id=problem_id)
        .values_list('examtag_id', flat=True)
        .distinct(),
    )
    point_exam_tag_ids = set(
        ExamTagProblemPoint.objects
        .filter(problem_id=problem_id)
        .values_list('exam_tag_id', flat=True)
        .distinct(),
    )

    missing_through_exam_tag_ids = point_exam_tag_ids - through_exam_tag_ids
    if missing_through_exam_tag_ids:
        through_model.objects.bulk_create(
            [
                through_model(problem_id=problem_id, examtag_id=exam_tag_id)
                for exam_tag_id in missing_through_exam_tag_ids
            ],
            ignore_conflicts=True,
        )

    missing_point_exam_tag_ids = through_exam_tag_ids - point_exam_tag_ids
    if missing_point_exam_tag_ids:
        default_points = float(Problem.objects.filter(id=problem_id).values_list('points', flat=True).first() or 0)
        ExamTagProblemPoint.objects.bulk_create(
            [
                ExamTagProblemPoint(
                    exam_tag_id=exam_tag_id,
                    problem_id=problem_id,
                    points=default_points,
                )
                for exam_tag_id in missing_point_exam_tag_ids
            ],
            ignore_conflicts=True,
        )

    return sorted(through_exam_tag_ids | point_exam_tag_ids)


def _sync_exam_tag_relations_for_exam(exam_tag_id):
    through_model = Problem.exam_tags.through
    through_problem_ids = set(
        through_model.objects
        .filter(examtag_id=exam_tag_id)
        .values_list('problem_id', flat=True)
        .distinct(),
    )
    point_problem_ids = set(
        ExamTagProblemPoint.objects
        .filter(exam_tag_id=exam_tag_id)
        .values_list('problem_id', flat=True)
        .distinct(),
    )

    missing_through_problem_ids = point_problem_ids - through_problem_ids
    if missing_through_problem_ids:
        through_model.objects.bulk_create(
            [
                through_model(problem_id=problem_id, examtag_id=exam_tag_id)
                for problem_id in missing_through_problem_ids
            ],
            ignore_conflicts=True,
        )

    missing_point_problem_ids = through_problem_ids - point_problem_ids
    if missing_point_problem_ids:
        problem_points = dict(
            Problem.objects
            .filter(id__in=missing_point_problem_ids)
            .values_list('id', 'points'),
        )
        ExamTagProblemPoint.objects.bulk_create(
            [
                ExamTagProblemPoint(
                    exam_tag_id=exam_tag_id,
                    problem_id=problem_id,
                    points=float(problem_points.get(problem_id, 0) or 0),
                )
                for problem_id in missing_point_problem_ids
            ],
            ignore_conflicts=True,
        )


def _sync_user_exam_progress(user_id, exam_tag_id, problem_configs=None, total_points=None):
    if problem_configs is None or total_points is None:
        problem_configs, total_points = _load_exam_problem_configs(exam_tag_id)

    if not problem_configs or total_points <= 0:
        ExamUserProgress.objects.filter(user_id=user_id, exam_tag_id=exam_tag_id).delete()
        return False

    problem_ids = tuple(problem_configs.keys())
    best_points_by_problem = {}
    submissions = (
        Submission.objects
        .filter(
            user_id=user_id,
            problem_id__in=problem_ids,
            status='D',
        )
        .only('problem_id', 'case_points', 'case_total')
    )
    for submission in submissions.iterator():
        config = problem_configs.get(submission.problem_id)
        if config is None:
            continue
        submission_points = _compute_progress_points(
            case_points=submission.case_points,
            case_total=submission.case_total,
            exam_problem_points=config['points'],
            is_partial=config['partial'],
        )
        previous_points = best_points_by_problem.get(submission.problem_id, 0)
        if submission_points > previous_points:
            best_points_by_problem[submission.problem_id] = submission_points

    earned_points = round(sum(best_points_by_problem.values()), 3)
    percent = _clamp_percent(round(earned_points / total_points * 100, 1))
    ExamUserProgress.objects.update_or_create(
        user_id=user_id,
        exam_tag_id=exam_tag_id,
        defaults={
            'earned_points': earned_points,
            'total_points': total_points,
            'percent': percent,
        },
    )
    return True


def _exam_tag_ids_for_problem(problem_id):
    return _sync_exam_tag_relations_for_problem(problem_id)


def _sync_exam_progress_for_user_problem(user_id, problem_id):
    exam_tag_ids = _exam_tag_ids_for_problem(problem_id)
    if not exam_tag_ids:
        return 0

    updated = 0
    for exam_tag_id in exam_tag_ids:
        if _sync_user_exam_progress(user_id=user_id, exam_tag_id=exam_tag_id):
            updated += 1
    return updated


@shared_task(bind=True)
def rebuild_exams_snapshots(self):
    try:
        payload = build_exam_snapshots()
        return payload['summary']['total']
    finally:
        cache.delete('exams:snapshot:queued')


@shared_task(bind=True)
def sync_exam_progress_for_submission(self, submission_id):
    submission = (
        Submission.objects
        .filter(id=submission_id)
        .only('id', 'user_id', 'problem_id')
        .first()
    )
    if submission is None:
        return 0
    return _sync_exam_progress_for_user_problem(user_id=submission.user_id, problem_id=submission.problem_id)


@shared_task(bind=True)
def sync_exam_progress_for_user_problem(self, user_id, problem_id):
    return _sync_exam_progress_for_user_problem(user_id=user_id, problem_id=problem_id)


@shared_task(bind=True)
def rebuild_exam_progress_for_exam(self, exam_tag_id):
    try:
        _sync_exam_tag_relations_for_exam(exam_tag_id)
        problem_configs, total_points = _load_exam_problem_configs(exam_tag_id)
        problem_ids = tuple(problem_configs.keys())
        submission_user_ids = set()
        if problem_ids:
            submission_user_ids = set(
                Submission.objects
                .filter(problem_id__in=problem_ids)
                .values_list('user_id', flat=True)
                .distinct(),
            )
        existing_user_ids = set(
            ExamUserProgress.objects
            .filter(exam_tag_id=exam_tag_id)
            .values_list('user_id', flat=True),
        )
        user_ids = sorted(submission_user_ids | existing_user_ids)

        if not user_ids:
            return 0

        with Progress(self, len(user_ids), stage='Syncing exam progress') as progress:
            updated = 0
            for index, user_id in enumerate(user_ids, 1):
                if _sync_user_exam_progress(
                    user_id=user_id,
                    exam_tag_id=exam_tag_id,
                    problem_configs=problem_configs,
                    total_points=total_points,
                ):
                    updated += 1
                if index % 10 == 0:
                    progress.done = index
            return updated
    finally:
        cache.delete(f'exams:progress:queued:{exam_tag_id}')
