import time

from django.contrib import messages
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Prefetch
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import redirect
from django.template.defaultfilters import floatformat
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic.detail import SingleObjectMixin

from judge.models import Contest, ContestParticipation, ContestSubmission, Organization
from judge.utils.conservative import SCORING_VERSION, aborted_filter, invalidate, start_build, stop, visible_points
from judge.utils.ranker import ranker
from judge.views.contests import ContestMixin, ContestRankingBase, ContestRankingProfile, PublicRankingView


class ConservativeControls(ContestMixin, SingleObjectMixin, View):
    http_method_names = ['post']

    def post(self, request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden(_('Only superusers can manage Conservative Ranking.'))
        self.object = self.get_object()
        try:
            action = request.POST.get('action')
            if action in ('enable', 'rebuild'):
                start_build(self.object.pk)
                messages.success(request, _('Conservative Ranking is being built from stored results.'))
            elif action == 'disable':
                stop(self.object.pk)
            elif action == 'visibility':
                value = request.POST.get('show_conservative_ranking')
                if value not in ('0', '1'):
                    raise ValidationError(_('Invalid visibility setting.'))
                Contest.objects.filter(pk=self.object.pk).update(show_conservative_ranking=value == '1')
                transaction.on_commit(lambda: invalidate(self.object.pk, force=True))
                messages.success(request, _('Conservative Ranking visibility saved.'))
            else:
                raise ValidationError(_('Invalid action.'))
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
        return redirect('contest_edit', contest=self.object.key)


class ConservativeRanking(ContestRankingBase):
    template_name = 'contest/conservative.html'
    tab = 'conservative'
    public_link = False

    def get_title(self):
        return '%s - %s' % (self.object.name, _('Conservative Ranking'))

    @cached_property
    def is_frozen(self):
        return bool(self.object.frozen_last_minutes and self.object._now >= self.object.frozen_time and not self.can_edit)

    def check_can_see_own_scoreboard(self):
        if not self.object.can_see_conservative_ranking(self.request.user):
            raise Http404()

    def get_rendered_ranking_table(self):
        contest = self.object
        own_only = not self.public_link and not contest.can_see_full_scoreboard(self.request.user)
        show_virtual = not self.public_link and self.request.GET.get('show_virtual') == 'true'
        version = cache.get('conservative:version:%s' % contest.pk, 'initial')
        scope = self.request.user.pk if own_only or self.can_edit else 'public'
        key = 'conservative:table:%s:%s:%s:%s:%s:%s:%s:%s:%s:%s' % (
            contest.pk, contest.conservative_revision, version, scope, self.is_frozen,
            show_virtual, self.request.LANGUAGE_CODE, contest.conservative_state,
            str(contest.has_hidden_subtasks) + str(contest.show_conservative_ranking) + (
                contest.frozen_time.isoformat() if self.is_frozen else ''), SCORING_VERSION)
        # Administrative cells contain CSRF tokens; never share/cache them.
        lock_owned = False
        if not self.can_edit:
            stored = cache.get(key)
            if stored and time.time() - stored['at'] < 5:
                return stored['html']
            lock_owned = cache.add(key + ':render', True, 10)
            if stored and not lock_owned:
                return stored['html']
        context = {'contest': contest, 'can_edit': self.can_edit, 'table_id': 'ranking-table',
                   'is_ICPC_format': False, 'is_frozen': self.is_frozen, 'has_rating': False,
                   'conservative_admin': self.request.user.is_superuser and not self.public_link}
        context['conservative_outdated'] = any(
            config.get('scoring_version') != SCORING_VERSION for config in contest.conservative_config.values())
        if contest.conservative_state == 'ON' and not context['conservative_outdated']:
            users, problems, total_ac, incomplete = self.ranking(own_only, show_virtual)
            context.update(users=users, problems=problems, total_ac=total_ac, conservative_incomplete=incomplete)
        rendered = render_to_string('contest/conservative-table.html', context, request=self.request)
        if not self.can_edit:
            cache.set(key, {'html': rendered, 'at': time.time()}, 60)
            if lock_owned:
                cache.delete(key + ':render')
        return rendered

    def ranking(self, own_only, show_virtual):
        contest = self.object
        problems = list(contest.contest_problems.select_related('problem').order_by('order', 'pk'))
        problem_map = {cp.pk: cp for cp in problems}
        participations = contest.users.filter(virtual__gt=ContestParticipation.SPECTATE).select_related('user__user')
        if not show_virtual:
            participations = participations.filter(virtual=ContestParticipation.LIVE)
        if own_only:
            participations = participations.filter(user=self.request.profile)
        participations = list(participations.prefetch_related(Prefetch('user__organizations',
            queryset=Organization.objects.filter(is_unlisted=False))).annotate(
                submission_count=Count('submission', filter=~aborted_filter('submission__submission__'))))
        best, incomplete = {}, False
        rows = ContestSubmission.objects.filter(participation_id__in=[p.pk for p in participations],
            submission__offline_hidden=False).exclude(submission__status__in=('QU', 'P', 'G')).exclude(aborted_filter())
        if self.is_frozen:
            rows = rows.filter(submission__date__lt=contest.frozen_time)
        valid = rows.filter(submission__status__in=('D', 'CE'),
                            conservative_data__generation=contest.conservative_revision,
                            conservative_data__scoring_version=SCORING_VERSION, conservative_data__status='valid')
        incomplete = rows.exclude(submission__status__in=('D', 'CE'),
            conservative_data__generation=contest.conservative_revision,
            conservative_data__scoring_version=SCORING_VERSION, conservative_data__status='valid').exists()
        fields = ['submission_id', 'problem_id', 'participation_id']
        fields += ['conservative_data'] if contest.has_hidden_subtasks else ['conservative_points']
        for row in valid.order_by('submission__date', 'submission_id').values(*fields).iterator():
            cp = problem_map.get(row['problem_id'])
            if cp is None:
                continue
            if contest.has_hidden_subtasks:
                hidden = {int(n.strip()) for n in cp.hidden_subtasks.split(',') if n.strip().isdigit() and int(n.strip()) > 0}
                points = visible_points(row['conservative_data'], cp, hidden)
            else:
                points = row['conservative_points']
            if points is None:
                incomplete = True
                continue
            pair = row['participation_id'], cp.pk
            if pair not in best or best[pair][0] < points:
                best[pair] = points, row['submission_id']
        users, total_ac = [], {str(cp.pk): 0 for cp in problems}
        for participation in participations:
            profile = participation.user
            cells, score = [], 0.0
            for cp in problems:
                result = best.get((participation.pk, cp.pk))
                if result is None:
                    cells.append(format_html('<td></td>'))
                    continue
                points, sid = result
                score += points
                if points == cp.points:
                    total_ac[str(cp.pk)] += 1
                cells.append(format_html('<td class="{state}"><a href="{url}">{points}</a></td>',
                    state='full-score' if points == cp.points else ('partial-score' if points else 'failed-score'),
                    url=reverse('submission_status', args=[sid]), points=floatformat(points, -contest.points_precision)))
            score = round(score, contest.points_precision)
            users.append(ContestRankingProfile(id=profile.pk, user=profile.user, css_class=profile.css_class,
                username=profile.username, display_name=profile.display_name, organization=profile.organization,
                points=score, cumtime=0, tiebreaker=0, virtual=participation.virtual, participation=participation,
                participation_rating=None, problem_cells=cells,
                result_cell=format_html('<td class="user-points">{}</td>', floatformat(score, -contest.points_precision))))
        users.sort(key=lambda u: (u.participation.is_disqualified, -u.points, u.username, u.virtual))
        ranked = list(ranker(users, key=lambda u: (u.participation.is_disqualified, u.points)))
        if own_only:
            ranked = [('???', u) for rank, u in ranked]
        return ranked, problems, total_ac, incomplete

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(has_rating=False, is_ICPC_format=False, cache_timeout=0,
                       show_virtual=self.request.GET.get('show_virtual') == 'true',
                       conservative_poll=True, is_public_ranking_link=self.public_link)
        return context


class PublicConservativeRanking(ConservativeRanking):
    public_link = True
    dispatch = PublicRankingView.dispatch
    get_object = PublicRankingView.get_object

    @property
    def can_edit(self):
        return False

    def check_can_see_own_scoreboard(self):
        if (not self.object.show_scoreboard or self.object.offline_mode or self.object.conservative_state == 'OFF'):
            raise Http404()
        if not self.object.show_conservative_ranking and not self.object.is_conservative_organizer(self.request.user):
            raise Http404()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(public_normal_ranking_url=reverse('public_ranking', args=[self.link.token]),
                       public_conservative_url=reverse('public_conservative_ranking', args=[self.link.token]))
        return context
