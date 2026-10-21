"""Batch admission shares the Profile lock with normal submit/join/offline flows."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from judge.models import (Contest, ContestParticipation, ContestProblem, ContestSubmission,
                          Language, Profile, Submission, SubmissionBatchItem, SubmissionSource)
from judge.utils.exam_offline import active_attempt, finish_attempt


class QueueFull(Exception):
    pass


def ongoing(contest):
    if not contest.start_time <= timezone.now() < contest.end_time:
        raise ValidationError('Contest chưa bắt đầu hoặc đã kết thúc.')


def validate_item(contest, item, profile=None):
    ongoing(contest)
    if profile is None:
        profile = Profile.objects.select_related('user', 'current_contest__contest').filter(
            user__username=item.username).first()
    if profile is None:
        raise ValidationError('User không tồn tại.')
    user = profile.user
    if not user.is_active:
        raise ValidationError('Tài khoản đã bị khóa.')
    if not contest.is_accessible_by(user):
        raise ValidationError('User không có quyền truy cập contest.')
    if contest.banned_users.filter(pk=profile.pk).exists():
        raise ValidationError('User bị cấm tham gia contest.')
    if profile.pk in contest.editor_ids or profile.pk in contest.tester_ids:
        raise ValidationError('Tác giả/tester chỉ được spectate; không thể nộp vào lượt thi chính thức.')
    participation = ContestParticipation.objects.filter(contest=contest, user=profile, virtual=0).first()
    if participation and participation.is_disqualified:
        raise ValidationError('Lượt thi đã bị loại.')
    if participation and not participation.pre_registered and participation.end_time <= timezone.now():
        raise ValidationError('Lượt thi chính thức của user đã hết giờ.')
    if participation is None and contest.require_registration and not contest.can_register:
        raise ValidationError('User chưa đăng ký và contest đã đóng đăng ký.')
    cp = ContestProblem.objects.select_related('problem').filter(
        contest=contest, problem__code=item.problem_code).first()
    if cp is None:
        raise ValidationError('Bài không thuộc contest đã chọn.')
    problem = cp.problem
    if problem.has_external_problem:
        raise ValidationError('Batch hiện chỉ hỗ trợ bài chấm trên ClueOJ.')
    if problem.banned_users.filter(pk=profile.pk).exists():
        raise ValidationError('User bị cấm nộp bài này.')
    language = Language.objects.filter(key=item.language_key).first()
    if language is None or language.key not in ('CPP20', 'PYPY3'):
        raise ValidationError('Không tìm thấy runtime C++20/PyPy3 tương ứng.')
    if not problem.allowed_languages.filter(pk=language.pk).exists():
        raise ValidationError('Bài không cho phép ngôn ngữ đã chọn.')
    if not item.source.strip() or len(item.source) > 65536 or '\x00' in item.source:
        raise ValidationError('Mã nguồn rỗng, chứa byte NUL hoặc dài quá 65536 ký tự.')
    if participation and cp.max_submissions is not None and ContestSubmission.objects.filter(
            participation=participation, problem=cp).count() >= cp.max_submissions:
        raise ValidationError('User đã hết số lần nộp cho bài này.')
    changes = []
    if profile.current_contest_id and profile.current_contest_id != getattr(participation, 'id', None):
        changes.append('Thoát contest ' + profile.current_contest.contest.key)
    attempt = active_attempt(profile.pk)
    if attempt:
        changes.append('Dừng lượt offline #%s' % attempt.pk)
    if profile.current_contest_id != getattr(participation, 'id', None) or participation is None:
        changes.append('Join contest ' + contest.key)
    return profile, participation, cp, language, '; '.join(changes)


def admit_item(item_id):
    # The item lock makes duplicate Celery deliveries harmless. The profile lock
    # serializes different batches and ordinary user submissions/transitions.
    with transaction.atomic():
        item = SubmissionBatchItem.objects.select_for_update().select_related('batch__created_by').get(pk=item_id)
        if item.state != 'pending' or not item.batch.started_at:
            return None
        if not item.batch.created_by.is_active or not item.batch.created_by.is_superuser:
            raise ValidationError('Người tạo batch không còn quyền superuser.')
        profile = Profile.objects.select_for_update().select_related('user', 'current_contest__contest').filter(
            user__username=item.username).first()
        contest = Contest.objects.get(pk=item.batch.contest_id)
        profile, participation, cp, language, changes = validate_item(contest, item, profile)
        if not profile.user.has_perm('judge.spam_submission') and Submission.objects.filter(
                user=profile, rejudged_date__isnull=True).exclude(status__in=['D', 'IE', 'CE', 'AB']).count() >= settings.DMOJ_SUBMISSION_LIMIT:
            raise QueueFull()
        # No state changes until every admission check passes.
        attempt = active_attempt(profile.pk)
        if attempt:
            finish_attempt(profile.pk, attempt.pk, early=True)
        if participation is None:
            participation = ContestParticipation.objects.create(contest=contest, user=profile, virtual=0)
            # Avoid saving a stale Contest object over concurrent admin edits.
            Contest.objects.filter(pk=contest.pk).update(user_count=contest.users.filter(virtual=0).count())
        elif participation.pre_registered:
            participation.real_start = timezone.now()
            participation.save(update_fields=['real_start'])
        profile.current_contest = participation
        profile.save(update_fields=['current_contest'])
        sub = Submission.objects.create(user=profile, problem=cp.problem, language=language,
                                        contest_object=contest, locked_after=contest.locked_after)
        # Check actual admission timestamp; rollback transitions at the deadline.
        if not contest.start_time <= sub.date < min(contest.end_time, participation.end_time):
            raise ValidationError('Contest hoặc lượt thi đã hết giờ trước khi nhận bài.')
        ContestSubmission.objects.create(submission=sub, problem=cp, participation=participation)
        SubmissionSource.objects.create(submission=sub, source=item.source)
        item.submission = sub
        item.state = 'created'
        item.message = 'Đã tạo submission; đang gửi chấm. Nếu bị gián đoạn, chấm lại submission này.'
        item.transition = changes
        item.source = ''  # The canonical source now belongs to SubmissionSource.
        item.save(update_fields=['submission', 'state', 'message', 'transition', 'source'])
    return sub
