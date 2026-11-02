from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from judge.models import Problem


def validate_mirror_source_for_target(user, source, target_problem=None):
    if source is None:
        return

    validate_mirror_structure(source.pk, target_problem.pk if target_problem else None)
    if not user or not user.is_authenticated:
        raise ValidationError(_('You must be able to edit a problem to select it as a new mirror source.'))
    # A saved relationship is the grant, including when another editor cannot
    # view the source. Only a newly selected source requires edit permission.
    if user.is_superuser or (target_problem and target_problem.pk and Problem.objects.filter(
            pk=target_problem.pk, mirror_of_id=source.pk).exists()):
        return

    if not source.is_editable_by(user):
        raise ValidationError(_('You must be able to edit a problem to select it as a new mirror source.'))


def validate_mirror_structure(source_id, target_id=None):
    if not source_id:
        return
    if source_id == target_id:
        raise ValidationError(_('A problem cannot mirror itself.'))
    source = Problem.objects.filter(pk=source_id).first()
    if source is None:
        raise ValidationError(_('Mirror source problem does not exist.'))
    if source.mirror_of_id is not None:
        raise ValidationError(_('Mirror sources must be root problems. Only one mirror level is allowed.'))
    if source.has_external_problem:
        raise ValidationError(_('Virtual Judge problems cannot be used as mirror sources.'))
    if source.subtask_tests_locked:
        from judge.utils.subtask_scoring import LOCKED_MESSAGE
        raise ValidationError(LOCKED_MESSAGE)
    if target_id and Problem.objects.filter(mirror_of_id=target_id).exists():
        raise ValidationError(_('A problem with mirrors cannot itself become a mirror.'))


def resolve_mirror_root_id(mirror_of_id, current_problem_id=None):
    if not mirror_of_id:
        return None

    validate_mirror_structure(mirror_of_id, current_problem_id)
    return mirror_of_id


def test_data_source(problem):
    """Return the problem whose complete dataset the judge executes."""
    if not problem.is_mirror:
        return problem
    return problem.mirror_of


def mirror_problem_ids(problem_id):
    return {problem_id, *Problem.objects.filter(mirror_of_id=problem_id).values_list('pk', flat=True)}


def get_mirrorable_source_queryset(user, target_problem=None):
    if user is None or not user.is_authenticated:
        return Problem.objects.none()

    queryset = (Problem.objects.all() if user.is_superuser else Problem.get_editable_problems(user)).defer('description')
    queryset = queryset.filter(mirror_of__isnull=True, subtask_score_state='OFF').exclude(external_problem__is_active=True)

    if target_problem is not None and target_problem.pk:
        if Problem.objects.filter(mirror_of_id=target_problem.pk).exists():
            return Problem.objects.none()
        queryset = queryset.exclude(pk=target_problem.pk)

    return queryset.distinct()
