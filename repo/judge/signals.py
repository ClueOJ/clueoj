import errno
import os
from typing import Optional

from django.conf import settings
from django.contrib.flatpages.models import FlatPage
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.db import transaction
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete, pre_save
from django.dispatch import receiver
from registration.models import RegistrationProfile
from registration.signals import user_registered

from judge.caching import finished_submission
from judge.models import BlogPost, Comment, Contest, ContestAnnouncement, ContestSubmission, EFFECTIVE_MATH_ENGINES, \
    ExamTag, ExamTagProblemPoint, Judge, Language, License, MiscConfig, Organization, Problem, ProblemData, Profile, Submission, \
    WebAuthnCredential
from judge.tasks import on_new_comment, rebuild_exam_progress_for_exam, rebuild_exams_snapshots, \
    sync_exam_progress_for_user_problem
from judge.utils.problem_mirror import sync_mirror_archive_for_problem, sync_mirror_archives_for_root
from judge.views.register import RegistrationView


def get_pdf_path(basename: str) -> Optional[str]:
    if not settings.DMOJ_PDF_PROBLEM_CACHE:
        return None

    return os.path.join(settings.DMOJ_PDF_PROBLEM_CACHE, basename)


def unlink_if_exists(file):
    try:
        os.unlink(file)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def queue_exams_snapshot_rebuild():
    def _enqueue():
        # Debounce repeated model save events in short bursts.
        if cache.add('exams:snapshot:queued', 1, 10):
            result = rebuild_exams_snapshots.apply_async(countdown=2)
            cache.set('exams:snapshot:last_task', result.id, 86400)

    transaction.on_commit(_enqueue)


def queue_exam_progress_rebuild(exam_tag_id):
    if not exam_tag_id:
        return
    cache_key = f'exams:progress:queued:{exam_tag_id}'

    def _enqueue():
        if cache.add(cache_key, 1, 10):
            rebuild_exam_progress_for_exam.apply_async(args=[exam_tag_id], countdown=2)

    transaction.on_commit(_enqueue)


def _exam_tag_ids_for_problem(problem_id):
    exam_tag_ids = set(
        Problem.exam_tags.through.objects
        .filter(problem_id=problem_id)
        .values_list('examtag_id', flat=True),
    )
    exam_tag_ids |= set(
        ExamTagProblemPoint.objects
        .filter(problem_id=problem_id)
        .values_list('exam_tag_id', flat=True),
    )
    return exam_tag_ids


@receiver(post_save, sender=Problem)
def problem_update(sender, instance, **kwargs):
    if hasattr(instance, '_updating_stats_only'):
        return

    related_problem_ids = set(getattr(instance, '_mirror_cache_related_ids', set()) or set())
    related_problem_ids.add(instance.id)

    cache.delete_many([
        make_template_fragment_key('submission_problem', (problem_id,))
        for problem_id in related_problem_ids
    ] + [
        make_template_fragment_key('problem_feed', (problem_id,))
        for problem_id in related_problem_ids
    ] + [
        'problem_tls:%s' % problem_id
        for problem_id in related_problem_ids
    ] + [
        'problem_mls:%s' % problem_id
        for problem_id in related_problem_ids
    ])
    cache.delete_many([
        make_template_fragment_key('problem_html', (problem_id, engine, lang))
        for problem_id in related_problem_ids
        for lang, _ in settings.LANGUAGES
        for engine in EFFECTIVE_MATH_ENGINES
    ])
    cache.delete_many([
        make_template_fragment_key('problem_authors', (problem_id, lang))
        for problem_id in related_problem_ids
        for lang, _ in settings.LANGUAGES
    ])
    cache.delete_many([
        'generated-meta-problem:%s:%d' % (lang, problem_id)
        for problem_id in related_problem_ids
        for lang, _ in settings.LANGUAGES
    ])

    for problem_id in related_problem_ids:
        problem_code = instance.code
        if problem_id != instance.id:
            problem_code = Problem.objects.filter(pk=problem_id).values_list('code', flat=True).first()
        if not problem_code:
            continue
        for lang, _ in settings.LANGUAGES:
            cached_pdf_filename = get_pdf_path('%s.%s.pdf' % (problem_code, lang))
            if cached_pdf_filename is not None:
                unlink_if_exists(cached_pdf_filename)
    queue_exams_snapshot_rebuild()
    for exam_tag_id in _exam_tag_ids_for_problem(instance.id):
        queue_exam_progress_rebuild(exam_tag_id)


@receiver(pre_delete, sender=Problem)
def problem_pre_delete(sender, instance, **kwargs):
    instance._exam_tag_ids_before_delete = _exam_tag_ids_for_problem(instance.id)


@receiver(post_delete, sender=Problem)
def problem_delete(sender, instance, **kwargs):
    queue_exams_snapshot_rebuild()
    exam_tag_ids = set(getattr(instance, '_exam_tag_ids_before_delete', set()))
    for exam_tag_id in exam_tag_ids:
        queue_exam_progress_rebuild(exam_tag_id)


@receiver(post_save, sender=ProblemData)
def problem_data_update(sender, instance, **kwargs):
    if not getattr(instance, '_zipfile_changed', False):
        return

    problem = instance.problem
    if problem.mirror_of_id:
        transaction.on_commit(lambda: sync_mirror_archive_for_problem(
            problem, bootstrap_cases_if_empty=False, heal_missing_files=True, force_regenerate=True,
        ))
        return

    if Problem.objects.filter(mirror_root_id=problem.id).exclude(pk=problem.id).exists():
        transaction.on_commit(lambda: sync_mirror_archives_for_root(problem))


@receiver(post_save, sender=Profile)
def profile_update(sender, instance, **kwargs):
    if hasattr(instance, '_updating_stats_only'):
        return

    cache.delete_many([make_template_fragment_key('user_about', (instance.id, engine))
                       for engine in EFFECTIVE_MATH_ENGINES])


@receiver(post_delete, sender=WebAuthnCredential)
def webauthn_delete(sender, instance, **kwargs):
    profile = instance.user
    if profile.webauthn_credentials.count() == 0:
        profile.is_webauthn_enabled = False
        profile.save(update_fields=['is_webauthn_enabled'])


@receiver(post_save, sender=Contest)
def contest_update(sender, instance, **kwargs):
    if hasattr(instance, '_updating_stats_only'):
        return

    cache.delete_many(['generated-meta-contest:%d' % instance.id] +
                      [make_template_fragment_key('contest_html', (instance.id, engine))
                       for engine in EFFECTIVE_MATH_ENGINES])


@receiver(post_save, sender=ExamTag)
def exam_tag_update(sender, instance, **kwargs):
    queue_exams_snapshot_rebuild()


@receiver(post_delete, sender=ExamTag)
def exam_tag_delete(sender, instance, **kwargs):
    queue_exams_snapshot_rebuild()


@receiver(post_save, sender=License)
def license_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key('license_html', (instance.id,)))


@receiver(post_save, sender=Language)
def language_update(sender, instance, **kwargs):
    cache.delete_many([make_template_fragment_key('language_html', (instance.id,)),
                       'lang:cn_map'])


@receiver(post_save, sender=Judge)
def judge_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key('judge_html', (instance.id,)))


@receiver(post_save, sender=Comment)
def comment_update(sender, instance, created, **kwargs):
    cache.delete('comment_feed:%d' % instance.id)
    if not created:
        return
    on_new_comment.delay(instance.id)


@receiver(post_save, sender=BlogPost)
def post_update(sender, instance, **kwargs):
    cache.delete_many([
        make_template_fragment_key('post_summary', (instance.id,)),
        'blog_slug:%d' % instance.id,
        'blog_feed:%d' % instance.id,
    ])
    cache.delete_many([make_template_fragment_key('post_content', (instance.id, engine))
                       for engine in EFFECTIVE_MATH_ENGINES])


@receiver(post_delete, sender=Submission)
def submission_delete(sender, instance, **kwargs):
    finished_submission(instance)
    instance.user._updating_stats_only = True
    instance.user.calculate_points()
    instance.problem._updating_stats_only = True
    instance.problem.update_stats()
    if _exam_tag_ids_for_problem(instance.problem_id):
        sync_exam_progress_for_user_problem.delay(instance.user_id, instance.problem_id)


@receiver(post_delete, sender=ContestSubmission)
def contest_submission_delete(sender, instance, **kwargs):
    participation = instance.participation
    participation.recompute_results()
    Submission.objects.filter(id=instance.submission_id).update(contest_object=None)


@receiver(post_save, sender=Organization)
def organization_update(sender, instance, **kwargs):
    cache.delete_many([make_template_fragment_key('organization_html', (instance.id, engine))
                       for engine in EFFECTIVE_MATH_ENGINES])


@receiver(m2m_changed, sender=Organization.admins.through)
def organization_admin_update(sender, instance, action, **kwargs):
    if action == 'post_add':
        pks = kwargs.get('pk_set') or set()
        for profile in Profile.objects.filter(pk__in=pks):
            if profile.organizations.filter(pk=instance.pk).exists():
                continue
            if instance.has_reached_member_limit():
                continue
            profile.organizations.add(instance)


@receiver(post_save, sender=MiscConfig)
def misc_config_update(sender, instance, **kwargs):
    cache.delete('misc_config')


@receiver(post_delete, sender=MiscConfig)
def misc_config_delete(sender, instance, **kwargs):
    cache.delete('misc_config')


@receiver(post_save, sender=ContestSubmission)
def contest_submission_update(sender, instance, **kwargs):
    Submission.objects.filter(id=instance.submission_id).update(contest_object_id=instance.participation.contest_id)


@receiver(post_save, sender=FlatPage)
def flatpage_update(sender, instance, **kwargs):
    cache.delete(make_template_fragment_key('flatpage', (instance.url, )))


@receiver(m2m_changed, sender=Profile.organizations.through)
def profile_organization_update(sender, instance, action, reverse, **kwargs):
    orgs_to_be_updated = Organization.objects.none()
    if action == 'pre_clear':
        if reverse:
            instance._organization_ids_before_clear = {instance.pk}
        else:
            instance._organization_ids_before_clear = set(
                instance.organizations.values_list('pk', flat=True),
            )
        return

    if action in ('post_add', 'post_remove'):
        if reverse:
            orgs_to_be_updated = Organization.objects.filter(pk=instance.pk)
        else:
            pks = kwargs.get('pk_set') or set()
            orgs_to_be_updated = Organization.objects.filter(pk__in=pks)
    elif action == 'post_clear':
        org_ids = getattr(instance, '_organization_ids_before_clear', set())
        orgs_to_be_updated = Organization.objects.filter(pk__in=org_ids)
        if hasattr(instance, '_organization_ids_before_clear'):
            delattr(instance, '_organization_ids_before_clear')

    for org in orgs_to_be_updated:
        org.on_user_changes()


@receiver(m2m_changed, sender=Problem.exam_tags.through)
def problem_exam_tags_update(sender, instance, action, reverse, pk_set, **kwargs):
    if action == 'pre_clear':
        if reverse:
            instance._exam_progress_problem_ids_before_clear = set(
                instance.problems.values_list('id', flat=True),
            )
        else:
            instance._exam_progress_exam_tag_ids_before_clear = set(
                instance.exam_tags.values_list('id', flat=True),
            )
        return

    if action not in ('post_add', 'post_remove', 'post_clear'):
        return

    exam_tag_ids = set()

    if action == 'post_add':
        if reverse:
            problem_points = dict(Problem.objects.filter(id__in=pk_set).values_list('id', 'points'))
            rows = [
                ExamTagProblemPoint(
                    exam_tag_id=instance.id,
                    problem_id=problem_id,
                    points=float(problem_points.get(problem_id, 0) or 0),
                )
                for problem_id in pk_set
            ]
            exam_tag_ids.add(instance.id)
        else:
            default_points = float(instance.points or 0)
            rows = [
                ExamTagProblemPoint(
                    exam_tag_id=exam_tag_id,
                    problem_id=instance.id,
                    points=default_points,
                )
                for exam_tag_id in pk_set
            ]
            exam_tag_ids |= set(pk_set)
        if rows:
            ExamTagProblemPoint.objects.bulk_create(rows, ignore_conflicts=True)

    elif action == 'post_remove':
        if reverse:
            ExamTagProblemPoint.objects.filter(exam_tag_id=instance.id, problem_id__in=pk_set).delete()
            exam_tag_ids.add(instance.id)
        else:
            ExamTagProblemPoint.objects.filter(problem_id=instance.id, exam_tag_id__in=pk_set).delete()
            exam_tag_ids |= set(pk_set)

    else:  # post_clear
        if reverse:
            ExamTagProblemPoint.objects.filter(exam_tag_id=instance.id).delete()
            exam_tag_ids.add(instance.id)
            instance._exam_progress_problem_ids_before_clear = set()
        else:
            exam_tag_ids = set(getattr(instance, '_exam_progress_exam_tag_ids_before_clear', set()))
            ExamTagProblemPoint.objects.filter(problem_id=instance.id).delete()
            instance._exam_progress_exam_tag_ids_before_clear = set()

    queue_exams_snapshot_rebuild()
    for exam_tag_id in exam_tag_ids:
        queue_exam_progress_rebuild(exam_tag_id)


@receiver(pre_save, sender=ExamTagProblemPoint)
def exam_tag_problem_point_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        return
    old_pair = (
        ExamTagProblemPoint.objects
        .filter(pk=instance.pk)
        .values_list('exam_tag_id', 'problem_id')
        .first()
    )
    instance._old_exam_tag_problem_pair = old_pair


@receiver(post_save, sender=ExamTagProblemPoint)
def exam_tag_problem_point_update(sender, instance, **kwargs):
    through_model = Problem.exam_tags.through
    old_pair = getattr(instance, '_old_exam_tag_problem_pair', None)
    if old_pair and old_pair != (instance.exam_tag_id, instance.problem_id):
        through_model.objects.filter(examtag_id=old_pair[0], problem_id=old_pair[1]).delete()

    through_model.objects.get_or_create(examtag_id=instance.exam_tag_id, problem_id=instance.problem_id)
    queue_exams_snapshot_rebuild()
    queue_exam_progress_rebuild(instance.exam_tag_id)


@receiver(post_delete, sender=ExamTagProblemPoint)
def exam_tag_problem_point_delete(sender, instance, **kwargs):
    Problem.exam_tags.through.objects.filter(
        examtag_id=instance.exam_tag_id,
        problem_id=instance.problem_id,
    ).delete()
    queue_exams_snapshot_rebuild()
    queue_exam_progress_rebuild(instance.exam_tag_id)


@receiver(post_save, sender=ContestAnnouncement)
def contest_announcement_create(sender, instance, created, **kwargs):
    if not created:
        return

    instance.send()


@receiver(user_registered, sender=RegistrationView)
def registration_user_registered(sender, user, request, **kwargs):
    """Automatically activate user if SEND_ACTIVATION_EMAIL is False"""

    if not getattr(settings, 'SEND_ACTIVATION_EMAIL', True):
        # get should never fail here
        # but if it does, we won't catch it so it can show up in our log
        profile = RegistrationProfile.objects.get(user=user)

        user.is_active = True
        profile.activated = True

        with transaction.atomic():
            user.save()
            profile.save()
