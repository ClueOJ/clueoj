from datetime import date, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404
from django.utils.decorators import method_decorator
from django.utils.formats import date_format
from django.views.decorators.cache import never_cache

from judge.models import StreakContribution, StreakRun, StreakProblemState, StreakRebuildRequest, Submission
from judge.utils.streaks import _tier, enabled
from judge.views.user import UserPage


@method_decorator(never_cache, name='dispatch')
class UserStreakPage(LoginRequiredMixin, UserPage):
    template_name = 'user/user-streaks.html'

    def dispatch(self, request, *args, **kwargs):
        if not enabled():
            raise Http404
        if request.user.is_authenticated and kwargs['user'] != request.user.username:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        return self.request.profile

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = self.object
        summary = context['streak']
        today = summary['today']
        try:
            year = int(self.request.GET.get('year', today.year))
        except ValueError:
            year = today.year
        year = max(1971, min(today.year, year))
        runs = list(StreakRun.objects.filter(user=profile, start__lte=date(year, 12, 31),
                                             end__gte=date(year, 1, 1)).values_list('start', 'end', 'length'))
        lengths = {}
        for start, end, length in runs:
            day = max(start, date(year, 1, 1))
            last = min(end, date(year, 12, 31))
            while day <= last:
                lengths[day] = length
                day += timedelta(days=1)
        grid_start = date(year, 1, 1) - timedelta(days=date(year, 1, 1).weekday())
        weeks, month_labels = [], []
        cursor = grid_start
        while cursor <= date(year, 12, 31):
            cells, label = [], ''
            for offset in range(7):
                day = cursor + timedelta(days=offset)
                length = lengths.get(day, 0) if day.year == year else 0
                if day.year == year and day.day == 1:
                    # Numeric month labels stay narrow in every locale and
                    # never overlap on the 13px heat columns.
                    label = str(day.month)
                cells.append({
                    'date': day, 'blank': day.year != year, 'kept': length > 0,
                    'length': length, 'tier': _tier(length) if length else '',
                    'today': day == today, 'future': day > today,
                })
            weeks.append(cells)
            month_labels.append(label)
            cursor += timedelta(days=7)
        months = []
        for month in range(1, 13):
            first = date(year, month, 1)
            cell_cursor = first - timedelta(days=first.weekday())
            month_cells = []
            while len(month_cells) < 42:
                day = cell_cursor + timedelta(days=len(month_cells))
                length = lengths.get(day, 0) if day.month == month and day.year == year else 0
                month_cells.append({
                    'date': day, 'number': day.day, 'blank': day.month != month or day.year != year,
                    'kept': length > 0, 'length': length, 'tier': _tier(length) if length else '',
                    'today': day == today, 'future': day > today,
                })
            months.append({'label': date_format(first, 'F'), 'cells': month_cells})
        selected = None
        try:
            selected = date.fromisoformat(self.request.GET.get('day', ''))
        except ValueError:
            pass
        if selected and (selected.year != year or selected > today):
            selected = None
        calendar_month = selected.month if selected else (today.month if year == today.year else 1)
        context.update(calendar_month=calendar_month, year=year, weeks=weeks, month_labels=month_labels, months=months,
                       previous_year=year - 1 if year > 1971 else None,
                       next_year=year + 1 if year < today.year else None,
                       pending=StreakProblemState.objects.filter(user=profile, pending=True).exists() or
                       StreakRebuildRequest.objects.filter(kind='user', object_id=profile.pk).exists())
        runs = Paginator(StreakRun.objects.filter(user=profile).order_by('-start'), 5).get_page(self.request.GET.get('page'))
        context['runs_page'] = runs
        context['streak_runs'] = [{'start': run.start, 'end': run.end, 'length': run.length,
                                  'active': run.end >= today - timedelta(days=1),
                                  'missed': run.end + timedelta(days=1),
                                  'record': run.length == summary['longest']} for run in runs]
        context['selected_day'] = selected
        context['contributions'] = []
        if selected:
            # Recheck current publication at read time; never expose hidden offline results.
            from judge.utils.streaks import eligible_problems
            evidence = list(StreakContribution.objects.filter(user=profile, day=selected)
                            .order_by('submission_id')[:100])
            visible = {s.pk: s for s in Submission.visible.filter(
                user=profile, pk__in=[row.submission_id for row in evidence],
                problem_id__in=eligible_problems().values('pk'),
            ).select_related('problem')}
            context['contributions'] = [{'submission': visible[row.submission_id], 'before': row.previous_points,
                                         'after': row.points} for row in evidence if row.submission_id in visible]
        return context
