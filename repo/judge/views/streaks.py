import calendar
from datetime import date, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.paginator import Paginator
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from judge.models import StreakContribution, StreakDay, StreakRun, StreakProblemState, StreakRebuildRequest, Submission
from judge.utils.streaks import enabled
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
        days = set(StreakDay.objects.filter(user=profile, day__gte=date(year, 1, 1),
                                            day__lt=date(year + 1, 1, 1)).values_list('day', flat=True))
        months = []
        for month in range(1, 13):
            cells = []
            for day in calendar.Calendar(firstweekday=0).itermonthdates(year, month):
                cells.append({'date': day, 'number': day.day, 'blank': day.month != month,
                              'kept': day in days, 'today': day == today, 'future': day > today})
            months.append({'number': month, 'cells': cells})
        context.update(year=year, months=months, previous_year=year - 1 if year > 1971 else None,
                       next_year=year + 1 if year < today.year else None,
                       pending=StreakProblemState.objects.filter(user=profile, pending=True).exists() or
                       StreakRebuildRequest.objects.filter(kind='user', object_id=profile.pk).exists())
        runs = Paginator(StreakRun.objects.filter(user=profile).order_by('-start'), 20).get_page(self.request.GET.get('page'))
        context['runs_page'] = runs
        context['streak_runs'] = [{'start': run.start, 'end': run.end, 'length': run.length,
                                  'active': run.end >= today - timedelta(days=1),
                                  'missed': run.end + timedelta(days=1),
                                  'record': run.length == summary['longest']} for run in runs]
        selected = None
        try:
            selected = date.fromisoformat(self.request.GET.get('day', ''))
        except ValueError:
            pass
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
