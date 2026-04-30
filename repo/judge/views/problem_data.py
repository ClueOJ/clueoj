import json
import mimetypes
import os
from itertools import chain
from zipfile import BadZipfile, ZipFile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.forms import BaseModelFormSet, CharField, ChoiceField, HiddenInput, ModelForm, NumberInput, Select, \
    formset_factory
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import DetailView

from judge.forms import FREE_ORGANIZATION_PLAN_MESSAGE
from judge.highlight_code import highlight_code
from judge.models import Problem, ProblemData, ProblemTestCase, Submission, problem_data_storage
from judge.models.problem_data import CUSTOM_CHECKERS, IO_METHODS
from judge.utils.problem_data import ProblemDataCompiler
from judge.utils.problem_mirror import get_mirrorable_source_queryset, get_problem_single_organization, \
    sync_mirror_archive_for_problem, validate_mirror_source_for_target
from judge.utils.unicode import utf8text
from judge.utils.views import TitleMixin, add_file_response, generic_message
from judge.views.problem import ProblemMixin
from judge.widgets import Select2Widget

mimetypes.init()
mimetypes.add_type('application/x-yaml', '.yml')


def _can_download_problem_data(user, problem):
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    root_problem = problem.mirror_root if problem.mirror_root_id else problem
    return root_problem.is_editable_by(user)


def checker_args_cleaner(self):
    data = self.cleaned_data['checker_args']
    if not data or data.isspace():
        return ''
    try:
        if not isinstance(json.loads(data), dict):
            raise ValidationError(_('Checker arguments must be a JSON object.'))
    except ValueError:
        raise ValidationError(_('Checker arguments is invalid JSON.'))
    return data


def grader_args_cleaner(self):
    data = self.cleaned_data['grader_args']
    if not data or data.isspace():
        return ''
    try:
        if not isinstance(json.loads(data), dict):
            raise ValidationError(_('Grader arguments must be a JSON object'))
    except ValueError:
        raise ValidationError(_('Grader arguments is invalid JSON'))
    return data


class ProblemDataForm(ModelForm):
    io_method = ChoiceField(choices=IO_METHODS, label=gettext_lazy('IO Method'), initial='standard', required=False,
                            widget=Select2Widget(attrs={'style': 'width: 200px'}))
    io_input_file = CharField(max_length=100, label=gettext_lazy('Input from file'), required=False)
    io_output_file = CharField(max_length=100, label=gettext_lazy('Output to file'), required=False)
    checker_type = ChoiceField(choices=CUSTOM_CHECKERS, widget=Select2Widget(attrs={'style': 'width: 200px'}))

    def clean_zipfile(self):
        if hasattr(self, 'zip_valid') and not self.zip_valid:
            raise ValidationError(_('Your zip file is invalid!'))
        return self.cleaned_data['zipfile']

    clean_checker_args = checker_args_cleaner
    clean_grader_args = grader_args_cleaner

    class Meta:
        model = ProblemData
        fields = [
            'zipfile',
            'grader', 'io_method', 'io_input_file', 'io_output_file',
            'custom_grader', 'custom_header', 'grader_args',
            'checker', 'custom_checker', 'checker_args', 'checker_type',
            'output_limit',
        ]
        widgets = {
            'checker_args': HiddenInput,
            'checker': Select2Widget(attrs={'style': 'width: 200px'}),
            'grader': Select2Widget(attrs={'style': 'width: 200px'}),
        }
        help_texts = {
            'output_limit': _('Can be left blank. In case the output can be too long (over 20MB), please set this.'),
        }


class ProblemCaseForm(ModelForm):
    clean_checker_args = checker_args_cleaner

    class Meta:
        model = ProblemTestCase
        fields = ('order', 'type', 'input_file', 'output_file', 'points',
                  'is_pretest',  # 'output_limit', 'output_prefix',
                  'checker', 'checker_args', 'generator_args')
        widgets = {
            'generator_args': HiddenInput,
            'type': Select(attrs={'style': 'width: 100%'}),
            'points': NumberInput(attrs={'style': 'width: 4em'}),
            # 'output_prefix': NumberInput(attrs={'style': 'width: 4.5em'}),
            # 'output_limit': NumberInput(attrs={'style': 'width: 6em'}),
            'checker_args': HiddenInput,
        }


class ProblemCaseFormSet(formset_factory(ProblemCaseForm, formset=BaseModelFormSet, extra=0, max_num=1,
                                         can_delete=True)):
    model = ProblemTestCase

    def __init__(self, *args, **kwargs):
        self.valid_files = kwargs.pop('valid_files', None)
        super(ProblemCaseFormSet, self).__init__(*args, **kwargs)

    def _construct_form(self, i, **kwargs):
        form = super(ProblemCaseFormSet, self)._construct_form(i, **kwargs)
        form.valid_files = self.valid_files
        return form


class ProblemMirrorForm(ModelForm):
    required_css_class = 'required'

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super(ProblemMirrorForm, self).__init__(*args, **kwargs)

        target_problem = self.instance if (self.instance and self.instance.pk) else None
        target_org = get_problem_single_organization(target_problem) if target_problem else None

        self.fields['mirror_of'].required = False
        self.fields['mirror_of'].empty_label = 'None'
        self.fields['mirror_of'].label_from_instance = lambda obj: '%s - %s' % (obj.code, obj.name)

        self.fields['mirror_of'].queryset = get_mirrorable_source_queryset(
            self.user,
            target_problem=target_problem,
            target_org=target_org,
        )
        if target_problem is not None and target_problem.mirror_of_id is not None:
            self.fields['mirror_of'].queryset = (
                self.fields['mirror_of'].queryset | Problem.objects.filter(pk=target_problem.mirror_of_id)
            ).distinct()
        choices = list(self.fields['mirror_of'].choices)
        if choices and choices[0][0] in ('', None):
            choices[0] = ('', 'None')
        else:
            choices.insert(0, ('', 'None'))
        self.fields['mirror_of'].choices = choices

    def clean_mirror_of(self):
        mirror_of = self.cleaned_data.get('mirror_of')
        target_problem = self.instance if (self.instance and self.instance.pk) else None
        target_org = get_problem_single_organization(target_problem) if target_problem else None
        validate_mirror_source_for_target(
            user=self.user,
            source=mirror_of,
            target_problem=target_problem,
            target_org=target_org,
        )
        return mirror_of

    class Meta:
        model = Problem
        fields = ('mirror_of',)
        widgets = {
            'mirror_of': Select2Widget(attrs={
                'style': 'width: 100%',
                'data-placeholder': 'None',
            }),
        }
        help_texts = {
            'mirror_of': _('Select a source problem to mirror test archive from. Leave empty to use this problem data.'),
        }


class ProblemManagerMixin(LoginRequiredMixin, ProblemMixin, DetailView):
    free_plan_readonly = False

    def allow_free_plan_readonly(self, problem):
        return False

    def get_object(self, queryset=None):
        self.free_plan_readonly = False
        problem = super(ProblemManagerMixin, self).get_object(queryset)
        if problem.is_manually_managed:
            raise Http404()
        if self.request.user.is_superuser:
            return problem

        if problem.is_blocked_by_free_organization_plan(self.request.user):
            if self.allow_free_plan_readonly(problem):
                self.free_plan_readonly = True
                return problem
            raise Http404()

        if problem.is_editable_by(self.request.user):
            return problem
        raise Http404()


class ProblemSubmissionDiff(TitleMixin, ProblemMixin, DetailView):
    template_name = 'problem/submission-diff.html'

    def get_title(self):
        return _('Comparing submissions for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Comparing submissions for {0}')).format(
            format_html('<a href="{1}">{0}</a>', self.object.name, reverse('problem_detail', args=[self.object.code])),
        ))

    def get_object(self, queryset=None):
        problem = super(ProblemSubmissionDiff, self).get_object(queryset)
        if problem.is_editable_by(self.request.user):
            return problem
        raise Http404()

    def get_context_data(self, **kwargs):
        context = super(ProblemSubmissionDiff, self).get_context_data(**kwargs)

        subs = None
        if 'username' in self.request.GET:
            usernames = self.request.GET.getlist('username')
            subs = Submission.objects.filter(problem=self.object, user__user__username__in=usernames)
        elif 'id' in self.request.GET:
            ids = self.request.GET.getlist('id')
            subs = Submission.objects.filter(problem=self.object, id__in=ids)

        if not subs:
            raise Submission.DoesNotExist()

        subs = subs.order_by('id')
        context['submissions'] = subs.filter(language__file_only=False)

        # If we have associated data we can do better than just guess
        data = ProblemTestCase.objects.filter(dataset=self.object, type='C')
        if data:
            num_cases = data.count()
        else:
            num_cases = subs.first().test_cases.count()
        context['num_cases'] = num_cases
        return context

    def get(self, request, *args, **kwargs):
        try:
            return super(ProblemSubmissionDiff, self).get(request, *args, **kwargs)
        except Submission.DoesNotExist:
            return generic_message(self.request, _('No such submissions'), _('Could not find any submissions.'))


class ProblemDataView(TitleMixin, ProblemManagerMixin):
    template_name = 'problem/data.html'

    def allow_free_plan_readonly(self, problem):
        return self.request.method in ('GET', 'HEAD') and \
            problem.can_download_data_as_free_organization_admin(self.request.user)

    def get_title(self):
        return _('Editing data for {0}').format(self.object.name)

    def get_content_title(self):
        return mark_safe(escape(_('Editing data for %s')) % (
            format_html('<a href="{1}">{0}</a>', self.object.name,
                        reverse('problem_detail', args=[self.object.code]))))

    def get_data_form(self, post=False):
        form = ProblemDataForm(data=self.request.POST if post else None, prefix='problem-data',
                               files=self.request.FILES if post else None,
                               instance=ProblemData.objects.get_or_create(problem=self.object)[0])
        if self.object.is_mirror:
            form.fields['zipfile'].disabled = True
            form.fields['zipfile'].help_text = _(
                'This problem mirrors another root test archive, so archive upload is managed automatically.',
            )
        return form

    def get_case_formset(self, files, post=False):
        return ProblemCaseFormSet(data=self.request.POST if post else None, prefix='cases', valid_files=files,
                                  queryset=ProblemTestCase.objects.filter(dataset_id=self.object.pk).order_by('order'))

    def get_mirror_form(self, post=False):
        return ProblemMirrorForm(
            data=self.request.POST if post else None,
            prefix='mirror',
            instance=self.object,
            user=self.request.user,
        )

    def get_valid_files(self, data, post=False):
        try:
            if post and 'problem-data-zipfile-clear' in self.request.POST:
                return []
            elif post and 'problem-data-zipfile' in self.request.FILES:
                return ZipFile(self.request.FILES['problem-data-zipfile']).namelist()
            elif data.zipfile:
                return ZipFile(data.zipfile.path).namelist()
        except (BadZipfile, OSError):
            return []
        return []

    def _has_invalid_case_file_mapping(self, valid_files):
        valid_file_set = set(valid_files or [])
        case_queryset = ProblemTestCase.objects.filter(dataset_id=self.object.pk, type='C').only('input_file', 'output_file')
        for case in case_queryset:
            if case.input_file not in valid_file_set or case.output_file not in valid_file_set:
                return True
        return False

    def _sync_mirror_before_render_if_needed(self, data_form, valid_files):
        if not self.object.is_mirror:
            return data_form, valid_files

        if not valid_files:
            needs_sync = ProblemTestCase.objects.filter(dataset_id=self.object.pk, type='C').exists()
        else:
            needs_sync = self._has_invalid_case_file_mapping(valid_files)
        if not needs_sync:
            return data_form, valid_files

        sync_mirror_archive_for_problem(
            self.object, bootstrap_cases_if_empty=True, heal_missing_files=True, force_regenerate=True,
        )
        fresh_form = self.get_data_form()
        fresh_valid_files = self.get_valid_files(fresh_form.instance)
        return fresh_form, fresh_valid_files

    def _save_cases_formset(self, problem, cases_formset):
        for case in cases_formset.save(commit=False):
            case.dataset_id = problem.id
            case.save()
        for case in cases_formset.deleted_objects:
            case.delete()

    def get_context_data(self, **kwargs):
        context = super(ProblemDataView, self).get_context_data(**kwargs)
        if 'data_form' not in context:
            data_form = self.get_data_form()
            valid_files = self.get_valid_files(data_form.instance)
            data_form, valid_files = self._sync_mirror_before_render_if_needed(data_form, valid_files)
            context['data_form'] = data_form
            context['valid_files'] = valid_files
            context['data_form'].zip_valid = valid_files is not False
            context['cases_formset'] = self.get_case_formset(valid_files)
        if 'mirror_form' not in context:
            context['mirror_form'] = self.get_mirror_form()
        context['valid_files_json'] = mark_safe(json.dumps(context['valid_files']))
        context['valid_files'] = set(context['valid_files'])
        context['all_case_forms'] = chain(context['cases_formset'], [context['cases_formset'].empty_form])

        if self.request.user.has_perm('judge.create_mass_testcases'):
            context['testcase_limit'] = 9999
            context['testcase_soft_limit'] = 9999
        else:
            context['testcase_limit'] = settings.VNOJ_TESTCASE_HARD_LIMIT
            context['testcase_soft_limit'] = settings.VNOJ_TESTCASE_SOFT_LIMIT
        context['is_readonly_free_plan'] = self.free_plan_readonly
        context['free_plan_message'] = FREE_ORGANIZATION_PLAN_MESSAGE
        context['is_mirror_problem'] = self.object.is_mirror
        context['mirror_source'] = self.object.mirror_of
        context['mirror_root'] = self.object.mirror_root
        context['mirror_dependents_count'] = kwargs.get('mirror_dependents_count', 0)
        context['show_mirror_root_warning'] = kwargs.get('show_mirror_root_warning', False)
        return context

    def _archive_change_requested(self):
        return 'problem-data-zipfile' in self.request.FILES or 'problem-data-zipfile-clear' in self.request.POST

    def check_valid(self, mirror_form, data_form, cases_formset):
        if not mirror_form.is_valid() or not data_form.is_valid() or not cases_formset.is_valid():
            return False
        number_of_cases = cases_formset.total_form_count() - len(cases_formset.deleted_forms)
        if number_of_cases > settings.VNOJ_TESTCASE_HARD_LIMIT and \
           not self.request.user.has_perm('judge.create_mass_testcases'):
            error = ValidationError(
                _('Too many testcases, number of testcases must not exceed %s') % settings.VNOJ_TESTCASE_HARD_LIMIT,
                code='too_many_testcases',
            )
            cases_formset._non_form_errors.append(error)
            return False
        return True

    def post(self, request, *args, **kwargs):
        problem = get_object_or_404(Problem, code=kwargs.get(self.slug_url_kwarg))
        if problem.is_blocked_by_free_organization_plan(request.user):
            return generic_message(request, _('Free organization plan'), FREE_ORGANIZATION_PLAN_MESSAGE, status=403)

        self.object = problem = self.get_object()
        mirror_form = self.get_mirror_form(post=True)
        data_form = self.get_data_form(post=True)
        valid_files = self.get_valid_files(data_form.instance, post=True)
        data_form.zip_valid = valid_files is not False
        cases_formset = self.get_case_formset(valid_files, post=True)
        archive_change_requested = self._archive_change_requested()

        if archive_change_requested and problem.is_mirror:
            data_form.add_error('zipfile', _(
                'Mirror problems cannot upload archives directly. Update the root problem archive instead.',
            ))

        mirror_dependents_count = 0
        if archive_change_requested and not problem.is_mirror:
            mirror_dependents_count = Problem.objects.filter(mirror_root_id=problem.id).exclude(pk=problem.id).count()
            if mirror_dependents_count and request.POST.get('confirm_mirror_root_archive_update') != '1':
                data_form.add_error(
                    None,
                    _('This problem is mirrored by %(count)d other problem(s). '
                      'Please confirm before replacing the shared root archive.') % {
                        'count': mirror_dependents_count,
                    },
                )

        if self.check_valid(mirror_form, data_form, cases_formset):
            previous_mirror_of_id = problem.mirror_of_id
            problem = mirror_form.save()
            mirror_changed = previous_mirror_of_id != problem.mirror_of_id

            data = data_form.save()
            self._save_cases_formset(problem, cases_formset)

            if mirror_changed and previous_mirror_of_id and not problem.mirror_of_id:
                # Explicitly removing mirror should detach shared archive immediately.
                data.zipfile = None
                data.save(update_fields=['zipfile'])
                ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order'), [])
                return HttpResponseRedirect(request.get_full_path())
            if problem.is_mirror:
                # Mirror problems share archive with root, but keep editable testcase structure locally.
                sync_mirror_archive_for_problem(
                    problem,
                    bootstrap_cases_if_empty=mirror_changed,
                    heal_missing_files=True,
                    force_regenerate=True,
                )
                return HttpResponseRedirect(request.get_full_path())
            if mirror_changed:
                problem.refresh_from_db()
            ProblemDataCompiler.generate(problem, data, problem.cases.order_by('order'), valid_files)
            return HttpResponseRedirect(request.get_full_path())
        return self.render_to_response(self.get_context_data(mirror_form=mirror_form,
                                                             data_form=data_form, cases_formset=cases_formset,
                                                             valid_files=valid_files,
                                                             mirror_dependents_count=mirror_dependents_count,
                                                             show_mirror_root_warning=bool(mirror_dependents_count)))

    put = post


@login_required
def problem_data_file(request, problem, path):
    object = get_object_or_404(Problem, code=problem)
    if not _can_download_problem_data(request.user, object):
        raise Http404()

    problem_dir = problem_data_storage.path(problem)
    if os.path.commonpath((problem_data_storage.path(os.path.join(problem, path)), problem_dir)) != problem_dir:
        raise Http404()

    response = HttpResponse()

    if hasattr(settings, 'DMOJ_PROBLEM_DATA_INTERNAL'):
        url_path = '%s/%s/%s' % (settings.DMOJ_PROBLEM_DATA_INTERNAL, problem, path)
    else:
        url_path = None

    try:
        add_file_response(request, response, url_path, os.path.join(problem, path), problem_data_storage)
    except IOError:
        raise Http404()

    response['Content-Type'] = 'application/octet-stream'
    return response


@login_required
def problem_init_view(request, problem):
    problem = get_object_or_404(Problem, code=problem)
    if not _can_download_problem_data(request.user, problem):
        raise Http404()

    try:
        with problem_data_storage.open(os.path.join(problem.code, 'init.yml'), 'rb') as f:
            data = utf8text(f.read()).rstrip('\n')
    except IOError:
        raise Http404()

    return render(request, 'problem/yaml.html', {
        'raw_source': data, 'highlighted_source': highlight_code(data, 'yaml'),
        'title': _('Generated init.yml for %s') % problem.name,
        'content_title': mark_safe(escape(_('Generated init.yml for %s')) % (
            format_html('<a href="{1}">{0}</a>', problem.name,
                        reverse('problem_detail', args=[problem.code])))),
    })
