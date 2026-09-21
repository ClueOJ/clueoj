from datetime import date, timedelta
from urllib.parse import urlencode

from django import forms
from django.core.paginator import Paginator
from django.db.models import Max, Sum
from django.db.models.functions import TruncMonth
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from judge.models import JudgingUsageDaily, Organization
from judge.utils.judging_usage import accounting_timezone


class UsageFilter(forms.Form):
    start = forms.DateField(required=False)
    end = forms.DateField(required=False)
    group = forms.ChoiceField(choices=(('day', 'day'), ('month', 'month')), required=False)
    period = forms.ChoiceField(choices=(('current', 'current'), ('previous', 'previous'), ('all', 'all')), required=False)


def judging_usage(request, slug=None):
    if not request.user.is_authenticated or not request.user.is_superuser:
        raise Http404()
    organization = get_object_or_404(Organization, slug=slug) if slug else None
    form = UsageFilter(request.GET)
    if not form.is_valid():
        return HttpResponseBadRequest(_('Invalid date range.'))

    today = timezone.localtime(timezone.now(), accounting_timezone()).date()
    start, end = today.replace(day=1), today
    usages = JudgingUsageDaily.objects.all()
    if organization:
        usages = usages.filter(organization_key=organization.pk)
    period = form.cleaned_data['period']
    if period == 'previous':
        end = start - timedelta(days=1)
        start = end.replace(day=1)
    elif period == 'all':
        start = usages.order_by('usage_date').values_list('usage_date', flat=True).first() or start
    start = form.cleaned_data['start'] or start
    end = form.cleaned_data['end'] or end
    if start > end or (end - start).days > 36600 or end.year >= 9999:
        return HttpResponseBadRequest(_('Invalid date range.'))
    group = form.cleaned_data['group'] or 'day'
    # Keep charts bounded even when the admin selects years of history.
    if (end - start).days > 366:
        group = 'month'
    usages = usages.filter(usage_date__gte=start, usage_date__lte=end)
    metrics = dict(seconds=Sum('seconds'), attempt_count=Sum('attempts'), rejudge_count=Sum('rejudges'))
    totals = usages.aggregate(**metrics)
    totals['seconds'] = totals['seconds'] or 0
    totals['hours'] = totals['seconds'] / 3600
    totals['attempts'] = totals.pop('attempt_count') or 0
    totals['rejudges'] = totals.pop('rejudge_count') or 0

    bucket = TruncMonth('usage_date') if group == 'month' else None
    series = usages.annotate(bucket=bucket).values('bucket') if bucket is not None else usages.values('usage_date')
    series = series.annotate(seconds=Sum('seconds'), attempt_count=Sum('attempts')).order_by()
    values = {row['bucket' if bucket is not None else 'usage_date']: row for row in series}
    cursor = start.replace(day=1) if group == 'month' else start
    labels, hours, seconds, attempts = [], [], [], []
    while cursor <= end:
        point = values.get(cursor, {})
        value = point.get('seconds', 0)
        attempts.append(point.get('attempt_count', 0))
        labels.append(cursor.strftime('%Y-%m' if group == 'month' else '%Y-%m-%d'))
        seconds.append(value)
        hours.append(value / 3600)
        if group == 'month':
            cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        else:
            cursor += timedelta(days=1)

    params = urlencode({'start': start.isoformat(), 'end': end.isoformat(), 'group': group})
    page = None
    if organization is None:
        rows = usages.values('organization_key').annotate(
            label=Max('organization_name'), **metrics,
        ).order_by('-seconds', 'organization_key')
        page = Paginator(rows, 50).get_page(request.GET.get('page'))
        objects = Organization.objects.in_bulk([row['organization_key'] for row in page if row['organization_key']])
        for row in page:
            key = row['organization_key']
            obj = objects.get(key)
            row['hours'] = row['seconds'] / 3600
            row['attempts'] = row.pop('attempt_count')
            row['rejudges'] = row.pop('rejudge_count')
            row['url'] = None
            if key == 0:
                row['label'] = _('No organization')
            else:
                row['label'] = obj.name if obj else row['label'] or '#%s' % key
                if obj:
                    row['url'] = reverse('organization_judging_usage', args=[obj.slug]) + '?' + params

    title = _('Judging resources for %s') % organization.name if organization else _('Judging resources')
    return render(request, 'stats/judging-usage.html', {
        'title': title, 'content_title': title, 'organization': organization,
        'totals': totals, 'page_obj': page, 'start': start.isoformat(), 'end': end.isoformat(),
        'group': group, 'params': params, 'accounting_timezone': str(accounting_timezone()),
        'chart': {'labels': labels, 'hours': hours, 'seconds': seconds, 'attempts': attempts},
    })
