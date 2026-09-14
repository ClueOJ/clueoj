import logging

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Max, Q, Sum
from django.http import Http404, HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.generic import ListView, TemplateView

from judge.models.storage import (
    StorageEvictionRule, StorageOrganizationUsage, StorageProblemUsage,
    StorageSystemStatus, StorageUsageSample,
)
from judge.tasks.storage import (
    _eviction_candidate_queryset, storage_apply_eviction_rules, storage_evict_problem,
    storage_full_reconcile, storage_sync_catalog,
)
from judge.utils import storage_client
from judge.utils.views import TitleMixin

logger = logging.getLogger('judge.views.storage_admin')

BULK_EVICT_LIMIT = 200
SCHEDULE_PREVIEW_LIMIT = 100

ACTION_MESSAGES = {
    'sync': _('Catalog sync queued.'),
    'reconcile': _('Full reconciliation queued.'),
    'apply': _('Clear rules applied.'),
    'evict': _('Local clear queued for problem.'),
    'evict_bulk': _('Local clear queued for selected problems.'),
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

    def _redirect(self, action):
        url = reverse('status_storage')
        section = self.request.POST.get('section') or ''
        params = []
        if section in self.SECTIONS:
            params.append('section=%s' % section)
        if action:
            params.append('done=%s' % action)
        if params:
            url += '?' + '&'.join(params)
        return HttpResponseRedirect(url)

    def post(self, request, *args, **kwargs):
        action = request.POST.get('action')
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
                return self._redirect(None)
        elif action == 'evict_bulk':
            problem_ids = [pid for pid in request.POST.getlist('problem_ids') if pid.isdigit()]
            clearable = StorageProblemUsage.objects.filter(
                problem_id__in=problem_ids[:BULK_EVICT_LIMIT],
                catalog_state='present',
                local_status='present',
                r2_status__iexact='ready',
            ).values_list('problem_id', flat=True)
            for pid in clearable:
                storage_evict_problem.delay(int(pid))
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
            return self._redirect(None)
        return self._redirect(action)

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

    def _rule_candidates(self, idle_hours, max_bytes=None, limit=SCHEDULE_PREVIEW_LIMIT):
        """Problems a rule will clear, with the time each becomes eligible.

        Includes problems that are not idle enough yet, so admins can see the
        upcoming schedule instead of only already-eligible problems. Problems
        with recent/active submissions or grading are never eligible and stay
        hidden until they go quiet.
        """
        now = timezone.now()
        window = timezone.timedelta(hours=max(1, idle_hours))
        candidates = (
            _eviction_candidate_queryset(now, max_bytes=max_bytes)
            .annotate(last_submission=Max('problem__submission__date'))
            .order_by('local_ready_at', 'problem_id')[:limit]
        )
        rows = []
        for usage in candidates:
            idle_since = usage.local_ready_at
            if usage.last_submission and usage.last_submission > idle_since:
                idle_since = usage.last_submission
            eligible_at = idle_since + window
            rows.append({
                'problem_id': usage.problem_id,
                'code': usage.code,
                'allocated_label': _format_bytes(usage.allocated_bytes),
                'last_submission': usage.last_submission,
                'idle_since': idle_since,
                'eligible_at': eligible_at,
                'eligible_now': eligible_at <= now,
            })
        rows.sort(key=lambda row: row['eligible_at'])
        return rows

    def _rules_with_counts(self):
        rules = []
        for rule in StorageEvictionRule.objects.all():
            schedule = self._rule_candidates(rule.idle_hours, rule.max_size_bytes)
            rules.append({
                'rule': rule,
                'candidate_count': len(schedule),
                'max_size_label': _format_bytes(rule.max_size_bytes) if rule.max_size_bytes else None,
                'sample': schedule[:3],
                'schedule': schedule,
            })
        return rules

    def _passive_sweep_schedule(self):
        """The always-on passive sweep, shown like a built-in rule."""
        if not getattr(settings, 'STORAGE_LOCAL_EVICTION_ENABLED', False):
            return None
        idle_hours = max(1, int(getattr(settings, 'STORAGE_LOCAL_EVICTION_IDLE_HOURS', 24)))
        return {
            'name': _('Passive sweep'),
            'idle_hours': idle_hours,
            'enabled': True,
            'schedule': self._rule_candidates(idle_hours),
        }


    SECTIONS = ('overview', 'rules', 'problems', 'events')

    def get_context_data(self, **kwargs):
        context = super(StorageAdminOverview, self).get_context_data(**kwargs)
        section = self.request.GET.get('section') or 'overview'
        if section not in self.SECTIONS:
            section = 'overview'
        status, _created = StorageSystemStatus.objects.get_or_create(id=1)

        context_data = {
            'title': _('Storage overview'),
            'content_title': _('Storage overview'),
            'section': section,
            'status': status,
            'done_action': self.request.GET.get('done'),
            'done_message': ACTION_MESSAGES.get(self.request.GET.get('done')),
            'filters': self.request.GET,
            'format_bytes': _format_bytes,
        }

        for usage in context['usages']:
            usage.allocated_label = _format_bytes(usage.allocated_bytes)
            usage.logical_label = _format_bytes(usage.logical_bytes)
            usage.r2_status_normalized = (usage.r2_status or '').upper()
            usage.clearable = (
                usage.catalog_state == 'present' and usage.local_status == 'present'
                and usage.r2_status_normalized == 'READY'
            )

        if section == 'overview':
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
            context_data.update({
                'total_problems': aggregates['problem_count'] or 0,
                'total_logical_label': _format_bytes(aggregates['total_logical']),
                'total_allocated_label': _format_bytes(aggregates['total_allocated']),
                'total_archive_label': _format_bytes(aggregates['total_archive']),
                'total_files': aggregates['total_files'] or 0,
                'r2_ready': r2_ready,
                'volume_total_label': _format_bytes(volume_total),
                'volume_used_label': _format_bytes(volume_used),
                'volume_percent': int(volume_used * 100 / volume_total) if volume_total else 0,
                'org_rows': org_rows,
            })
        elif section == 'rules':
            rules = self._rules_with_counts()
            passive_sweep = self._passive_sweep_schedule()
            context_data.update({
                'rules': rules,
                'passive_sweep': passive_sweep,
                'scheduled_total': sum(
                    len(entry['schedule'])
                    for entry in rules if entry['rule'].enabled
                ) + (len(passive_sweep['schedule']) if passive_sweep else 0),
                'rule_sweep_seconds': int(getattr(settings, 'STORAGE_RULE_SWEEP_SECONDS', 3600)),
            })
        elif section == 'events':
            context_data['evict_events'] = self._recent_evict_events()

        context.update(context_data)
        return context


def _spark_points(samples, width=120, height=26):
    """SVG polyline points for a usage series, oldest first."""
    if len(samples) < 2:
        return ''
    max_value = max(sample.total_logical_bytes for sample in samples) or 1
    step = width / (len(samples) - 1)
    return ' '.join(
        '%s,%s' % (
            round(index * step, 1),
            round(height - (sample.total_logical_bytes * height / max_value), 1),
        )
        for index, sample in enumerate(samples)
    )


class StorageAdminOrganizations(LoginRequiredMixin, TitleMixin, TemplateView):
    """Per-organization storage usage statistics for superusers."""
    template_name = 'status/storage-admin-orgs.html'
    title = _('Organization storage')

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            raise Http404()
        return super(StorageAdminOrganizations, self).dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super(StorageAdminOrganizations, self).get_context_data(**kwargs)
        usages = list(
            StorageOrganizationUsage.objects
            .filter(problem_count__gt=0)
            .select_related('organization')
            .order_by('-total_allocated_bytes')
        )
        samples_by_org = {}
        for sample in (
            StorageUsageSample.objects
            .filter(organization_id__in=[row.organization_id for row in usages])
            .order_by('-sampled_at')
        ):
            samples_by_org.setdefault(sample.organization_id, []).append(sample)
        org_rows = []
        for row in usages:
            samples = list(reversed(samples_by_org.get(row.organization_id, [])[:30]))
            org_rows.append({
                'organization': row.organization,
                'problem_count': row.problem_count,
                'file_count': row.total_file_count,
                'logical_label': _format_bytes(row.total_logical_bytes),
                'allocated_label': _format_bytes(row.total_allocated_bytes),
                'archive_label': _format_bytes(row.total_archive_bytes),
                'orphan_label': _format_bytes(row.orphan_bytes),
                'quota_label': _format_bytes(row.quota_bytes) if row.quota_bytes else _('Unlimited'),
                'quota_percent': int(min(100, row.total_allocated_bytes * 100 / row.quota_bytes))
                if row.quota_bytes else 0,
                'stale': row.stale,
                'observed_at': row.observed_at,
                'spark_points': _spark_points(samples),
                'trend_first': _format_bytes(samples[0].total_logical_bytes) if samples else '',
                'trend_last': _format_bytes(samples[-1].total_logical_bytes) if samples else '',
            })
        unassigned = StorageProblemUsage.objects.filter(
            catalog_state='present', owner_organization_id__isnull=True,
        ).aggregate(
            count=Count('pk'),
            allocated=Sum('allocated_bytes'),
            logical=Sum('logical_bytes'),
        )
        totals = StorageOrganizationUsage.objects.filter(problem_count__gt=0).aggregate(
            problems=Sum('problem_count'),
            logical=Sum('total_logical_bytes'),
            allocated=Sum('total_allocated_bytes'),
            archive=Sum('total_archive_bytes'),
        )
        context.update({
            'title': _('Organization storage'),
            'content_title': _('Organization storage'),
            'org_rows': org_rows,
            'org_count': len(org_rows),
            'total_problems': totals['problems'] or 0,
            'total_logical_label': _format_bytes(totals['logical']),
            'total_allocated_label': _format_bytes(totals['allocated']),
            'total_archive_label': _format_bytes(totals['archive']),
            'unassigned_problems': unassigned['count'] or 0,
            'unassigned_allocated_label': _format_bytes(unassigned['allocated']),
            'format_bytes': _format_bytes,
        })
        return context
