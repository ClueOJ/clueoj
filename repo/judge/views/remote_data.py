"""Submission-scoped access to manifests and existing storage presigned URLs."""
import json

from django.conf import settings
from django.core import signing
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from judge.models import Judge, Problem, Submission
from judge.utils import storage_client
from judge.utils.remote_data import TICKET_SALT, remote_ready
from judge.utils.test_data_lock import locked_problem


def _current(ticket):
    row = Submission.objects.select_related('problem', 'problem__mirror_of').filter(pk=ticket['submission']).first()
    if (not row or row.status not in Submission.IN_PROGRESS_GRADING_STATUS or
            (row.batch_scoring or {}).get('dispatch_id') != ticket['dispatch'] or
            row.judged_on_id != ticket['judge'] or not row.problem.is_locked or row.problem.is_manually_managed):
        return None
    if not Judge.objects.filter(pk=ticket['judge'], is_remote=True, online=True, is_blocked=False).exists():
        return None
    root = row.problem.mirror_of if row.problem.mirror_of_id else row.problem
    data = remote_ready(root, include_manifest=True)
    if root.pk != ticket['root'] or not data or data.sha256 != ticket['manifest']:
        return None
    return data


def _locked_current(ticket):
    try:
        with locked_problem(ticket['root']):
            return _current(ticket)
    except Problem.DoesNotExist:
        return None


@csrf_exempt
@require_POST
def remote_data(request):
    if not getattr(settings, 'REMOTE_JUDGING_ENABLED', False):
        return JsonResponse({'error': 'Remote grading is disabled.'}, status=503)
    try:
        body = json.loads(request.body)
        ticket = signing.loads(body['ticket'], salt=TICKET_SALT,
                               max_age=getattr(settings, 'REMOTE_DATA_TICKET_SECONDS', 3600))
        if not isinstance(ticket, dict) or not all(key in ticket for key in (
                'submission', 'dispatch', 'judge', 'root', 'manifest')):
            raise ValueError
        paths = body.get('paths', [])
        if not isinstance(paths, list) or len(paths) > 100 or any(not isinstance(path, str) for path in paths):
            raise ValueError
    except (ValueError, TypeError, KeyError, signing.BadSignature):
        return JsonResponse({'error': 'Invalid grading-data request.'}, status=403)
    # Only short DB reads under the publication lock; storage HTTP must not block Unlock.
    data = _locked_current(ticket)
    if data is None:
        return JsonResponse({'error': 'This grading run is no longer eligible.'}, status=409)
    manifest = data.manifest
    allowed = {item['path'] for item in manifest['files']}
    if any(path not in allowed for path in paths):
        return JsonResponse({'error': 'Path is outside the grading manifest.'}, status=403)
    urls = {}
    for path in set(paths):
        result = storage_client.request_download_url(ticket['root'], ttl_seconds=300, path=path)
        if result is None:
            return JsonResponse({'error': 'Grading data is not available from storage.'}, status=503)
        urls[path] = result
    if _locked_current(ticket) is None:
        return JsonResponse({'error': 'This grading run is no longer eligible.'}, status=409)
    payload = {'sha256': ticket['manifest'], 'downloads': urls}
    if not paths:
        payload['manifest'] = manifest
    response = JsonResponse(payload)
    response['Cache-Control'] = 'no-store'
    return response
