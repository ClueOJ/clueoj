import json
import uuid
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from judge.models import Contest, SubmissionBatch, SubmissionBatchItem
from judge.tasks.batch_submit import run_submission_batch
from judge.utils.batch_submit import ongoing, validate_item


def superuser(view):
    @wraps(view)
    @login_required
    @never_cache
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not request.user.is_superuser:
            raise PermissionDenied()
        try:
            return view(request, *args, **kwargs)
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            message = ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Dữ liệu batch không hợp lệ.'
            return JsonResponse({'error': message}, status=400)
    return wrapped


def payload(request):
    if len(request.body) > 2 * 1024 * 1024:
        raise ValidationError('Mỗi lần tải lên tối đa 2 MB.')
    data = json.loads(request.body)
    if not isinstance(data, dict):
        raise ValidationError('Dữ liệu không hợp lệ.')
    return data


@superuser
@require_http_methods(['GET'])
def index(request):
    return render(request, 'batch-submit.html', {
        'title': 'Batch submit',
        'batches': SubmissionBatch.objects.select_related('contest', 'created_by').order_by('-created_at')[:30],
    })


def verified_contest(code):
    if not isinstance(code, str) or not code.strip():
        raise ValidationError('Hãy nhập mã contest.')
    contest = Contest.objects.filter(key=code.strip()).first()
    if contest is None:
        raise ValidationError('Không tìm thấy contest có mã này.')
    now = timezone.now()
    if now < contest.start_time:
        raise ValidationError('Contest chưa bắt đầu.')
    if now >= contest.end_time:
        raise ValidationError('Contest đã kết thúc.')
    if not contest.contest_problems.exists():
        raise ValidationError('Contest chưa có bài để nộp.')
    return contest


@superuser
@require_http_methods(['GET'])
def problems(request):
    contest = verified_contest(request.GET.get('contest', ''))
    return JsonResponse({
        'contest': {'key': contest.key, 'name': contest.name},
        'problems': list(contest.contest_problems.order_by('order').values('problem__code', 'problem__name')),
    })


@superuser
@require_http_methods(['POST'])
def create(request):
    data = payload(request)
    contest = verified_contest(data['contest'])
    count = int(data['count'])
    if not 1 <= count <= 2000:
        raise ValidationError('Một batch nhận từ 1 đến 2000 file.')
    batch, _ = SubmissionBatch.objects.get_or_create(id=uuid.UUID(data['id']), defaults={
        'created_by': request.user, 'contest': contest, 'expected_count': count,
    })
    if batch.created_by_id != request.user.pk or batch.contest_id != contest.pk or batch.expected_count != count:
        raise ValidationError('Mã batch đã được dùng với nội dung khác.')
    return JsonResponse({'id': str(batch.pk)})


@superuser
@require_http_methods(['POST'])
def upload(request, batch_id):
    data = payload(request)
    rows = data['items']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValidationError('Mỗi lần tải tối đa 20 file.')
    with transaction.atomic():
        batch = get_object_or_404(SubmissionBatch.objects.select_for_update(), pk=batch_id)
        if batch.started_at or batch.created_by_id != request.user.pk:
            raise ValidationError('Batch đã chạy hoặc không thuộc người tạo hiện tại.')
        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError('Dòng dữ liệu không hợp lệ.')
            path = row['path']
            if not isinstance(path, str) or len(path) > 512 or '\\' in path:
                raise ValidationError('Đường dẫn file không hợp lệ.')
            parts = path.split('/')
            if len(parts) != 3 or any(not p or p in ('.', '..') or '\x00' in p for p in parts):
                raise ValidationError('Cấu trúc cần là sources/<username>/<file>.')
            username, filename = parts[1:]
            filename = filename.lower()
            path = '/'.join([parts[0], username, filename])
            extension = filename.rsplit('.', 1)[-1]
            language = {'cpp': 'CPP20', 'py': 'PYPY3'}.get(extension)
            code, source = row['problem'], row['source']
            if not language or len(username) > 150 or not isinstance(code, str) or len(code) > 128 or not isinstance(source, str) or len(source) > 65536:
                raise ValidationError('Tên user, bài, loại file hoặc độ dài mã nguồn không hợp lệ.')
            previous = batch.items.filter(path=path).first()
            if previous:
                if previous.problem_code != code or previous.source != source:
                    raise ValidationError('File trùng đường dẫn nhưng khác nội dung/gán bài.')
                continue
            item = SubmissionBatchItem(batch=batch, path=path, username=username, problem_code=code,
                                       language_key=language, source=source)
            try:
                _, _, _, _, item.transition = validate_item(batch.contest, item)
            except ValidationError as exc:
                item.state, item.message = 'invalid', ' '.join(exc.messages)
            item.save()
        if batch.items.count() > batch.expected_count:
            raise ValidationError('Số file vượt quá số lượng khai báo.')
    return JsonResponse({'ok': True})


@superuser
@require_http_methods(['GET'])
def detail(request, batch_id):
    batch = get_object_or_404(SubmissionBatch.objects.select_related('contest', 'created_by'), pk=batch_id)
    rows = list(batch.items.select_related('submission').defer('source', 'submission__error'))
    pending = sum(row.state == 'pending' for row in rows)
    if request.GET.get('format') == 'json':
        return JsonResponse({'pending': pending, 'started': bool(batch.started_at), 'items': [{
            'id': row.pk, 'state': row.state, 'message': row.message, 'transition': row.transition,
            'submission': row.submission_id,
            'result': (row.submission.result or row.submission.status) if row.submission_id else '',
        } for row in rows]})
    return render(request, 'batch-submit-detail.html', {
        'title': 'Batch submit — ' + batch.contest.key, 'batch': batch, 'rows': rows,
        'pending': pending, 'ready': len(rows) == batch.expected_count,
    })


@superuser
@require_http_methods(['POST'])
def start(request, batch_id):
    with transaction.atomic():
        batch = get_object_or_404(SubmissionBatch.objects.select_for_update().select_related('contest'), pk=batch_id)
        if batch.created_by_id != request.user.pk:
            raise PermissionDenied()
        ongoing(batch.contest)
        if batch.items.count() != batch.expected_count:
            raise ValidationError('Chưa tải đủ file. Quay về trang chọn thư mục để tạo batch đầy đủ.')
        if not batch.started_at:
            if not batch.items.filter(state='pending').exists():
                raise ValidationError('Không có file hợp lệ để nộp.')
            batch.started_at = timezone.now()
            batch.save(update_fields=['started_at'])
    # Redelivering only visits pending rows, never creates a second submission.
    try:
        run_submission_batch.delay(str(batch.pk))
    except Exception:
        return JsonResponse({'error': 'Không kết nối được hàng đợi. Bấm Tiếp tục để thử lại; batch đã được lưu.'}, status=503)
    return JsonResponse({'ok': True})
