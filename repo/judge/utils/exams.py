import json
import os
import tempfile

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from judge.models import ExamTag, ExamTagProblemPoint


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


def build_exam_snapshots():
    public_exam_problem_points = (
        ExamTagProblemPoint.objects
        .filter(problem__is_public=True, problem__is_organization_private=False)
        .select_related('problem')
        .only('exam_tag_id', 'points', 'sort_order', 'problem__code', 'problem__name', 'problem__source')
        .order_by('sort_order', 'problem__code')
    )

    exams = (
        ExamTag.objects
        .filter(is_public=True)
        .select_related('category')
        .prefetch_related(Prefetch('problem_points', queryset=public_exam_problem_points))
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
            'generated_at': generated_at,
            'problems': [
                {
                    'code': point.problem.code,
                    'name': point.problem.name,
                    'source': point.problem.source,
                    'exam_points': round(point.points or 0, 3),
                    'url': f'/problem/{point.problem.code}',
                } for point in problem_points
            ],
        }

    index_payload = {
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
