import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Max, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView

from judge.models.storage import (
    StorageEvictionRule, StorageOrganizationUsage, StorageProblemUsage, StorageSystemStatus,
)
from judge.tasks.storage import (
    _eviction_candidate_queryset, storage_apply_eviction_rules, storage_evict_problem,
    storage_full_reconcile, storage_sync_catalog,
)
from judge.utils import storage_client
from judge.utils.views import TitleMixin

logger = logging.getLogger('judge.views.storage_admin')

ACTION_MESSAGES = {
    'sync': _('Catalog sync queued.'),
    'reconcile': _('Full reconciliation queued.'),
    'apply': _('Clear rules applied.'),
    'evict': _('Local clear queued for problem.'),
    'add_rule': _('Clear rule created.'),
    'delete_rule': _('Clear rule deleted.'),
    'toggle_rule': _('Clear rule updated.'),
}


def _format_bytes(value):
    value = int(value or 0)
    units = ('B', 'KB', 'MB', 'GB', 'TB')
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return ('%.1f %s' % (size, unit)) if unit != 'B' else ('%d B' % value)
        size /= 1024


class StorageAdminOverview(LoginRequiredMixin, TitleMixin, ListView):
    """System-wide storage overview for superusers.

    Shows global accounting, volume health, organization rollups, the admin
    clear-rule table, per-problem clear actions, and the recent eviction log.
    """
    template_name = 'status/storage-admin.html'
    context_object_name = 'usages'
    paginate_by = 50
    title = _('Storage overview')

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise Http404()
        return super(StorageAdminOverview, self).dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
        redirect_url = reverse('status_storage')
        if action == 'sync':
            storage_sync_catalog.delay()
        elif action == 'reconcile':
            storage_full_reconcile.delay()
        elif action == 'apply':
            rule_id = request.POST.get('rule_id')
            storage_apply_eviction_rules.delay(int(rule_id) if rule_id else None)
        elif action == 'evict':
            problem_id = request.POST.get('problem_id')
            if problem_id and problem_id.isdigit():
                storage_evict_problem.delay(int(problem_id))
            else:
                return HttpResponseRedirect(redirect_url)
        elif action == 'add_rule':
            name = (request.POST.get('name') or '').strip()
            idle_hours = request.POST.get('idle_hours')
            max_size_mb = request.POST.get('max_size_mb')
            if name:
                StorageEvictionRule.objects.create(
                    name=name[:100],
                    idle_hours=max(1, int(idle_hours or 24)),
                    max_size_bytes=(int(float(max_size_mb) * 1024 * 1024)
                                    if max_size_mb not in (None, '', '0') else None),
                )
        elif action == 'delete_rule':
            StorageEvictionRule.objects.filter(pk=request.POST.get('rule_id')).delete()
        elif action == 'toggle_rule':
            rule = StorageEvictionRule.objects.filter(pk=request.POST.get('rule_id')).first()
            if rule:
                rule.enabled = not rule.enabled
                rule.save(update_fields=['enabled', 'updated_at'])
        else:
            return HttpResponseRedirect(redirect_url)
        return HttpResponseRedirect(redirect_url + '?done=%s' % action)

    def _recent_evict_events(self):
        items, _cursor, _more = storage_client.get_audit_events(action='problem.evict', limit=10)
        return items or []

    def get_queryset(self):
        queryset = StorageProblemUsage.objects.select_related('problem').annotate(
            last_submission=Max('problem__submission__date'),
        ).order_by('-allocated_bytes')
        search = self.request.GET.get('search')
        if search:
            queryset = queryset.filter(Q(code__icontains=search) | Q(problem__name__icontains=search))
        local_status = self.request.GET.get('local_status')
        if local_status:
            queryset = queryset.filter(local_status=local_status)
        r2_status = self.request.GET.get('r2_status')
        if r2_status:
            queryset = queryset.filter(r2_status__iexact=r2_status)
        return queryset

    def _rules_with_counts(self):
        rules = []
        now = timezone.now()
        for rule in StorageEvictionRule.objects.all():
            cutoff = now - timezone.timedelta(hours=max(1, rule.idle_hours))
            candidates = _eviction_candidate_queryset(cutoff, max_bytes=rule.max_size_bytes)
            rules.append({
                'rule': rule,
                'candidate_count': candidates.count(),
                'max_size_label': _format_bytes(rule.max_size_bytes) if rule.max_size_bytes else None,
                'sample': list(candidates.order_by('local_ready_at', 'problem_id')
                               .values('problem_id', 'code', 'allocated_bytes')[:3]),
            })
        return rules


    def get_context_data(self, **kwargs):
        context = super(StorageAdminOverview, self).get_context_data(**kwargs)
        status, _created = StorageSystemStatus.objects.get_or_create(id=1)
        present = StorageProblemUsage.objects.filter(catalog_state='present')
        aggregates = present.aggregate(
            total_logical=Sum('logical_bytes'),
            total_allocated=Sum('allocated_bytes'),
            total_archive=Sum('archive_bytes'),
            total_files=Sum('file_count'),
            problem_count=Count('pk'),
        )
        r2_ready = present.filter(r2_status__iexact='ready', downloadable=True).count()
        volume_total = status.volume_total_bytes or 0
        volume_used = max(0, volume_total - (status.volume_free_bytes or 0))
        for usage in context['usages']:
            usage.allocated_label = _format_bytes(usage.allocated_bytes)
            usage.logical_label = _format_bytes(usage.logical_bytes)
            usage.r2_status_normalized = (usage.r2_status or '').upper()
            usage.clearable = (
                usage.catalog_state == 'present' and usage.local_status == 'present'
                and usage.r2_status_normalized == 'READY'
            )
        org_rows = []
        for row in (
            StorageOrganizationUsage.objects.select_related('organization')
            .order_by('-total_allocated_bytes')[:20]
        ):
            org_rows.append({
                'organization': row.organization,
                'problem_count': row.problem_count,
                'logical_label': _format_bytes(row.total_logical_bytes),
                'allocated_label': _format_bytes(row.total_allocated_bytes),
                'quota_label': _format_bytes(row.quota_bytes) if row.quota_bytes else _('Unlimited'),
                'quota_percent': int(min(100, row.total_allocated_bytes * 100 / row.quota_bytes))
                if row.quota_bytes else 0,
                'stale': row.stale,
            })
        context.update({
            'title': _('Storage overview'),
            'content_title': _('Storage overview'),
            'status': status,
            'total_problems': aggregates['problem_count'] or 0,
            'total_logical_label': _format_bytes(aggregates['total_logical']),
            'total_allocated_label': _format_bytes(aggregates['total_allocated']),
            'total_archive_label': _format_bytes(aggregates['total_archive']),
            'total_files': aggregates['total_files'] or 0,
            'r2_ready': r2_ready,
            'volume_total_label': _format_bytes(volume_total),
            'volume_used_label': _format_bytes(volume_used),
            'volume_percent': int(volume_used * 100 / volume_total) if volume_total else 0,
            'rules': self._rules_with_counts(),
            'org_rows': org_rows,
            'evict_events': self._recent_evict_events(),
            'done_action': self.request.GET.get('done'),
            'done_message': ACTION_MESSAGES.get(self.request.GET.get('done')),
            'filters': self.request.GET,
            'format_bytes': _format_bytes,
        })
        return context
