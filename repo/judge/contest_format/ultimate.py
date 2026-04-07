from datetime import timedelta
from django.db import connection
from django.utils.translation import gettext as _, gettext_lazy
from judge.timezone import from_database_time, to_database_time
from django.core.exceptions import ValidationError
from django.db.models import Min, Max, OuterRef, Subquery, F
from django.template.defaultfilters import floatformat
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy

from judge.contest_format.default import DefaultContestFormat
from judge.contest_format.registry import register_contest_format
from judge.utils.timedelta import nice_repr


@register_contest_format('ultimate')
class UltimateContestFormat(DefaultContestFormat):
    name = gettext_lazy('Ultimate')
    config_defaults = {'cumtime': False, 'last_score_altering': False, 'counted_submission': 1}
    """
        cumtime: Specify True if time penalties are to be computed. Defaults to False.
        last_score_altering: Specify True if ties are to be broken by the time of the last score altering submission.
        Defaults to False.
    """

    @classmethod
    def validate(cls, config):
        if config is None:
            return

        if not isinstance(config, dict):
            raise ValidationError('IOI-styled contest expects no config or dict as config')

        for key, value in config.items():
            if key not in cls.config_defaults:
                raise ValidationError('unknown config key "%s"' % key)
            if not isinstance(value, type(cls.config_defaults[key])):
                raise ValidationError('invalid type for config key "%s"' % key)

    def __init__(self, contest, config):
        self.config = self.config_defaults.copy()
        self.config.update(config or {})
        self.contest = contest

    def get_results_by_subtask(self, participation, include_frozen=False):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT q.prob,
                    q.prob_points,
                    MIN(q.date) as `date`,
                    q.batch_points,
                    q.total_batch_points,
                    q.batch,
                    q.subid
                FROM (
                        SELECT cp.id          as `prob`,
                                cp.points      as `prob_points`,
                                sub.id         as `subid`,
                                sub.date       as `date`,
                                tc.points      as `points`,
                                tc.batch       as `batch`,
                                SUM(tc.points) as `batch_points`,
                                SUM(tc.total)  as `total_batch_points`
                        FROM judge_contestproblem cp
                                INNER JOIN judge_contestsubmission cs
                                ON cs.problem_id = cp.id AND cs.participation_id = %s
                                INNER JOIN judge_submission sub
                                ON sub.id = cs.submission_id AND sub.status = 'D'
                                INNER JOIN judge_submissiontestcase tc
                                ON sub.id = tc.submission_id
                        WHERE (sub.date, sub.id) IN (
                            SELECT MAX(sub2.date), MAX(sub2.id)
                            FROM judge_contestsubmission cs2
                                    INNER JOIN judge_submission sub2
                                    ON sub2.id = cs2.submission_id AND sub2.status = 'D'
                            WHERE cs2.participation_id = %s AND cs2.problem_id = cp.id
                            GROUP BY cs2.problem_id
                        )
                        GROUP BY cp.id, tc.batch, sub.id
                    ) q
                        INNER JOIN (
                    SELECT prob, batch, MAX(r.batch_points) as max_batch_points
                    FROM (
                            SELECT cp.id          as `prob`,
                                    tc.batch       as `batch`,
                                    SUM(tc.points) as `batch_points`
                            FROM judge_contestproblem cp
                                    INNER JOIN judge_contestsubmission cs
                                    ON cs.problem_id = cp.id AND cs.participation_id = %s
                                    INNER JOIN judge_submission sub
                                    ON sub.id = cs.submission_id AND sub.status = 'D'
                                    INNER JOIN judge_submissiontestcase tc
                                    ON sub.id = tc.submission_id
                            WHERE (sub.date, sub.id) IN (
                                SELECT MAX(sub2.date), MAX(sub2.id)
                                FROM judge_contestsubmission cs2
                                        INNER JOIN judge_submission sub2
                                        ON sub2.id = cs2.submission_id AND sub2.status = 'D'
                                WHERE cs2.participation_id = %s AND cs2.problem_id = cp.id
                                GROUP BY cs2.problem_id
                            )
                            GROUP BY cp.id, tc.batch, sub.id
                        ) r
                    GROUP BY prob, batch
                ) p
                ON p.prob = q.prob AND (p.batch = q.batch OR (p.batch IS NULL AND q.batch IS NULL))
                WHERE p.max_batch_points = q.batch_points
                GROUP BY q.prob, q.batch
            """,
                (
                    participation.id,
                    participation.id,
                    participation.id,
                    participation.id,
                ),
            )

            return cursor.fetchall()


    def update_participation(self, participation):
        cumtime = 0
        last_submission_time = 0
        score = 0
        format_data = {}
        queryset = (participation.submissions.values('problem_id')
                                             .filter(submission__date=Subquery(
                                                 participation.submissions.filter(problem_id=OuterRef('problem_id'))
                                                                          .order_by('-submission__date')
                                                                          .values('submission__date')[:1]))
                                             .values_list('problem_id', 'submission__date', 'points'))

        for problem_id, time, points in queryset:
            if points:
                dt = (time - self.contest.start_time).total_seconds()
                cumtime += dt
                last_submission_time = max(last_submission_time, dt)
            else:
                dt = 0

            format_data[str(problem_id)] = {'points': points, 'time': dt}
            score += points

        participation.cumtime = max(cumtime, 0)
        participation.score = score
        participation.tiebreak = last_submission_time
        participation.format_data = format_data
        participation.save()

    def get_first_solves_and_total_ac(self, problems, participations, frozen=False):
        first_solves = {}
        total_ac = {}

        show_time = self.config['cumtime'] or self.config.get('last_score_altering', False)
        for problem in problems:
            problem_id = str(problem.id)
            min_time = None
            first_solves[problem_id] = None
            total_ac[problem_id] = 0

            for participation in participations:
                format_data = (participation.format_data or {}).get(problem_id)
                if format_data:
                    points = format_data['points']
                    time = format_data['time']

                    if points == problem.points:
                        total_ac[problem_id] += 1

                        # Only acknowledge first solves for live participations
                        if show_time and participation.virtual == 0 and (min_time is None or min_time > time):
                            min_time = time
                            first_solves[problem_id] = participation.id

        return first_solves, total_ac

    def display_user_problem(self, participation, contest_problem, first_solves, frozen=False):
        format_data = (participation.format_data or {}).get(str(contest_problem.id))
        if format_data:
            show_time = self.config['cumtime'] or self.config.get('last_score_altering', False)
            return format_html(
                '<td class="{state}"><a href="{url}">{points}<div class="solving-time">{time}</div></a></td>',
                state=(('pretest-' if self.contest.run_pretests_only and contest_problem.is_pretested else '') +
                       ('first-solve ' if first_solves.get(str(contest_problem.id), None) == participation.id else '') +
                       self.best_solution_state(format_data['points'], contest_problem.points)),
                url=reverse('contest_user_submissions',
                            args=[self.contest.key, participation.user.user.username, contest_problem.problem.code]),
                points=floatformat(format_data['points'], -self.contest.points_precision),
                time=nice_repr(timedelta(seconds=format_data['time']), 'noday') if show_time else '',
            )
        else:
            return mark_safe('<td></td>')

    def display_participation_result(self, participation, frozen=False):
        show_time = self.config['cumtime'] or self.config.get('last_score_altering', False)
        return format_html(
            '<td class="user-points"><a href="{url}">{points}<div class="solving-time">{cumtime}</div></a></td>',
            url=reverse('contest_all_user_submissions',
                        args=[self.contest.key, participation.user.user.username]),
            points=floatformat(participation.score, -self.contest.points_precision),
            cumtime=nice_repr(timedelta(seconds=participation.cumtime), 'noday') if show_time else '',
        )

    def get_short_form_display(self):
        yield _('The last submission for each problem will be used.')

        if self.config['last_score_altering']:
            if self.config['cumtime']:
                yield _('Ties will be broken by the sum of the last score altering submission time on problems with '
                        'a non-zero score, followed by the time of the last score altering submission.')
            else:
                yield _('Ties will be broken by the time of the last score altering submission.')
        elif self.config['cumtime']:
            yield _('Ties will be broken by the sum of the last score altering submission time on problems with a '
                    'non-zero score.')
        else:
            yield _('Ties by score will **not** be broken.')
