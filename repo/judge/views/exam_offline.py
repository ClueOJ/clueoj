from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from judge.models import ExamOfflineAttempt, ExamOfflineSubmission, ExamTag, SubmissionSource
from judge.utils.exam_offline import finish_attempt, result_rows, reveal_attempt, start_attempt
from judge.utils.views import generic_message


@login_required
@require_POST
def start(request, slug):
    exam = get_object_or_404(ExamTag, slug=slug, is_public=True)
    try:
        attempt = start_attempt(request.profile.pk, exam.pk, request.POST.get('day_number', 1))
    except ValidationError as exc:
        return generic_message(request, 'Không thể bắt đầu', ' '.join(exc.messages), status=409)
    return redirect('exam_offline_attempt', attempt_id=attempt.id)


@login_required
@require_POST
def finish(request, attempt_id):
    get_object_or_404(ExamOfflineAttempt, pk=attempt_id, user=request.profile)
    finish_attempt(request.profile.pk, attempt_id, early=True)
    return redirect('exam_offline_attempt', attempt_id=attempt_id)


@login_required
@require_POST
def reveal(request, attempt_id):
    get_object_or_404(ExamOfflineAttempt, pk=attempt_id, user=request.profile)
    try:
        reveal_attempt(request.profile.pk, attempt_id)
    except ValidationError as exc:
        return generic_message(request, 'Chưa thể xem kết quả', ' '.join(exc.messages), status=409)
    return redirect('exam_offline_attempt', attempt_id=attempt_id)


@login_required
def detail(request, attempt_id):
    get_object_or_404(ExamOfflineAttempt, pk=attempt_id, user=request.profile)
    attempt = finish_attempt(request.profile.pk, attempt_id)
    rows, total, pending = result_rows(attempt)
    entries = ExamOfflineSubmission.objects.filter(attempt_problem__attempt=attempt).select_related(
        'submission', 'attempt_problem__problem').order_by('-submission_id')
    history = Paginator(entries, 30).get_page(request.GET.get('page'))
    response = render(request, 'exams/offline-attempt.html', {
        'title': 'Thi thử offline - ' + attempt.exam_name, 'attempt': attempt, 'rows': rows,
        'total': total, 'maximum': sum(row.points for row in rows), 'pending': pending,
        'history': history, 'remaining': max(0, (attempt.deadline - timezone.now()).total_seconds()),
    })
    response['Cache-Control'] = 'private, no-store'
    return response


@login_required
def history(request, slug):
    exam = get_object_or_404(ExamTag, slug=slug)
    attempts = Paginator(ExamOfflineAttempt.objects.filter(user=request.profile, exam=exam), 20).get_page(
        request.GET.get('page'))
    for attempt in attempts:
        _, attempt.score, attempt.pending = result_rows(attempt)
    return render(request, 'exams/offline-history.html', {'title': 'Lịch sử thi — ' + exam.name,
                                                       'exam': exam, 'attempts': attempts})


@login_required
def source(request, submission_id):
    get_object_or_404(ExamOfflineSubmission, submission_id=submission_id,
                     attempt_problem__attempt__user=request.profile)
    code = get_object_or_404(SubmissionSource, submission_id=submission_id)
    response = HttpResponse(code.source, content_type='text/plain; charset=utf-8')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def guard(request, match):
    """An allowlist makes newly added stats/API pages fail closed during a session."""
    attempt = getattr(request, 'offline_attempt', None)
    if not attempt:
        return None
    # Staff may test/administer during an attempt; Django admin still enforces
    # its own active-user and per-model permissions. Match the namespace, not a URL prefix.
    if request.user.is_staff and 'admin' in match.namespaces:
        return None
    name, kwargs = match.url_name or '', match.kwargs
    if name in ('all_submissions', 'all_user_submissions', 'chronological_submissions', 'user_submissions'):
        return None
    if name == 'submission_status' and ExamOfflineSubmission.objects.filter(
            submission_id=kwargs.get('submission'), attempt_problem__attempt=attempt).exists():
        return None
    if name in ('exam_offline_attempt', 'exam_offline_finish') and kwargs.get('attempt_id') == attempt.id:
        return None
    if name == 'exam_offline_source' and ExamOfflineSubmission.objects.filter(
            submission_id=kwargs.get('submission_id'), attempt_problem__attempt=attempt).exists():
        return None
    if name in ('auth_logout', 'auth_login', 'login_2fa', 'webauthn_assert', 'language_template_ajax',
                'password_change', 'password_change_done', 'javascript-catalog'):
        return None
    if name in ('problem_detail', 'problem_raw', 'problem_pdf', 'problem_submit'):
        row = attempt.problems.select_related('problem').filter(problem__code=kwargs.get('problem')).first()
        if row:
            if kwargs.get('submission') and not ExamOfflineSubmission.objects.filter(
                    submission_id=kwargs['submission'], attempt_problem=row).exists():
                return generic_message(request, 'Đang thi offline', 'Chỉ được dùng bài nộp của phiên này.', status=403)
            return None
    return generic_message(request, 'Đang thi offline',
                           'Trang này tạm khóa trong phiên thi. Hãy quay lại phiên hoặc kết thúc phiên để tiếp tục.',
                           status=403)
