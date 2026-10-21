import json
from collections import defaultdict
from math import fsum

from django.http import HttpResponseForbidden, JsonResponse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from judge.models import Contest


class Resolver(TemplateView):
    title = _("Resolver")
    template_name = "resolver/resolver.html"

    def get_contest_json(self):
        # Presentation indexes must be contiguous even when contest orders are not.
        problem_ids = list(self.contest.contest_problems.order_by('order', 'id').values_list('id', flat=True))
        indexes = {problem_id: str(index) for index, problem_id in enumerate(problem_ids, 1)}
        contest_format = self.contest.format
        hidden = contest_format.get_hidden_subtasks()
        precision = self.contest.points_precision
        maxima = defaultdict(dict)
        users = {}

        def bucket(problem_id, batch):
            return '2' if batch in hidden.get(str(problem_id), set()) else '1'

        def combine(rows, record_maxima=False):
            scores = defaultdict(lambda: defaultdict(list))
            for problem_id, _, _, batch, points, total in rows:
                if problem_id not in indexes:
                    continue
                scores[problem_id][bucket(problem_id, batch)].append(points)
                if record_maxima:
                    maxima[problem_id][batch] = max(maxima[problem_id].get(batch, 0), total)
            return scores

        def rounded(problem_id, scores):
            buckets = ('1', '2') if hidden.get(str(problem_id)) else ('1',)
            # Round once per displayed group, after adding raw subtask scores.
            return {key: round(fsum(scores.get(key, [])), precision) for key in buckets}

        for number, participation in enumerate(self.contest.users.filter(virtual=0).select_related('user__user'), 1):
            points = combine(contest_format.get_results_by_subtask(participation, True), record_maxima=True)
            frozen = combine(contest_format.get_results_by_subtask(participation, False))
            users[str(number)] = {
                'name': participation.user.username,
                'last_submission': participation.cumtime,
                'problems': {
                    indexes[problem_id]: {
                        'points': rounded(problem_id, points[problem_id]),
                        'frozen_points': rounded(problem_id, frozen.get(problem_id, {})),
                    }
                    for problem_id in points
                },
            }

        problems = {}
        for problem_id in problem_ids:
            totals = defaultdict(list)
            for batch, total in maxima[problem_id].items():
                totals[bucket(problem_id, batch)].append(total)
            problems[indexes[problem_id]] = rounded(problem_id, totals)

        return {
            'points_precision': precision,
            'problem_sub': [2 if hidden.get(str(problem_id)) else 1 for problem_id in problem_ids],
            'sub_frozen': [[2] if hidden.get(str(problem_id)) else [] for problem_id in problem_ids],
            'problems': problems,
            'users': users,
        }

    def get_context_data(self, **kwargs):
        context = super(Resolver, self).get_context_data(**kwargs)
        context["contest_json"] = mark_safe(json.dumps(self.get_contest_json()))
        return context

    def get(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden()
        self.contest = Contest.objects.get(key=kwargs.get("contest"))
        if not self.contest.format.has_hidden_subtasks:
            return HttpResponseForbidden()

        if self.request.GET.get("json"):
            json_dumps_params = {"ensure_ascii": False}
            return JsonResponse(
                self.get_contest_json(), json_dumps_params=json_dumps_params
            )
        return super(Resolver, self).get(request, *args, **kwargs)
