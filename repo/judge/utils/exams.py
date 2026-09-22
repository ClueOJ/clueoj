import fcntl
import json
from contextlib import contextmanager
import os
import tempfile

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from judge.models import ExamScoreMilestone, ExamTag, ExamTagProblemPoint
from judge.utils.score_milestones import serialize_score_reference

SNAPSHOT_SCHEMA_VERSION = 2


def exams_snapshot_root():
    return getattr(
        settings,
        'CLUE_EXAMS_SNAPSHOT_ROOT',
        getattr(settings, 'VNOJ_EXAMS_SNAPSHOT_ROOT', '/cache/exams'),
    )


def exams_index_path():
    return os.path.join(exams_snapshot_root(), 'index.json')


def exam_detail_path(slug):
    return os.path.join(exams_snapshot_root(), 'detail', f'{slug}.json')


def _ensure_parent(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)


def _atomic_write_json(path, payload):
    _ensure_parent(path)
    with tempfile.NamedTemporaryFile('w', dir=os.path.dirname(path), delete=False, encoding='utf-8') as tmp:
        json.dump(payload, tmp, ensure_ascii=False, separators=(',', ':'))
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)


def _status_from_counts(available_count, expected_count):
    if available_count <= 0:
        return 'missing'
    if expected_count > 0 and available_count >= expected_count:
        return 'complete'
    return 'updating'


def _progress_text(available_count, expected_count):
    if expected_count > 0:
        return f'{available_count}/{expected_count}'
    return str(available_count)


@contextmanager
def snapshot_build_lock():
    # All web/worker processes share the snapshot volume. OS-owned locks are
    # released on process death, have no expiring lease, and cannot be deleted
    # by an older worker. Never unlink the lock file.
    path = os.path.join(exams_snapshot_root(), '.build.lock')
    _ensure_parent(path)
    with open(path, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def build_exam_snapshots():
    with snapshot_build_lock():
        return _build_exam_snapshots()


def _build_exam_snapshots():
    public_exam_problem_points = (
        ExamTagProblemPoint.objects
        .filter(problem__is_public=True, problem__is_organization_private=False)
        .select_related('problem')
        .only('exam_tag_id', 'points', 'sort_order', 'day_number', 'problem__code', 'problem__name', 'problem__source')
        .order_by('sort_order', 'problem__code')
    )

    exams = (
        ExamTag.objects
        .filter(is_public=True)
        .select_related('category')
        .prefetch_related(
            Prefetch('problem_points', queryset=public_exam_problem_points),
            Prefetch('score_milestones', queryset=ExamScoreMilestone.objects.filter(is_active=True),
                     to_attr='active_score_milestones'),
        )
        .order_by('sort_order', 'name', 'slug')
    )

    now = timezone.now()
    generated_at = now.isoformat()
    detail_entries = {}
    summary = {
        'total': 0,
        'complete': 0,
        'updating': 0,
        'missing': 0,
    }
    items = []

    for exam in exams:
        problem_points = list(exam.problem_points.all())
        available_count = len(problem_points)
        expected_count = exam.expected_count
        total_points = round(sum(item.points or 0 for item in problem_points), 3)
        status = _status_from_counts(available_count, expected_count)
        progress_text = _progress_text(available_count, expected_count)
        summary['total'] += 1
        summary[status] += 1

        item = {
            'id': exam.id,
            'score_reference': serialize_score_reference(exam),
            'duration_minutes': exam.duration_minutes,
            'day_count': exam.day_count,
            'virtual_offline_enabled': exam.virtual_offline_enabled,
            'slug': exam.slug,
            'name': exam.name,
            'year': exam.year,
            'exam_date': exam.exam_date.isoformat() if exam.exam_date else '',
            'category': exam.category.name if exam.category_id else '',
            'exam_type': exam.exam_type,
            'province': exam.province,
            'status_note': exam.status_note,
            'expected_count': expected_count,
            'available_count': available_count,
            'total_points': total_points,
            'status': status,
            'progress_text': progress_text,
            'detail_url': f'/exams/{exam.slug}/',
        }
        items.append(item)
        detail_entries[exam.slug] = {
            **item,
            'schema_version': SNAPSHOT_SCHEMA_VERSION,
            'generated_at': generated_at,
            'problems': [
                {
                    'code': point.problem.code,
                    'name': point.problem.name,
                    'source': point.problem.source,
                    'day_number': point.day_number,
                    'exam_points': round(point.points or 0, 3),
                    'url': f'/problem/{point.problem.code}',
                } for point in problem_points
            ],
        }

    index_payload = {
        'schema_version': SNAPSHOT_SCHEMA_VERSION,
        'generated_at': generated_at,
        'summary': summary,
        'items': items,
    }
    _atomic_write_json(exams_index_path(), index_payload)
    for slug, payload in detail_entries.items():
        _atomic_write_json(exam_detail_path(slug), payload)

    # Remove stale detail snapshots for tags that no longer exist.
    detail_dir = os.path.join(exams_snapshot_root(), 'detail')
    if os.path.isdir(detail_dir):
        valid_filenames = {f'{slug}.json' for slug in detail_entries.keys()}
        for filename in os.listdir(detail_dir):
            if not filename.endswith('.json') or filename in valid_filenames:
                continue
            stale_path = os.path.join(detail_dir, filename)
            if os.path.isfile(stale_path):
                os.unlink(stale_path)
    return index_payload


def load_exam_index_snapshot():
    path = exams_index_path()
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_exam_detail_snapshot(slug):
    path = exam_detail_path(slug)
    if not os.path.exists(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_current_exam_snapshot(slug=None):
    def load():
        return load_exam_detail_snapshot(slug) if slug is not None else load_exam_index_snapshot()

    data = load()
    if data is not None and data.get('schema_version') == SNAPSHOT_SCHEMA_VERSION:
        return data
    # Recheck after taking the same lock used by workers, so simultaneous
    # requests for a legacy snapshot only cause one rebuild.
    with snapshot_build_lock():
        data = load()
        if data is not None and data.get('schema_version') == SNAPSHOT_SCHEMA_VERSION:
            return data
        index = load_exam_index_snapshot()
        if (slug is not None and data is None and index is not None
                and index.get('schema_version') == SNAPSHOT_SCHEMA_VERSION
                and not any(item['slug'] == slug for item in index['items'])):
            return None
        _build_exam_snapshots()
        return load()
