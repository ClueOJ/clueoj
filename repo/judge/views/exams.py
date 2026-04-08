from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from judge.models import ExamCategory, ExamProvince, ExamTag, ExamUserProgress, Problem, Submission
from judge.tasks import rebuild_exams_snapshots
from judge.utils.celery import task_status_url_by_id
from judge.utils.exams import build_exam_snapshots, load_exam_detail_snapshot, load_exam_index_snapshot
from judge.utils.views import TitleMixin


STATUS_LABELS = {
    'complete': _('Đã có'),
    'updating': _('Đang cập nhật'),
    'missing': _('Không sở hữu'),
}


def _format_points(value):
    text = f'{float(value or 0):.3f}'.rstrip('0').rstrip('.')
    return text or '0'


def _normalize_percent(value):
    return max(0.0, min(100.0, round(float(value or 0), 1)))


def _compute_progress_points(case_points, case_total, exam_problem_points, is_partial):
    earned_points = round((case_points / case_total) * exam_problem_points if case_total > 0 else 0, 3)
    earned_points = min(earned_points, exam_problem_points)
    if not is_partial and earned_points != exam_problem_points:
        return 0
    return earned_points


def _normalize_payload():
    payload = load_exam_index_snapshot()
    if payload is None:
        payload = build_exam_snapshots()
    elif payload.get('items') and 'total_points' not in payload['items'][0]:
        payload = build_exam_snapshots()

    for item in payload.get('items', []):
        item['status_label'] = str(STATUS_LABELS.get(item['status'], item['status']))
    return payload


def _load_detail_payload(slug):
    data = load_exam_detail_snapshot(slug)
    if data is None:
        # Snapshot may be briefly stale while worker is rebuilding.
        build_exam_snapshots()
        data = load_exam_detail_snapshot(slug)
    return data


class ExamsListView(TitleMixin, TemplateView):
    template_name = 'exams/list.html'
    title = _('Thư viện đề thi')

    def _selected_category(self):
        return (
            self.request.GET.get('exam_category', '').strip() or
            self.request.GET.get('exam_type', '').strip()  # Legacy query param compatibility.
        )

    def _category_choices(self, selected_value):
        categories = list(
            ExamCategory.objects
            .filter(is_active=True)
            .order_by('sort_order', 'name')
            .values_list('name', flat=True)
        )
        if selected_value and selected_value not in categories:
            categories.append(selected_value)
        return [('', str(_('Tất cả')))] + [(value, value) for value in categories]

    def _province_choices(self, items, selected_value):
        provinces = list(
            ExamProvince.objects
            .filter(is_active=True)
            .order_by('sort_order', 'name')
            .values_list('name', flat=True)
        )
        if selected_value and selected_value not in provinces:
            provinces.append(selected_value)
        return [('', str(_('Tất cả')))] + [(value, value) for value in provinces]

    def _selected_year(self):
        return self.request.GET.get('year', '').strip()

    def _year_choices(self, items, selected_value):
        years = sorted({int(item['year']) for item in items if item.get('year') is not None}, reverse=True)
        choices = [('', str(_('Tất cả')))] + [(str(year), str(year)) for year in years]
        if selected_value and selected_value not in {value for value, _ in choices}:
            choices.append((selected_value, selected_value))
        return choices

    def _filter_items(self, items):
        keyword = self.request.GET.get('keyword', '').strip().lower()
        category = self._selected_category().lower()
        province = self.request.GET.get('province', '').strip()
        year = self._selected_year()

        def matched(item):
            if keyword:
                haystack = ' '.join([
                    item.get('name', ''),
                    item.get('category', ''),
                    item.get('province', ''),
                    item.get('exam_type', ''),
                    item.get('status_note', ''),
                ]).lower()
                if keyword not in haystack:
                    return False
            if category and category != (item.get('category', '') or '').lower():
                return False
            if province and province != (item.get('province', '') or ''):
                return False
            if year and str(item.get('year') or '') != year:
                return False
            return True

        return [item for item in items if matched(item)]

    def _decorate_item(self, item):
        exam_kind = item.get('category') or item.get('exam_type') or ''
        meta_parts = []
        if item.get('year'):
            meta_parts.append(str(item['year']))
        if exam_kind:
            meta_parts.append(exam_kind)
        if item.get('province'):
            meta_parts.append(item['province'])
        item['exam_kind'] = exam_kind
        item['meta_line'] = ' · '.join(meta_parts)

    def _sort_items(self, items):
        year_sort = self.request.GET.get('year_sort', '').strip().lower()
        if year_sort not in ('asc', 'desc'):
            return items

        if year_sort == 'asc':
            return sorted(
                items,
                key=lambda item: (
                    item.get('year') is None,
                    item.get('year') or 0,
                    item.get('name', ''),
                ),
            )

        return sorted(
            items,
            key=lambda item: (
                item.get('year') is None,
                -(item.get('year') or 0),
                item.get('name', ''),
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payload = _normalize_payload()
        items = payload.get('items', [])
        filtered_items = [dict(item) for item in self._sort_items(self._filter_items(items))]
        for item in filtered_items:
            self._decorate_item(item)

        if self.request.user.is_authenticated:
            exam_ids = [item['id'] for item in filtered_items if item.get('id')]
            progress_rows = ExamUserProgress.objects.filter(
                user_id=self.request.user.profile.id,
                exam_tag_id__in=exam_ids,
            )
            progress_by_exam = {row.exam_tag_id: row for row in progress_rows}
            for item in filtered_items:
                total_points = float(item.get('total_points') or 0)
                progress = progress_by_exam.get(item.get('id'))
                if progress is not None:
                    earned_points = float(progress.earned_points or 0)
                    total_for_display = float(progress.total_points or total_points)
                    if total_for_display <= 0:
                        total_for_display = total_points
                    percent = _normalize_percent(progress.percent)
                else:
                    earned_points = 0.0
                    total_for_display = total_points
                    percent = 0.0
                if total_for_display > 0:
                    text = f'{_format_points(earned_points)}/{_format_points(total_for_display)} · {percent:.1f}%'
                else:
                    text = f'{_format_points(earned_points)} · 0.0%'
                item['user_progress'] = {
                    'earned_points': earned_points,
                    'total_points': total_for_display,
                    'percent': percent,
                    'percent_css': f'{percent:.1f}',
                    'text': text,
                }
        else:
            for item in filtered_items:
                item['user_progress'] = None

        context['summary'] = payload.get('summary', {})
        context['items'] = filtered_items
        context['total_pages'] = 1
        context['current_page'] = 1
        selected_category = self._selected_category()
        selected_province = self.request.GET.get('province', '').strip()
        selected_year = self._selected_year()
        context['category_choices'] = self._category_choices(selected_category)
        context['province_choices'] = self._province_choices(items, selected_province)
        context['year_choices'] = self._year_choices(items, selected_year)
        context['filters'] = {
            'keyword': self.request.GET.get('keyword', '').strip(),
            'exam_category': selected_category,
            'province': selected_province,
            'year': selected_year,
            'year_sort': self.request.GET.get('year_sort', '').strip(),
        }
        context['generated_at'] = payload.get('generated_at')
        return context


class ExamDetailView(TitleMixin, TemplateView):
    template_name = 'exams/detail.html'

    def _hydrate_problem_progress(self, data):
        problems = data.get('problems') or []
        if not self.request.user.is_authenticated:
            for problem in problems:
                problem['user_progress'] = None
            return

        problem_codes = [problem.get('code') for problem in problems if problem.get('code')]
        if not problem_codes:
            return

        problem_rows = list(
            Problem.objects
            .filter(code__in=problem_codes)
            .values_list('id', 'code', 'partial'),
        )
        meta_by_code = {
            code: {
                'id': problem_id,
                'partial': bool(is_partial),
            }
            for problem_id, code, is_partial in problem_rows
        }
        config_by_problem_id = {}
        for problem in problems:
            meta = meta_by_code.get(problem.get('code'))
            if not meta:
                continue
            config_by_problem_id[meta['id']] = {
                'points': float(problem.get('exam_points') or 0),
                'partial': meta['partial'],
            }

        best_points_by_problem = {}
        if config_by_problem_id:
            submissions = (
                Submission.objects
                .filter(
                    user_id=self.request.user.profile.id,
                    problem_id__in=config_by_problem_id.keys(),
                    status='D',
                )
                .only('problem_id', 'case_points', 'case_total')
            )
            for submission in submissions.iterator():
                config = config_by_problem_id.get(submission.problem_id)
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

        for problem in problems:
            exam_points = float(problem.get('exam_points') or 0)
            meta = meta_by_code.get(problem.get('code'))
            earned_points = float(best_points_by_problem.get(meta['id'], 0)) if meta else 0.0
            if exam_points > 0:
                percent = _normalize_percent(round(earned_points / exam_points * 100, 1))
                text = f'{_format_points(earned_points)}/{_format_points(exam_points)} · {percent:.1f}%'
            else:
                percent = 0.0
                text = f'{_format_points(earned_points)} · 0.0%'
            problem['user_progress'] = {
                'earned_points': earned_points,
                'total_points': exam_points,
                'percent': percent,
                'percent_css': f'{percent:.1f}',
                'text': text,
            }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = self.kwargs['slug']
        data = _load_detail_payload(slug)
        if data is None:
            raise Http404()
        self._hydrate_problem_progress(data)
        data['status_label'] = str(STATUS_LABELS.get(data['status'], data['status']))
        can_manage_exams = self.request.user.is_authenticated and self.request.user.is_superuser
        if can_manage_exams and not data.get('id'):
            data['id'] = ExamTag.objects.filter(slug=slug).values_list('id', flat=True).first()
        context['exam'] = data
        context['title'] = data['name']
        context['can_manage_exams'] = can_manage_exams
        context['exam_admin_change_url'] = (
            reverse('admin:judge_examtag_change', args=[data['id']])
            if can_manage_exams and data.get('id') else ''
        )
        return context


class ExamsListApiView(View):
    def get(self, request, *args, **kwargs):
        return JsonResponse(_normalize_payload())


class ExamDetailApiView(View):
    def get(self, request, slug, *args, **kwargs):
        data = _load_detail_payload(slug)
        if data is None:
            raise Http404()
        return JsonResponse(data)


class ExamsRebuildApiView(View):
    def post(self, request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_superuser:
            return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)

        # Keep one queued task to avoid accidental storms from repeated clicks.
        queued = cache.add('exams:snapshot:queued', 1, 60)
        if not queued:
            status_id = cache.get('exams:snapshot:last_task')
            return JsonResponse({
                'ok': True,
                'queued': False,
                'task_id': status_id,
                'task_url': task_status_url_by_id(status_id) if status_id else '',
            })

        result = rebuild_exams_snapshots.delay()
        cache.set('exams:snapshot:last_task', result.id, 86400)
        return JsonResponse({
            'ok': True,
            'queued': True,
            'task_id': result.id,
            'task_url': task_status_url_by_id(
                result.id,
                message='Rebuilding exams snapshot...',
                redirect=reverse('exams_list'),
            ),
        })
