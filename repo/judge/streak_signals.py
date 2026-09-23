from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver

from judge.models import Problem, Profile, Submission, StreakProblemState, Organization
from judge.utils.streaks import enabled, queue_pair, queue_scope


@receiver(post_save, sender=Submission)
def streak_submission_saved(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not enabled():
        return
    relevant = {'status', 'result', 'points', 'case_points', 'case_total', 'is_pretested'}
    if update_fields is not None and not relevant.intersection(update_fields):
        return
    if instance.status in ('D', 'CE', 'IE', 'AB') and not instance.offline_hidden:
        queue_pair(instance.user_id, instance.problem_id, instance)


@receiver(post_delete, sender=Submission)
def streak_submission_deleted(sender, instance, **kwargs):
    if enabled() and StreakProblemState.objects.filter(user_id=instance.user_id, problem_id=instance.problem_id).exists():
        queue_pair(instance.user_id, instance.problem_id)


@receiver(pre_save, sender=Problem)
def streak_problem_before(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not enabled() or not instance.pk:
        return
    fields = {'is_public', 'is_organization_private', 'points', 'partial'}
    if update_fields is not None and not fields.intersection(update_fields):
        return
    old = Problem.objects.filter(pk=instance.pk).values(*fields).first()
    instance._streak_changed = old and any(old[f] != getattr(instance, f) for f in fields)


@receiver(post_save, sender=Problem)
def streak_problem_saved(sender, instance, **kwargs):
    if getattr(instance, '_streak_changed', False):
        queue_scope('problem', instance.pk)
        instance._streak_changed = False


@receiver(post_delete, sender=Problem)
def streak_problem_deleted(sender, instance, **kwargs):
    queue_scope('problem', instance.pk)


@receiver(m2m_changed, sender=Problem.organizations.through)
def streak_problem_orgs(sender, instance, action, reverse, pk_set, **kwargs):
    if not enabled():
        return
    if reverse and action == 'pre_clear':
        instance._streak_problem_ids = list(instance.problem_set.values_list('pk', flat=True))
    if action in ('post_add', 'post_remove', 'post_clear'):
        ids = (pk_set if action != 'post_clear' else getattr(instance, '_streak_problem_ids', [])) if reverse else [instance.pk]
        for problem_id in ids:
            queue_scope('problem', problem_id)


@receiver(pre_save, sender=Profile)
def streak_timezone_before(sender, instance, raw=False, update_fields=None, **kwargs):
    if raw or not enabled() or not instance.pk or (update_fields is not None and 'timezone' not in update_fields):
        return
    old = Profile.objects.filter(pk=instance.pk).values_list('timezone', flat=True).first()
    instance._streak_timezone_changed = old is not None and old != instance.timezone


@receiver(post_save, sender=Profile)
def streak_timezone_saved(sender, instance, **kwargs):
    if getattr(instance, '_streak_timezone_changed', False):
        queue_scope('user', instance.pk)
        instance._streak_timezone_changed = False


@receiver(pre_delete, sender=Organization)
def streak_organization_deleted(sender, instance, **kwargs):
    if enabled():
        for problem_id in instance.problem_set.values_list('pk', flat=True).iterator():
            queue_scope('problem', problem_id)
