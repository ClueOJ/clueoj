"""Offline exam lifecycle. Lock order: Profile, then attempt, then submission.

Every transition and submission admission uses the same Profile row lock, shared
with ContestJoin/Leave. Judge work is dispatched only after admission commits.
"""
from datetime import timedelta
from functools import wraps

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from judge.models import (ExamOfflineAttempt, ExamOfflineAttemptProblem, ExamOfflineSubmission,
                          ExamTag, Profile, Submission)


def active_attempt(user_id):
    return ExamOfflineAttempt.objects.filter(active_user_id=user_id).first()


def _finish(attempt, now, early=False):
    if attempt.ended_at is not None:
        return attempt
    attempt.ended_at = min(now, attempt.deadline)
    attempt.end_reason = 'early' if early and now < attempt.deadline else 'timeout'
    attempt.active_user = None
    attempt.save(update_fields=['ended_at', 'end_reason', 'active_user'])
    return attempt


def reveal_attempt(user_id, attempt_id):
    with transaction.atomic():
        Profile.objects.select_for_update().get(pk=user_id)
        attempt = ExamOfflineAttempt.objects.get(pk=attempt_id, user_id=user_id)
        if not attempt.ended_at:
            raise ValidationError('Hãy kết thúc phiên trước khi xem kết quả.')
        if attempt.revealed_at:
            return attempt
        attempt.revealed_at = timezone.now()
        attempt.save(update_fields=['revealed_at'])
        Submission.objects.filter(offline_entry__attempt_problem__attempt=attempt).update(offline_hidden=False)
        from django.core.cache import cache
        from judge.tasks.exam_offline import refresh_offline_progress
        def published():
            cache.delete_many(['user_complete:%d' % user_id, 'user_attempted:%s' % user_id])
            refresh_offline_progress.delay(attempt.id)
        transaction.on_commit(published)
        return attempt


def expire_locked(user_id):
    attempt = active_attempt(user_id)
    if attempt and attempt.deadline <= timezone.now():
        _finish(attempt, timezone.now())
        return None
    return attempt


def locked_contest_transition(method):
    @wraps(method)
    def wrapped(self, request, *args, **kwargs):
        from judge.utils.views import generic_message
        with transaction.atomic():
            request.profile = Profile.objects.select_for_update().get(pk=request.profile.pk)
            if expire_locked(request.profile.pk):
                return generic_message(request, 'Đang thi offline',
                                       'Hãy kết thúc phiên offline trước khi vào contest.', status=409)
            request.profile.update_contest()
            return method(self, request, *args, **kwargs)
    return wrapped


def start_attempt(user_id, exam_id, day_number=1):
    with transaction.atomic():
        profile = Profile.objects.select_for_update().get(pk=user_id)
        profile.update_contest()
        if profile.current_contest_id:
            raise ValidationError('Bạn phải thoát contest trước khi bắt đầu thi offline.')
        if expire_locked(user_id):
            raise ValidationError('Bạn đang có một phiên offline. Hãy kết thúc phiên đó trước.')
        exam = ExamTag.objects.get(pk=exam_id, is_public=True)
        if not exam.virtual_offline_enabled or not exam.duration_minutes:
            raise ValidationError('Đề này chưa bật thi offline hoặc chưa có thời lượng hợp lệ.')
        configs = exam.problem_points.all()
        if exam.day_count:
            try:
                day_number = int(day_number)
            except (ValueError, TypeError):
                raise ValidationError('Ngày thi không hợp lệ.')
            if not 1 <= day_number <= exam.day_count:
                raise ValidationError('Ngày thi không nằm trong cấu hình của đề.')
            configs = configs.filter(day_number=day_number)
        else:
            day_number = 0
        configs = list(configs.select_related('problem').order_by('sort_order', 'problem__code'))
        if not configs:
            raise ValidationError('Phần thi được chọn chưa có bài.')
        if any(not row.problem.is_accessible_by(profile.user) or row.problem.has_external_problem for row in configs):
            raise ValidationError('Thi offline yêu cầu mọi bài có quyền truy cập và dùng bộ chấm trên ClueOJ.')
        now = timezone.now()
        attempt = ExamOfflineAttempt.objects.create(user=profile, active_user=profile, exam=exam,
                    exam_name=exam.name, day_number=day_number, started_at=now, deadline=now + timedelta(minutes=exam.duration_minutes))
        ExamOfflineAttemptProblem.objects.bulk_create([
            ExamOfflineAttemptProblem(attempt=attempt, problem=row.problem, points=row.points,
                                      partial=row.problem.partial, sort_order=i)
            for i, row in enumerate(configs)
        ])
        return attempt


def finish_attempt(user_id, attempt_id, early=False):
    with transaction.atomic():
        Profile.objects.select_for_update().get(pk=user_id)
        attempt = ExamOfflineAttempt.objects.get(pk=attempt_id, user_id=user_id)
        if early or timezone.now() >= attempt.deadline:
            _finish(attempt, timezone.now(), early=early)
        return attempt


def admit_submission(profile, problem, expected_attempt):
    """Called with the Profile lock held; no writes before validation succeeds."""
    attempt = active_attempt(profile.pk)
    if expected_attempt:
        if not attempt or str(attempt.id) != str(expected_attempt) or timezone.now() >= attempt.deadline:
            raise ValidationError('Phiên thi đã kết thúc. Bài nộp này không được nhận vào phiên.')
    if attempt:
        if timezone.now() >= attempt.deadline:
            raise ValidationError('Phiên thi đã hết giờ. Hãy mở trang kết quả trước khi nộp luyện tập.')
        if profile.current_contest_id:
            raise ValidationError('Không thể đồng thời nộp vào contest và phiên offline.')
        row = attempt.problems.filter(problem=problem).first()
        if row is None:
            raise ValidationError('Hãy kết thúc phiên offline trước khi nộp bài ngoài đề.')
        return row
    return None


def link_submission(row, submission):
    if submission.date >= row.attempt.deadline:
        raise ValidationError('Phiên thi đã hết giờ; bài nộp không được nhận.')
    ExamOfflineSubmission.objects.create(attempt_problem=row, submission=submission)
    row.final_submission = submission
    row.save(update_fields=['final_submission'])


def result_rows(attempt):
    from judge.tasks.exams import _compute_progress_points
    rows = list(attempt.problems.select_related('problem', 'final_submission'))
    total = 0
    pending = False
    for row in rows:
        sub = row.final_submission
        row.earned = None
        row.verdict = 'Chưa nộp'
        if attempt.revealed_at:
            if sub is None:
                row.earned = 0
            elif sub.status in ('QU', 'P', 'G', 'IE'):
                row.verdict = 'Lỗi hệ thống — cần chấm lại' if sub.status == 'IE' else 'Đang chấm'
                pending = True
            else:
                row.verdict = sub.result or sub.status
                row.earned = (_compute_progress_points(sub.case_points, sub.case_total, row.points, row.partial)
                              if sub.status == 'D' else 0)
            total += row.earned or 0
    return rows, round(total, 3), pending


def locked_submission(method):
    @wraps(method)
    def wrapped(self, form):
        from judge.utils.views import generic_message
        try:
            with transaction.atomic():
                original_contest = self.request.profile.current_contest_id
                profile = Profile.objects.select_for_update().get(pk=self.request.profile.pk)
                profile.update_contest()
                if profile.current_contest_id != original_contest:
                    raise ValidationError('Lượt thi đã thay đổi. Hãy tải lại trang nộp bài.')
                self.request.profile = profile
                row = admit_submission(profile, self.object, self.request.POST.get('offline_attempt') or
                                       getattr(getattr(self.request, 'offline_attempt', None), 'id', None))
                if row and not form.cleaned_data['language'].key.startswith('CPP'):
                    raise ValidationError('Phiên thi offline chỉ nhận mã nguồn C++.')
                self.offline_problem = row
                return method(self, form)
        except ValidationError as exc:
            return generic_message(self.request, 'Không thể nhận bài', ' '.join(exc.messages), status=409)
    return wrapped
