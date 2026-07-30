import base64
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger('judge.storage')

DEFAULT_SCHEMA_VERSION = 1
READY_STATE_READY = 'ready'
READY_STATE_RESTORING = 'restoring'
READY_STATE_NOT_READY = 'not_ready'
READY_STATE_UNAVAILABLE = 'unavailable'

_service_token_lock = threading.Lock()
_service_token_cache = {'token': None, 'refresh_at': 0}


class StorageClientError(Exception):
    pass


def _base_url():
    return getattr(settings, 'STORAGE_SERVICE_BASE_URL', 'http://storage-web:2907/api/v1').rstrip('/')


def _token():
    secret = getattr(settings, 'STORAGE_CLUEOJ_SERVICE_SECRET', '')
    if secret:
        return _minted_service_token(secret)
    return getattr(settings, 'STORAGE_SERVICE_TOKEN', '')


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _minted_service_token(secret):
    now = int(time.time())
    skew = int(getattr(settings, 'STORAGE_SERVICE_TOKEN_SKEW_SECONDS', 30))
    cached = _service_token_cache.get('token')
    if cached and int(_service_token_cache.get('refresh_at') or 0) > now + skew:
        return cached
    with _service_token_lock:
        cached = _service_token_cache.get('token')
        if cached and int(_service_token_cache.get('refresh_at') or 0) > now + skew:
            return cached
        ttl = max(60, int(getattr(settings, 'STORAGE_SERVICE_TOKEN_TTL_SECONDS', 300)))
        issuer = getattr(settings, 'STORAGE_SERVICE_ISSUER', 'clueoj-storage')
        audience = getattr(settings, 'STORAGE_SERVICE_AUDIENCE', 'clueoj-storage')
        subject = getattr(settings, 'STORAGE_SERVICE_SUBJECT', 'clueoj')
        scopes = getattr(settings, 'STORAGE_SERVICE_SCOPES', ('read', 'mutate', 'downloads:issue'))
        if isinstance(scopes, str):
            scopes = [scope.strip() for scope in scopes.split(',') if scope.strip()]
        header = {'alg': 'HS256', 'typ': 'JWT'}
        payload = {
            'iss': issuer,
            'aud': audience,
            'sub': subject,
            'iat': now,
            'nbf': now - skew,
            'exp': now + ttl,
            'kind': 'service',
            'role': 'storage-admin',
            'scopes': list(scopes),
        }
        signing_input = '%s.%s' % (
            _b64url(json.dumps(header, separators=(',', ':')).encode('utf-8')),
            _b64url(json.dumps(payload, separators=(',', ':')).encode('utf-8')),
        )
        signature = hmac.new(str(secret).encode('utf-8'), signing_input.encode('ascii'), hashlib.sha256).digest()
        token = '%s.%s' % (signing_input, _b64url(signature))
        _service_token_cache['token'] = token
        _service_token_cache['refresh_at'] = now + ttl - skew
        return token


def _timeout():
    return getattr(settings, 'STORAGE_SERVICE_TIMEOUT', 10)


def _expected_schema_version():
    return getattr(settings, 'STORAGE_SERVICE_SCHEMA_VERSION', DEFAULT_SCHEMA_VERSION)


def expected_schema_version():
    return _expected_schema_version()


def _headers(request_id=None):
    headers = {
        'Authorization': f'Bearer {_token()}',
        'X-Storage-Audience': getattr(settings, 'STORAGE_SERVICE_AUDIENCE', 'clueoj-storage'),
        'X-Storage-Schema-Version': str(_expected_schema_version()),
        'Accept': 'application/json',
    }
    if request_id:
        headers['X-Request-ID'] = request_id
    return headers


def _json_headers(request_id=None):
    return {
        **_headers(request_id=request_id),
        'Content-Type': 'application/json',
    }


def _validate_schema(data):
    version = data.get('schema_version') or data.get('schemaVersion')
    if version is not None and int(version) != int(_expected_schema_version()):
        raise StorageClientError('storage schema version mismatch')


def problem_catalog_payload(problem):
    """Return storage catalog metadata for the current Problem contract."""
    owner_id = problem.storage_owner_organization_id
    if owner_id is None and problem.organizations.count() == 1:
        owner_id = problem.organizations.values_list('pk', flat=True)[0]
    return {
        'problem_pk': problem.pk,
        'external_id': str(problem.pk),
        'code': problem.code,
        'owner_organization_id': owner_id,
        'is_manually_managed': problem.is_manually_managed,
        'mirror_of_external_id': str(problem.mirror_of_id) if problem.mirror_of_id else None,
        'mirror_root_external_id': str(problem.mirror_root_id) if problem.mirror_root_id else None,
        'schema_version': _expected_schema_version(),
    }


def notify_problem_dirty(problem_id, code, mutation_id=None, catalog=None):
    """Non-blocking POST /api/v1/problems/<id>/dirty.

    Failure is non-fatal — the storage app watcher/reconcile will catch up.
    Only called when STORAGE_CATALOG_SYNC_ENABLED is True.
    """
    if not getattr(settings, 'STORAGE_CATALOG_SYNC_ENABLED', False):
        return
    if not _token():
        return
    if mutation_id is None:
        mutation_id = str(uuid.uuid4())
    try:
        resp = requests.post(
            f'{_base_url()}/problems/{problem_id}/dirty',
            headers={**_json_headers(request_id=mutation_id), 'Idempotency-Key': mutation_id},
            json={
                'problem_pk': problem_id,
                'external_id': str(problem_id),
                'code': code,
                'mutation_id': mutation_id,
                'schema_version': _expected_schema_version(),
                'catalog': catalog,
            },
            timeout=_timeout(),
        )
        if resp.status_code >= 400:
            logger.warning('Storage dirty notification for problem %s returned HTTP %s', problem_id, resp.status_code)
            resp.raise_for_status()
    except Exception:
        logger.warning('Failed to notify storage app of problem %s dirty: ', problem_id, exc_info=True)


def notify_problem_dirty_on_commit(problem, mutation_id=None):
    from django.db import transaction

    payload_problem_id = problem.pk
    payload_code = problem.code
    catalog = problem_catalog_payload(problem)
    transaction.on_commit(lambda: notify_problem_dirty(
        payload_problem_id, payload_code, mutation_id=mutation_id, catalog=catalog,
    ))


def notify_problem_deleted_on_commit(problem, mutation_id=None):
    from django.db import transaction

    payload_problem_id = problem.pk
    payload_code = problem.code
    catalog = {
        'problem_pk': problem.pk,
        'external_id': str(problem.pk),
        'code': problem.code,
        'owner_organization_id': problem.storage_owner_organization_id,
        'is_manually_managed': problem.is_manually_managed,
        'mirror_of_external_id': str(problem.mirror_of_id) if problem.mirror_of_id else None,
        'mirror_root_external_id': str(problem.mirror_root_id) if problem.mirror_root_id else None,
        'schema_version': _expected_schema_version(),
        'event_kind': 'delete',
        'catalog_state': 'deleted',
        'deleted': True,
        'deleted_at': timezone.now().isoformat(),
    }
    transaction.on_commit(lambda: notify_problem_dirty(
        payload_problem_id, payload_code, mutation_id=mutation_id, catalog=catalog,
    ))


def get_sync_changes(cursor=None, limit=500):
    """GET /api/v1/sync/changes — pull incremental catalog changes.

    Returns (changes, next_cursor, has_more) or (None, None, False) on error.
    """
    if not _token():
        return None, None, False
    params = {'limit': limit}
    if cursor:
        params['cursor'] = cursor
    try:
        request_id = str(uuid.uuid4())
        resp = requests.get(
            f'{_base_url()}/sync/changes',
            headers=_headers(request_id=request_id),
            params=params,
            timeout=_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        _validate_schema(data)
        if 'changes' not in data or 'next_cursor' not in data or 'has_more' not in data:
            raise StorageClientError('storage changes response missing changes/next_cursor/has_more')
        changes = data['changes']
        if not isinstance(changes, list):
            raise StorageClientError('storage changes is not a list')
        return changes, data.get('next_cursor'), bool(data.get('has_more'))
    except Exception:
        logger.warning('Failed to fetch sync changes: ', exc_info=True)
        return None, None, False


def ensure_problem_ready(problem_id, idempotency_key=None):
    if not _token():
        return {'ready': False, 'state': READY_STATE_UNAVAILABLE, 'status_code': None}
    try:
        request_id = idempotency_key or str(uuid.uuid4())
        resp = requests.post(
            f'{_base_url()}/problems/{problem_id}/ensure-ready',
            headers={**_json_headers(request_id=request_id), 'Idempotency-Key': request_id},
            json={'problem_external_id': str(problem_id), 'schema_version': _expected_schema_version()},
            timeout=_timeout(),
        )
        status_code = resp.status_code
        data = resp.json()
        if isinstance(data, dict):
            _validate_schema(data)
        else:
            data = {}
        status = str(data.get('status') or data.get('state') or '').lower()
        if status_code == 200 and (data.get('ready') is True or status == 'ready'):
            return {'ready': True, 'state': READY_STATE_READY, 'status_code': status_code, 'payload': data}
        if status_code == 202 or status in ('restoring', 'restore_pending', 'pending'):
            return {
                'ready': False,
                'state': READY_STATE_RESTORING,
                'status_code': status_code,
                'job_id': data.get('job_id') or data.get('jobId'),
                'payload': data,
            }
        if status_code == 409 and data.get('code') == 'ensure_ready_job_terminal' and data.get('retryable') is True:
            return {
                'ready': False,
                'state': READY_STATE_UNAVAILABLE,
                'status_code': status_code,
                'reset_idempotency': True,
                'payload': data,
            }
        if status_code == 409 and data.get('retryable') is True:
            return {
                'ready': False,
                'state': READY_STATE_UNAVAILABLE,
                'status_code': status_code,
                'payload': data,
            }
        if status_code == 409 or status in ('failed', 'error', 'missing', 'not_ready'):
            return {'ready': False, 'state': READY_STATE_NOT_READY, 'status_code': status_code, 'payload': data}
        if status_code >= 400:
            resp.raise_for_status()
        return {'ready': False, 'state': READY_STATE_UNAVAILABLE, 'status_code': status_code, 'payload': data}
    except Exception:
        logger.warning('Failed to ensure storage readiness for problem %s: ', problem_id, exc_info=True)
        return {'ready': False, 'state': READY_STATE_UNAVAILABLE, 'status_code': None}


def request_download_url(problem_external_id, ttl_seconds=180):
    """POST /api/v1/downloads — request a short-TTL R2 presigned GET URL.

    Returns dict with 'url', 'expires_at' or None on error.
    """
    if not _token():
        return None
    try:
        request_id = str(uuid.uuid4())
        resp = requests.post(
            f'{_base_url()}/downloads',
            headers={**_json_headers(request_id=request_id), 'Idempotency-Key': request_id},
            json={
                'problem_external_id': str(problem_external_id),
                'ttl_seconds': ttl_seconds,
                'schema_version': _expected_schema_version(),
            },
            timeout=_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        _validate_schema(data)
        return data
    except Exception:
        logger.warning('Failed to request download URL for problem %s: ', problem_external_id, exc_info=True)
        return None


def get_storage_volumes():
    """GET /api/v1/storage/volumes — get volume metrics for system status."""
    if not _token():
        return None
    try:
        resp = requests.get(
            f'{_base_url()}/storage/volumes',
            headers=_headers(request_id=str(uuid.uuid4())),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            _validate_schema(data)
            if 'items' in data:
                items = data.get('items')
                if not isinstance(items, list):
                    raise StorageClientError('storage volumes envelope items is not a list')
                return items
            if any(key in data for key in ('total_bytes', 'free_bytes', 'available_bytes')):
                return [data]
            raise StorageClientError('storage volumes response missing items')
        if isinstance(data, list):
            return data
        raise StorageClientError('storage volumes response must be an envelope or list')
    except Exception:
        logger.warning('Failed to fetch storage volumes: ', exc_info=True)
        return None


def get_organization_usage(organization_external_id):
    """GET /api/v1/organizations/<id>/usage — authoritative org quota/accounting."""
    if not _token():
        return None
    try:
        resp = requests.get(
            f'{_base_url()}/organizations/{organization_external_id}/usage',
            headers=_headers(request_id=str(uuid.uuid4())),
            timeout=_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            _validate_schema(data)
            return data
        raise StorageClientError('organization usage response must be an object')
    except Exception:
        logger.warning('Failed to fetch organization usage for %s: ', organization_external_id, exc_info=True)
        return None


def reconcile_catalog(problems=None):
    """POST /api/v1/catalog/problems:reconcile — trigger full catalog reconciliation."""
    if not _token():
        return None
    try:
        request_id = str(uuid.uuid4())
        payload = {
            'schema_version': _expected_schema_version(),
            'observed_at': timezone.now().isoformat(),
        }
        if problems is not None:
            payload['problems'] = [problem_catalog_payload(problem) for problem in problems]
        resp = requests.post(
            f'{_base_url()}/catalog/problems:reconcile',
            headers={**_json_headers(request_id=request_id), 'Idempotency-Key': request_id},
            json=payload,
            timeout=_timeout(),
        )
        resp.raise_for_status()
        data = resp.json()
        _validate_schema(data)
        return data
    except Exception:
        logger.warning('Failed to trigger catalog reconcile: ', exc_info=True)
        return None
