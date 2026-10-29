from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.translation import gettext as _

from judge.models import Problem
from judge.tasks.subtask_scoring import set_subtask_scoring
from judge.utils.views import generic_message


@login_required
def configure_subtask_scoring(request, problem):
    if not request.user.is_superuser:
        raise PermissionDenied
    target = get_object_or_404(Problem, code=problem)
    title = _('Cumulative subtask scoring')
    if request.method != 'POST':
        response = generic_message(request, title, _(
            'This address only accepts POST requests. '
            'Go back to the problem edit page and use the Enable or Disable button.'), status=405)
        response['Allow'] = 'POST'
        return response
    actions = request.POST.getlist('action')
    if not actions or (len(actions) == 1 and not actions[0].strip()):
        return generic_message(request, title, _(
            'No action was submitted: the "action" field is missing or empty. '
            'Go back to the problem edit page, reload it, and use the Enable or Disable button.'), status=400)
    if len(actions) != 1:
        return generic_message(request, title, _(
            'Multiple actions were submitted. Submit exactly one action: "enable" or "disable".'), status=400)
    action = actions[0]
    if action not in ('enable', 'disable'):
        return generic_message(request, title, _(
            'Unsupported action "%(action)s". Valid actions are "enable" (turn on and rebuild scores) '
            'and "disable" (turn off cumulative scoring).') % {'action': action}, status=400)
    try:
        set_subtask_scoring(target.pk, request.user, action == 'enable')
    except ValidationError as exc:
        return generic_message(request, _('Cumulative subtask scoring'), ' '.join(exc.messages), status=400)
    if request.POST.get('return_to') == 'admin':
        destination = reverse('admin:judge_problem_change', args=[target.pk])
    else:
        destination = reverse('problem_edit', args=[target.code])
    return HttpResponseRedirect(destination + '#subtask-score-controls')
