import hashlib
import hmac
import uuid

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from reversion import revisions

from judge.judgeapi import abort_submission, judge_submission
from judge.models.problem import Problem, SubmissionSourceAccess
from judge.models.profile import Profile
from judge.models.runtime import Language
from judge.utils.unicode import utf8bytes
from judge.utils.ai_code_detector import analyze_cpp_code

__all__ = ['SUBMISSION_RESULT', 'Submission', 'SubmissionSource', 'SubmissionTestCase']

SUBMISSION_RESULT = (
    ('AC', _('Accepted')),
    ('WA', _('Wrong Answer')),
    ('TLE', _('Time Limit Exceeded')),
    ('MLE', _('Memory Limit Exceeded')),
    ('OLE', _('Output Limit Exceeded')),
    ('IR', _('Invalid Return')),
    ('RTE', _('Runtime Error')),
    ('CE', _('Compile Error')),
    ('IE', _('Internal Error')),
    ('SC', _('Short Circuited')),
    ('AB', _('Aborted')),
)

SUBMISSION_STATUS = (
    ('QU', _('Queued')),
    ('P', _('Processing')),
    ('G', _('Grading')),
    ('D', _('Completed')),
    ('IE', _('Internal Error')),
    ('CE', _('Compile Error')),
    ('AB', _('Aborted')),
)

SUBMISSION_SEARCHABLE_STATUS = \
    SUBMISSION_RESULT + tuple([status for status in SUBMISSION_STATUS if status not in SUBMISSION_RESULT])


def _problem_data_local_usable(problem):
    from judge.models.problem_data import problem_data_storage

    try:
        data = problem.data_files
    except ObjectDoesNotExist:
        data = None
    if data is not None and data.zipfile and data.zipfile.name and problem_data_storage.exists(data.zipfile.name):
        return True
    return problem_data_storage.exists('%s/init.yml' % problem.code)


@revisions.register(follow=['test_cases'])
class Submission(models.Model):
    RESULT = SUBMISSION_RESULT
    STATUS = SUBMISSION_STATUS
    SEARCHABLE_STATUS = SUBMISSION_SEARCHABLE_STATUS
    IN_PROGRESS_GRADING_STATUS = ('QU', 'P', 'G')
    USER_DISPLAY_CODES = {
        'AC': _('Accepted'),
        'WA': _('Wrong Answer'),
        'SC': _('Short Circuited'),
        'TLE': _('Time Limit Exceeded'),
        'MLE': _('Memory Limit Exceeded'),
        'OLE': _('Output Limit Exceeded'),
        'IR': _('Invalid Return'),
        'RTE': _('Runtime Error'),
        'CE': _('Compile Error'),
        'IE': _('Internal Error (judging server error)'),
        'QU': _('Queued'),
        'P': _('Processing'),
        'G': _('Grading'),
        'D': _('Completed'),
        'AB': _('Aborted'),
    }

    user = models.ForeignKey(Profile, on_delete=models.CASCADE, db_index=False)
    problem = models.ForeignKey(Problem, on_delete=models.CASCADE, db_index=False)
    date = models.DateTimeField(verbose_name=_('submission time'), auto_now_add=True, db_index=True)
    time = models.FloatField(verbose_name=_('execution time'), null=True)
    memory = models.FloatField(verbose_name=_('memory usage'), null=True)
    points = models.FloatField(verbose_name=_('points granted'), null=True)
    language = models.ForeignKey(Language, verbose_name=_('submission language'),
                                 on_delete=models.CASCADE, db_index=False)
    status = models.CharField(verbose_name=_('status'), max_length=2, choices=STATUS, default='QU', db_index=True)
    result = models.CharField(verbose_name=_('result'), max_length=3, choices=SUBMISSION_RESULT,
                              default=None, null=True, blank=True)
    error = models.TextField(verbose_name=_('compile errors'), null=True, blank=True)
    current_testcase = models.IntegerField(default=0)
    batch = models.BooleanField(verbose_name=_('batched cases'), default=False)
    case_points = models.FloatField(verbose_name=_('test case points'), default=0)
    case_total = models.FloatField(verbose_name=_('test case total points'), default=0)
    judged_on = models.ForeignKey('Judge', verbose_name=_('judged on'), null=True, blank=True,
                                  on_delete=models.SET_NULL)
    judged_date = models.DateTimeField(verbose_name=_('submission judge time'), default=None, null=True)
    rejudged_date = models.DateTimeField(verbose_name=_('last rejudge date by admin'), null=True, blank=True)
    is_pretested = models.BooleanField(verbose_name=_('was ran on pretests only'), default=False)
    contest_object = models.ForeignKey('Contest', verbose_name=_('contest'), null=True, blank=True,
                                       on_delete=models.SET_NULL, related_name='+', db_index=False)
    locked_after = models.DateTimeField(verbose_name=_('submission lock'), null=True, blank=True)
    is_ai_generated = models.BooleanField(default=False, help_text='Whether the source code is marked as AI-generated.', verbose_name='Is AI-generated')
    reason_ai_generated = models.TextField(null=True,blank=True, help_text='The reason that source code is marked as AI-generated.', verbose_name='Reason AI-generated')

    @classmethod
    def result_class_from_code(cls, result, case_points, case_total):
        if result == 'AC':
            if case_points == case_total:
                return 'AC'
            return '_AC'
        return result

    @property
    def result_class(self):
        # This exists to save all these conditionals from being executed (slowly) in each row.html template
        if self.status in ('IE', 'CE'):
            return self.status
        return Submission.result_class_from_code(self.result, self.case_points, self.case_total)

    @property
    def memory_bytes(self):
        return self.memory * 1024 if self.memory is not None else 0

    @property
    def external_language_mapping(self):
        try:
            ext_sub = self.external_submission
        except ObjectDoesNotExist:
            return None
        raw_response = getattr(ext_sub, 'raw_response', None)
        if not isinstance(raw_response, dict):
            return None
        mapping = raw_response.get('_clueoj_selected_external_language')
        return mapping if isinstance(mapping, dict) else None

    @property
    def display_language_name(self):
        mapping = self.external_language_mapping
        if mapping:
            return (
                mapping.get('clueoj_key') or
                mapping.get('vjudge_name') or
                mapping.get('vjudge_id') or
                str(self.language)
            )
        return str(self.language)

    @property
    def display_language_short_name(self):
        mapping = self.external_language_mapping
        if mapping:
            return (
                mapping.get('clueoj_key') or
                mapping.get('vjudge_id') or
                mapping.get('vjudge_name') or
                self.language.short_display_name
            )
        return self.language.short_display_name

    @property
    def short_status(self):
        return self.result or self.status

    @property
    def long_status(self):
        return Submission.USER_DISPLAY_CODES.get(self.short_status, '')

    @cached_property
    def is_locked(self):
        return self.locked_after is not None and self.locked_after < timezone.now()

    def judge(self, *args, rejudge=False, force_judge=False, rejudge_user=None, ensure_ready_attempt=0, **kwargs):
        if force_judge or not self.is_locked:
            if getattr(settings, 'STORAGE_ENSURE_READY_ENABLED', False):
                from judge.utils.storage_client import READY_STATE_READY, READY_STATE_RESTORING, \
                    READY_STATE_UNAVAILABLE, ensure_problem_ready
                if type(self).objects.filter(pk=self.pk, status__in=('P', 'G')).exists():
                    return
                target = self.problem.mirror_root if self.problem.is_mirror and self.problem.mirror_root_id else self.problem
                idempotency_cache_key = 'storage:ensure-ready:idempotency:%s' % self.pk
                readiness_idempotency_key = cache.get(idempotency_cache_key)
                if not readiness_idempotency_key:
                    readiness_idempotency_key = 'submission:%s:%s' % (self.pk, uuid.uuid4())
                    cache.set(idempotency_cache_key, readiness_idempotency_key, 3600)
                readiness = ensure_problem_ready(target.pk, idempotency_key=readiness_idempotency_key)
                if readiness is True:
                    readiness = {'ready': True, 'state': READY_STATE_READY}
                elif readiness is False:
                    readiness = {'ready': False, 'state': READY_STATE_UNAVAILABLE}
                state = readiness.get('state')
                if readiness.get('ready') is True or state == READY_STATE_READY:
                    cache.delete('storage:ensure-ready:submission:%s' % self.pk)
                    cache.delete(idempotency_cache_key)
                elif state in (READY_STATE_RESTORING, READY_STATE_UNAVAILABLE):
                    if readiness.get('reset_idempotency'):
                        cache.delete(idempotency_cache_key)
                    if state == READY_STATE_UNAVAILABLE and getattr(
                        settings, 'STORAGE_ENSURE_READY_DEGRADED_DISPATCH', False,
                    ) and _problem_data_local_usable(target):
                        pass
                    else:
                        max_attempts = int(getattr(settings, 'STORAGE_ENSURE_READY_MAX_ATTEMPTS', 12))
                        if ensure_ready_attempt < max_attempts:
                            from judge.tasks.storage import storage_retry_judge_submission
                            next_attempt = ensure_ready_attempt + 1
                            base_delay = int(getattr(settings, 'STORAGE_ENSURE_READY_RETRY_BASE_SECONDS', 5))
                            max_delay = int(getattr(settings, 'STORAGE_ENSURE_READY_RETRY_MAX_SECONDS', 60))
                            countdown = min(max_delay, max(base_delay, base_delay * (2 ** ensure_ready_attempt)))
                            cache_key = 'storage:ensure-ready:submission:%s' % self.pk
                            if cache.add(cache_key, next_attempt, countdown + 5):
                                task_kwargs = {
                                    'attempt': next_attempt,
                                    'rejudge': rejudge,
                                    'judge_id': kwargs.get('judge_id'),
                                    'batch_rejudge': kwargs.get('batch_rejudge', False),
                                }
                                transaction.on_commit(lambda: storage_retry_judge_submission.apply_async(
                                    args=[self.pk], kwargs=task_kwargs, countdown=countdown,
                                ))
                            if self.status != 'QU':
                                self.status = 'QU'
                                self.save(update_fields=['status'])
                            return
                        self.status = 'IE'
                        self.error = _('Problem test data restore did not complete before judging timeout.')
                        self.judged_date = timezone.now()
                        self.save(update_fields=['status', 'error', 'judged_date'])
                        cache.delete('storage:ensure-ready:submission:%s' % self.pk)
                        cache.delete(idempotency_cache_key)
                        return
                else:
                    self.status = 'IE'
                    self.error = _('Problem test data is not ready for judging.')
                    self.judged_date = timezone.now()
                    self.save(update_fields=['status', 'error', 'judged_date'])
                    cache.delete(idempotency_cache_key)
                    return
            if rejudge:
                with revisions.create_revision(manage_manually=True):
                    if rejudge_user:
                        revisions.set_user(rejudge_user)
                    revisions.set_comment('Rejudged')
                    revisions.add_to_revision(self)
            storage_claimed = False
            if getattr(settings, 'STORAGE_ENSURE_READY_ENABLED', False):
                storage_claimed = type(self).objects.filter(pk=self.pk).exclude(status__in=('P', 'G')).update(
                    status='P',
                ) == 1
                if not storage_claimed:
                    return
                self.status = 'P'
            judge_submission(self, *args, rejudge=rejudge, storage_claimed=storage_claimed, **kwargs)
            lang_name = self.language.name.lower()
            if 'c++' in lang_name:
                result = analyze_cpp_code(self.source.source)
                if result['ai_generated']:
                    self.is_ai_generated = True
                    self.reason_ai_generated = '\n'.join(result['reason'])
                else:
                    self.reason_ai_generated = ''         
                
                self.save(update_fields=['is_ai_generated', 'reason_ai_generated']) 

    judge.alters_data = True

    def abort(self):
        abort_submission(self)

    abort.alters_data = True

    def can_see_detail(self, user):
        if not user.is_authenticated:
            return False
        profile = user.profile
        source_visibility = self.problem.submission_source_visibility
        if self.problem.is_editable_by(user):
            return True
        elif user.has_perm('judge.view_all_submission'):
            return True
        elif not self.problem.is_public and user.has_perm('judge.suggest_new_problem') and self.problem.is_suggesting:
            return True
        elif self.user_id == profile.id:
            return True
        elif source_visibility == SubmissionSourceAccess.ALWAYS:
            return True
        elif source_visibility == SubmissionSourceAccess.SOLVED and \
                (self.problem.is_public or self.problem.testers.filter(id=profile.id).exists()) and \
                self.problem.submission_set.filter(user_id=profile.id, result='AC',
                                                   points=self.problem.points).exists():
            return True
        elif source_visibility == SubmissionSourceAccess.ONLY_OWN and \
                self.problem.testers.filter(id=profile.id).exists():
            return True

        # If user is an author or curator of the contest the submission was made in
        if self.contest_object is not None and user.profile.id in self.contest_object.editor_ids:
            return True

        return False

    def update_contest(self):
        try:
            contest = self.contest
        except AttributeError:
            return

        contest_problem = contest.problem
        contest.points = round(self.case_points / self.case_total * contest_problem.points
                               if self.case_total > 0 else 0, 3)

        partial = (contest_problem.partial and contest_problem.problem.partial)
        if not partial and contest.points != contest_problem.points:
            contest.points = 0

        contest.save()
        contest.participation.recompute_results()

    update_contest.alters_data = True

    @property
    def is_graded(self):
        return self.status not in ('QU', 'P', 'G')

    @cached_property
    def contest_key(self):
        if hasattr(self, 'contest'):
            return self.contest_object.key

    def __str__(self):
        return _('Submission %(id)d of %(problem)s by %(user)s') % {
            'id': self.id, 'problem': self.problem, 'user': self.user.user.username,
        }

    def get_absolute_url(self):
        return reverse('submission_status', args=(self.id,))

    @cached_property
    def contest_or_none(self):
        try:
            return self.contest
        except ObjectDoesNotExist:
            return None

    @classmethod
    def get_id_secret(cls, sub_id):
        return (hmac.new(utf8bytes(settings.EVENT_DAEMON_SUBMISSION_KEY), b'%d' % sub_id, hashlib.sha512)
                    .hexdigest()[:16] + '%08x' % sub_id)

    @cached_property
    def id_secret(self):
        return self.get_id_secret(self.id)

    class Meta:
        permissions = (
            ('abort_any_submission', _('Abort any submission')),
            ('rejudge_submission', _('Rejudge the submission')),
            ('rejudge_submission_lot', _('Rejudge a lot of submissions')),
            ('spam_submission', _('Submit without limit')),
            ('view_all_submission', _('View all submission')),
            ('resubmit_other', _("Resubmit others' submission")),
            ('lock_submission', _('Change lock status of submission')),
        )
        verbose_name = _('submission')
        verbose_name_plural = _('submissions')

        indexes = [
            # Passive local-test eviction only asks whether a problem has a
            # submission newer than a cutoff. This avoids grouping/scanning
            # the submission table on every Celery sweep.
            models.Index(fields=['problem', '-date'], name='judge_sub_problem_date_idx'),

            # For problem submission rankings
            models.Index(fields=['problem', 'user', '-points', '-time']),

            # For contest problem submission rankings
            models.Index(fields=['contest_object', 'problem', 'user', '-points', '-time']),

            # For main submission list filtering by some combination of result and language
            models.Index(fields=['result', '-id']),
            models.Index(fields=['result', 'language', '-id']),
            models.Index(fields=['language', '-id']),

            # For filtered main submission list result charts
            models.Index(fields=['result', 'problem']),
            models.Index(fields=['language', 'problem', 'result']),

            # For problem submissions result chart
            models.Index(fields=['problem', 'result']),

            # For user_attempted_ids and own problem submissions result chart
            models.Index(fields=['user', 'problem', 'result']),

            # For user_completed_ids
            models.Index(fields=['user', 'result']),
        ]


class SubmissionSource(models.Model):
    submission = models.OneToOneField(Submission, on_delete=models.CASCADE, verbose_name=_('associated submission'),
                                      related_name='source')
    source = models.TextField(verbose_name=_('source code'), max_length=65536)

    def __str__(self):
        return _('Source of %(submission)s') % {'submission': self.submission}


@revisions.register()
class SubmissionTestCase(models.Model):
    RESULT = SUBMISSION_RESULT

    submission = models.ForeignKey(Submission, verbose_name=_('associated submission'), db_index=False,
                                   related_name='test_cases', on_delete=models.CASCADE)
    case = models.IntegerField(verbose_name=_('test case ID'))
    status = models.CharField(max_length=3, verbose_name=_('status flag'), choices=SUBMISSION_RESULT)
    time = models.FloatField(verbose_name=_('execution time'), null=True)
    memory = models.FloatField(verbose_name=_('memory usage'), null=True)
    points = models.FloatField(verbose_name=_('points granted'), null=True)
    total = models.FloatField(verbose_name=_('points possible'), null=True)
    batch = models.IntegerField(verbose_name=_('batch number'), null=True)
    feedback = models.CharField(max_length=50, verbose_name=_('judging feedback'), blank=True)
    extended_feedback = models.TextField(verbose_name=_('extended judging feedback'), blank=True)
    output = models.TextField(verbose_name=_('program output'), blank=True)

    @property
    def long_status(self):
        return Submission.USER_DISPLAY_CODES.get(self.status, '')

    @property
    def result_class(self):
        if self.status in ('IE', 'CE'):
            return self.status
        return Submission.result_class_from_code(self.status, self.points, self.total)

    class Meta:
        unique_together = ('submission', 'case')
        verbose_name = _('submission test case')
        verbose_name_plural = _('submission test cases')
