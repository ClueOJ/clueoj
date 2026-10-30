from celery import Task, shared_task
from django.core.exceptions import ValidationError
from django.db.models import Q

__all__ = ['build_conservative', 'refresh_conservative_submission', 'rebuild_active_conservative']


class ConservativeBuildTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        from judge.models import Contest
        from judge.utils.conservative import invalidate
        contest_id, revision = args
        Contest.objects.filter(pk=contest_id, conservative_revision=revision, conservative_state='BUILDING').update(
            conservative_state='FAILED', conservative_error=str(exc)[:4000])
        invalidate(contest_id)


class ConservativeRefreshTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        from judge.models import Contest, ContestSubmission
        cs_id, revision = args
        row = ContestSubmission.objects.filter(pk=cs_id, conservative_data__generation=revision,
            conservative_data__status='pending').values('participation__contest_id', 'submission_id').first()
        if row:
            message = 'The score update failed. Rebuild Conservative Ranking after checking the worker.'
            ContestSubmission.objects.filter(pk=cs_id, conservative_data__generation=revision,
                conservative_data__status='pending').update(conservative_points=None, conservative_data={
                    'generation': revision, 'status': 'error', 'error': message})
            Contest.objects.filter(pk=row['participation__contest_id'], conservative_revision=revision,
                conservative_state__in=('BUILDING', 'ON')).update(
                    conservative_error='#%s: %s' % (row['submission_id'], message))


@shared_task(base=ConservativeRefreshTask, autoretry_for=(Exception,), retry_backoff=True, max_retries=5, acks_late=True)
def refresh_conservative_submission(cs_id, revision):
    from judge.utils.conservative import refresh_one
    return refresh_one(cs_id, revision)


@shared_task(base=ConservativeBuildTask, autoretry_for=(Exception,), retry_backoff=True, max_retries=5, acks_late=True)
def build_conservative(contest_id, revision):
    from judge.models import Contest, ContestSubmission
    from judge.utils.conservative import aborted_filter, invalidate, problem_layout, refresh_one, same_subtasks
    contest = Contest.objects.filter(pk=contest_id, conservative_revision=revision, conservative_state='BUILDING').first()
    if contest is None:
        return
    cutoff = contest.conservative_progress['cutoff']
    cursor = contest.conservative_progress['cursor']
    rows = ContestSubmission.objects.filter(participation__contest_id=contest_id)
    eligible = rows.exclude(aborted_filter())
    # Reconcile ignored rows separately so a resumed older build also clears
    # obsolete abort errors, without loading or validating their testcase data.
    aborted = rows.filter(aborted_filter()).filter(
        Q(conservative_data__generation__isnull=True) | Q(conservative_data__status__isnull=True) |
        ~Q(conservative_data__generation=revision) | ~Q(conservative_data__status='ignored'))
    for cs_id in aborted.values_list('pk', flat=True).iterator(chunk_size=100):
        refresh_one(cs_id, revision)
    while True:
        if not Contest.objects.filter(pk=contest_id, conservative_revision=revision, conservative_state='BUILDING').exists():
            return
        ids = list(eligible.filter(pk__gt=cursor, pk__lte=cutoff).order_by('pk').values_list('pk', flat=True)[:100])
        if not ids:
            break
        layouts = {cp.pk: problem_layout(cp.problem)[0]
                   for cp in contest.contest_problems.select_related('problem')}
        for cs_id in ids:
            refresh_one(cs_id, revision, layouts)
        cursor = ids[-1]
        progress = {'cursor': cursor, 'cutoff': cutoff, 'done': rows.filter(pk__lte=cursor).count(),
                    'total': rows.filter(pk__lte=cutoff).count(),
                    'errors': eligible.filter(conservative_data__generation=revision, conservative_data__status='error').count(),
                    'ignored': rows.filter(pk__lte=cutoff).filter(aborted_filter()).count()}
        # A duplicate delivery may finish later. It must not move the cursor back.
        Contest.objects.filter(pk=contest_id, conservative_revision=revision, conservative_state='BUILDING',
                               conservative_progress__cursor__lte=cursor).update(conservative_progress=progress)
    errors = eligible.filter(conservative_data__generation=revision, conservative_data__status='error')
    samples = list(errors.values_list('submission_id', 'conservative_data')[:20])
    message = '\n'.join('#%s: %s' % (sid, data['error']) for sid, data in samples)
    # Testcase counts/identities may vary; subtask counts and maxima must agree.
    for cp in contest.contest_problems.select_related('problem'):
        saved = contest.conservative_config.get(str(cp.pk), {})
        if not same_subtasks(saved.get('layout'), problem_layout(cp.problem)[0]):
            message = 'Subtask counts or maximum points changed during the build. Rebuild Conservative Ranking.'
    total = rows.filter(pk__lte=cutoff).count()
    progress = {'cursor': cutoff, 'cutoff': cutoff, 'done': total, 'total': total,
                'errors': errors.count(), 'ignored': rows.filter(pk__lte=cutoff).filter(aborted_filter()).count()}
    Contest.objects.filter(pk=contest_id, conservative_revision=revision, conservative_state='BUILDING').update(
        conservative_state='FAILED' if message else 'ON', conservative_error=message, conservative_progress=progress)
    invalidate(contest_id)


@shared_task(autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def rebuild_active_conservative(contest_id):
    from judge.models import Contest
    from judge.utils.conservative import start_build
    row = Contest.objects.filter(pk=contest_id, conservative_state__in=('BUILDING', 'ON')).values('conservative_revision').first()
    if row:
        try:
            start_build(contest_id)
        except ValidationError as exc:
            Contest.objects.filter(pk=contest_id, conservative_revision=row['conservative_revision']).exclude(conservative_state='OFF').update(
                conservative_state='FAILED', conservative_error=' '.join(exc.messages))
