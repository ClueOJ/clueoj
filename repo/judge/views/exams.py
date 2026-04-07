from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from judge.models import ExamTag
from judge.tasks import rebuild_exams_snapshots
from judge.utils.celery import task_status_url_by_id
from judge.utils.exams import build_exam_snapshots, load_exam_detail_snapshot, load_exam_index_snapshot
from judge.utils.views import TitleMixin


STATUS_LABELS = {
    'complete': _('Đã có'),
    'updating': _('Đang cập nhật'),
    'missing': _('Không sở hữu'),
}


def _normalize_payload():
    payload = load_exam_index_snapshot()
    if payload is None:
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

    def _filter_items(self, items):
        keyword = self.request.GET.get('keyword', '').strip().lower()
        year = self.request.GET.get('year', '').strip()
        exam_type = self.request.GET.get('exam_type', '').strip().lower()
        province = self.request.GET.get('province', '').strip().lower()
        status = self.request.GET.get('status', '').strip().lower()

        def matched(item):
            if keyword:
                haystack = ' '.join([
                    item.get('name', ''),
                    item.get('province', ''),
                    item.get('exam_type', ''),
                    item.get('status_note', ''),
                ]).lower()
                if keyword not in haystack:
                    return False
            if year and str(item.get('year') or '') != year:
                return False
            if exam_type and exam_type not in (item.get('exam_type', '') or '').lower():
                return False
            if province and province not in (item.get('province', '') or '').lower():
                return False
            if status and status != (item.get('status') or '').lower():
                return False
            return True

        return [item for item in items if matched(item)]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payload = _normalize_payload()
        items = payload.get('items', [])
        filtered_items = [dict(item) for item in self._filter_items(items)]
        can_manage_exams = self.request.user.is_authenticated and self.request.user.is_superuser
        if can_manage_exams:
            missing_id_slugs = [item['slug'] for item in filtered_items if item.get('slug') and not item.get('id')]
            id_by_slug = dict(ExamTag.objects.filter(slug__in=missing_id_slugs).values_list('slug', 'id'))
            for item in filtered_items:
                if not item.get('id') and item.get('slug') in id_by_slug:
                    item['id'] = id_by_slug[item['slug']]
                if item.get('id'):
                    item['admin_change_url'] = reverse('admin:judge_examtag_change', args=[item['id']])

        context['summary'] = payload.get('summary', {})
        context['items'] = filtered_items
        context['can_manage_exams'] = can_manage_exams
        context['exam_tag_add_url'] = reverse('admin:judge_examtag_add') if can_manage_exams else ''
        context['total_pages'] = 1
        context['current_page'] = 1
        context['filters'] = {
            'keyword': self.request.GET.get('keyword', '').strip(),
            'year': self.request.GET.get('year', '').strip(),
            'exam_type': self.request.GET.get('exam_type', '').strip(),
            'province': self.request.GET.get('province', '').strip(),
            'status': self.request.GET.get('status', '').strip(),
        }
        context['generated_at'] = payload.get('generated_at')
        return context


class ExamDetailView(TitleMixin, TemplateView):
    template_name = 'exams/detail.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        slug = self.kwargs['slug']
        data = _load_detail_payload(slug)
        if data is None:
            raise Http404()
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
