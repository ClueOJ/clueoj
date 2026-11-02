import uuid

from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods

from judge.models import Problem, ProblemInvocation, ProblemInvocationItem, ProblemInvocationSource
from judge.models.invocation import INVOCATION_EXPECTATIONS
from judge.utils.invocation_report import build_report
from judge.utils.invocation_storage import source_storage
from judge.utils.invocations import (create_invocation, delete_sources, enqueue_invocation, full_layout,
                                     invocation_languages, require_invocations, save_source_settings, upload_sources)


def invocation_problem(request, code):
    problem = get_object_or_404(Problem, code=code)
    require_invocations(request.user, problem)
    return problem


def invocation_page_context(problem):
    heading = _('Invocations for {}')
    link = format_html('<a href="{}">{}</a>', reverse('problem_detail', args=[problem.code]), problem.name)
    return {'problem': problem, 'title': heading.format(problem.name), 'content_title': format_html(heading, link),
            'can_invocations': True, 'invocations_active': True}


@require_http_methods(['GET', 'POST'])
def invocation_sources(request, problem):
    problem = invocation_problem(request, problem)
    errors = []
    bound_settings = {}
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'upload':
                upload_sources(request.user, problem, request.FILES.getlist('sources'))
            elif action == 'delete_sources':
                delete_sources(request.user, problem, request.POST.getlist('source'))
            elif action == 'save_settings':
                settings = []
                for source_id in request.POST.getlist('settings_source'):
                    prefix = 'subtask-%s-' % source_id
                    values = {'source': source_id, 'language': request.POST.get('language-' + source_id, ''),
                              'overall': request.POST.get('overall-' + source_id, ''),
                              'subtasks': {key[len(prefix):]: value for key, value in request.POST.items()
                                           if key.startswith(prefix) and value}}
                    settings.append(values)
                    bound_settings[source_id] = values
                save_source_settings(request.user, problem, settings)
            elif action == 'run':
                invocation = create_invocation(request.user, problem, request.POST.getlist('source'),
                                               request.POST.get('request_key'))
                return redirect('problem_invocation', problem.code, invocation.pk)
            else:
                raise ValidationError('Unknown action.')
            return redirect('problem_invocations', problem.code)
        except ValidationError as exc:
            errors = exc.messages
    sources = list(problem.invocation_sources.select_related('language', 'uploaded_by'))
    layout = full_layout(problem)
    groups = [row for row in layout or [] if row['batch']]
    source_rows = []
    for source in sources:
        values = bound_settings.get(str(source.pk), {})
        source_rows.append({
            'source': source, 'language_id': str(values.get('language', source.language_id) or ''),
            'overall': values.get('overall', source.expected_overall), 'groups': [
                {'batch': group['batch'], 'mode': group['mode'],
                 'expected': values.get('subtasks', source.expected_subtasks).get(str(group['batch']), '')}
                for group in groups], 'stale': bool(source.expected_subtasks and source.expected_layout != layout)})
    return render(request, 'problem/invocations.html', {
        **invocation_page_context(problem), 'errors': errors,
        'source_rows': source_rows, 'groups': groups, 'languages': list(invocation_languages(problem)),
        'expectations': INVOCATION_EXPECTATIONS, 'request_key': uuid.uuid4(),
        'invocations': problem.invocations.select_related('created_by')[:50],
    }, status=400 if errors else 200)


def invocation_source(request, problem, source_id):
    problem = invocation_problem(request, problem)
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])
    source = get_object_or_404(ProblemInvocationSource, pk=source_id, problem=problem)
    try:
        stream = source_storage().open(source.path, 'rb')
    except (OSError, ValidationError) as exc:
        raise Http404() from exc
    return FileResponse(stream, as_attachment=True, filename=source.filename,
                        content_type='text/plain; charset=utf-8')


@require_http_methods(['GET', 'POST'])
def invocation_detail(request, problem, invocation_id):
    problem = invocation_problem(request, problem)
    invocation = get_object_or_404(ProblemInvocation, pk=invocation_id, problem=problem)
    if request.method == 'POST':
        if request.POST.get('action') != 'retry':
            return JsonResponse({'error': 'Unknown action.'}, status=400)
        # Retry dispatch only; a deliberate new run is created from the library.
        ProblemInvocationItem.objects.filter(invocation=invocation, submission__status__in=('QU', 'IE')).update(
            dispatch_state='pending', dispatch_error='')
        enqueue_invocation(invocation.pk)
        return redirect('problem_invocation', problem.code, invocation.pk)
    return render(request, 'problem/invocation-report.html', {
        **invocation_page_context(problem), 'invocation': invocation, 'report': build_report(invocation),
    })


@require_http_methods(['GET'])
def invocation_status(request, problem, invocation_id):
    problem = invocation_problem(request, problem)
    invocation = get_object_or_404(ProblemInvocation, pk=invocation_id, problem=problem)
    report = build_report(invocation)
    return JsonResponse({'finished': report['finished'], 'html': render_to_string(
        'problem/invocation-table.html', {'report': report}, request=request)})
