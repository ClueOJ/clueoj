from collections import namedtuple

from django.conf import settings
from django.db.models import OuterRef, Subquery
from django.utils.translation import gettext as _

from judge.models import Problem, Submission
from judge.utils.subtask_scoring import cumulative_breakdown, user_problem_scores

PP_WEIGHT_TABLE = [pow(settings.DMOJ_PP_STEP, i) for i in range(settings.DMOJ_PP_ENTRIES)]

PPBreakdown = namedtuple('PPBreakdown', 'points weight scaled_points problem_name problem_code '
                                        'sub_id sub_date sub_points sub_total sub_result_class '
                                        'sub_short_status sub_long_status sub_lang')


def get_pp_breakdown(user, start=0, end=settings.DMOJ_PP_ENTRIES):
    scores = user_problem_scores(user.pk)
    best = (Submission.visible.filter(user_id=user.pk, problem_id=OuterRef('pk'))
            .order_by('-points', 'date', 'pk'))
    problems = {p.pk: p for p in Problem.get_public_problems().filter(pk__in=scores)
                .annotate(best_id=Subquery(best.values('pk')[:1]))}
    submissions = Submission.visible.select_related('language').in_bulk(
        [p.best_id for p in problems.values() if p.best_id])
    ordered = sorted((pk for pk in problems if scores[pk] > 0 and problems[pk].best_id in submissions),
                     key=lambda pk: (-scores[pk], -submissions[problems[pk].best_id].date.timestamp(), pk))
    data = ordered[start:end + 1]
    breakdown = []
    for weight, problem_id in zip(PP_WEIGHT_TABLE[start:end], data):
        problem = problems[problem_id]
        submission = submissions.get(problem.best_id)
        cumulative = problem.subtask_score_state == 'ON' or (
            problem.subtask_score_state == 'REBUILDING' and problem.subtask_score_progress.get('phase') == 'refresh')
        points = scores[problem_id]
        score = cumulative_breakdown(user.pk, problem) if cumulative else None
        sources = score['subtasks'] if cumulative else []
        dates = [row['date'] for row in sources if row['date']]
        breakdown.append(PPBreakdown(
            points=points,
            weight=weight * 100,
            scaled_points=points * weight,
            problem_name=problem.name,
            problem_code=problem.code,
            sub_id=None if cumulative else submission.pk,
            sub_date=max(dates) if dates else submission.date,
            sub_points=score['case_points'] if cumulative else submission.case_points,
            sub_total=score['case_total'] if cumulative else submission.case_total,
            sub_short_status='Σ' if cumulative else submission.short_status,
            sub_long_status=_('Cumulative subtasks') if cumulative else submission.long_status,
            sub_result_class='' if cumulative else submission.result_class,
            sub_lang=_('Subtasks') if cumulative else submission.language.short_display_name,
        ))
    has_more = end < min(len(PP_WEIGHT_TABLE), start + len(data))
    return breakdown, has_more
