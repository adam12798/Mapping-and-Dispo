"""Twilio webhook signature verification.

STAGE 1 (current): verify the X-Twilio-Signature header on webhook requests,
RECORD the outcome, and reject nothing. Every verification — pass and fail —
writes one GHLWebhookLog row, because Railway's application-log stream has
proven unreliable (it stopped ingesting for 9+ hours on 2026-08-31 while the
app ran fine), and an empty log cannot distinguish "no failures" from "no
traffic". The database is the system of record for the stage-2 gate; the log
lines below are kept as a convenience only.

Read the recorded results with:  python manage.py signature_watch

Log lines (unchanged, best-effort):
    TWILIO_SIG_FAIL   — signature present but did not validate
    TWILIO_SIG_SKIP   — verification skipped (no auth token configured)
    TWILIO_SIG_ERROR  — the verifier itself blew up

STAGE 2 (a separate later change) flips the decorator to return 403 on
failure, once signature_watch reports a clean gate on genuine traffic.

The signature is an HMAC-SHA1 of the exact public URL Twilio requested
(scheme, host, path, query string) plus the sorted POST params, keyed with the
account auth token — same algorithm as twilio.request_validator. Railway
terminates TLS at its proxy, so the URL must be rebuilt from the forwarded
proto/host headers, not from what the app server saw on its side of the proxy.
"""
import base64
import hashlib
import hmac
import json
import logging
from functools import wraps

from django.conf import settings

logger = logging.getLogger('twilio_signature')

# How signature rows are marked inside the shared GHLWebhookLog table, without
# touching the model (adding a webhook_type choice would force a migration):
#   source       — the exact-match marker every query filters on
#   webhook_type — a value deliberately outside WEBHOOK_TYPE_CHOICES; Django
#                  enforces choices only in full_clean(), never on save()
#   organization — left NULL: the org is not resolved until inside the view,
#                  and NULL keeps these rows out of both orgs' /ghl-debug/ page
SIGNATURE_LOG_SOURCE = 'twilio-signature'
SIGNATURE_LOG_TYPE = 'signature'

# error_message prefixes, so signature_watch can classify without re-parsing prose
FAIL_UNSIGNED = 'missing X-Twilio-Signature header'
FAIL_MISMATCH = 'signature mismatch'


def _signature_url(request):
    """The URL Twilio signed: public scheme + host + full path with query."""
    proto = request.headers.get('X-Forwarded-Proto', '').split(',')[0].strip()
    if not proto:
        proto = 'https' if request.is_secure() else 'http'
    host = request.headers.get('X-Forwarded-Host', '').split(',')[0].strip()
    if not host:
        host = request.get_host()
    return f'{proto}://{host}{request.get_full_path()}'


def _expected_signature(url, post_params, auth_token):
    payload = url + ''.join(k + v for k, v in sorted(post_params.items()))
    digest = hmac.new(
        auth_token.encode('utf-8'), payload.encode('utf-8'), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode('ascii')


def verify_twilio_signature(request):
    """Returns (valid, detail): True/False, or None when unverifiable."""
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not auth_token:
        return None, 'TWILIO_AUTH_TOKEN not configured'
    provided = request.headers.get('X-Twilio-Signature', '')
    if not provided:
        return False, FAIL_UNSIGNED
    url = _signature_url(request)
    post_params = dict(request.POST.items()) if request.method == 'POST' else {}
    expected = _expected_signature(url, post_params, auth_token)
    if hmac.compare_digest(expected, provided):
        return True, url
    return False, f'{FAIL_MISMATCH} for url={url}'


def _record_signature_result(request, valid, detail):
    """Persist one row per verification attempt. Never raises — a logging
    failure must not change how a Twilio webhook is handled."""
    try:
        from maps.models import GHLWebhookLog

        outcome = 'pass' if valid else ('skip' if valid is None else 'fail')
        # Deliberately NOT the request body: it carries lead names, addresses
        # and message text. Only what diagnosing a signature problem needs.
        summary = json.dumps({
            'outcome': outcome,
            'path': request.path,
            'method': request.method,
            'from': request.POST.get('From', '') or request.GET.get('From', ''),
            'has_signature': bool(request.headers.get('X-Twilio-Signature')),
            'signed_url': _signature_url(request),
        })
        GHLWebhookLog.objects.create(
            organization=None,
            direction='inbound',
            webhook_type=SIGNATURE_LOG_TYPE,
            source=SIGNATURE_LOG_SOURCE,
            lead_name='',
            url=_signature_url(request)[:500],
            payload=summary,
            response_status=200,
            success=bool(valid),
            error_message='' if valid else (detail or ''),
        )
    except Exception:
        logger.exception('TWILIO_SIG_DBLOG_ERROR path=%s', request.path)


def twilio_signature_log_only(view_func):
    """Stage 1 decorator: verify, record, never reject (see module docstring)."""
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        try:
            valid, detail = verify_twilio_signature(request)
            if valid is None:
                logger.warning('TWILIO_SIG_SKIP path=%s detail=%s', request.path, detail)
            elif not valid:
                sender = request.POST.get('From', '') or request.GET.get('From', '')
                logger.warning(
                    'TWILIO_SIG_FAIL path=%s method=%s from=%s detail=%s',
                    request.path, request.method, sender, detail,
                )
            _record_signature_result(request, valid, detail)
        except Exception:
            logger.exception('TWILIO_SIG_ERROR path=%s', request.path)
        return view_func(request, *args, **kwargs)
    return wrapper
