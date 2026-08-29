"""Twilio webhook signature verification.

STAGE 1 (current): verify the X-Twilio-Signature header on webhook requests
and LOG failures — nothing is rejected. Grep Railway logs for:

    TWILIO_SIG_FAIL   — signature present but did not validate
    TWILIO_SIG_SKIP   — verification skipped (no auth token configured)
    TWILIO_SIG_ERROR  — the verifier itself blew up

After several days of real traffic with zero TWILIO_SIG_FAIL lines on genuine
Twilio posts, STAGE 2 (a separate change) flips the decorator to return 403
on failure.

The signature is an HMAC-SHA1 of the exact public URL Twilio requested
(scheme, host, path, query string) plus the sorted POST params, keyed with the
account auth token — same algorithm as twilio.request_validator. Railway
terminates TLS at its proxy, so the URL must be rebuilt from the forwarded
proto/host headers, not from what the app server saw on its side of the proxy.
"""
import base64
import hashlib
import hmac
import logging
from functools import wraps

from django.conf import settings

logger = logging.getLogger('twilio_signature')


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
        return False, 'missing X-Twilio-Signature header'
    url = _signature_url(request)
    post_params = dict(request.POST.items()) if request.method == 'POST' else {}
    expected = _expected_signature(url, post_params, auth_token)
    if hmac.compare_digest(expected, provided):
        return True, url
    return False, f'signature mismatch for url={url}'


def twilio_signature_log_only(view_func):
    """Stage 1 decorator: verify and log, never reject (see module docstring)."""
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
        except Exception:
            logger.exception('TWILIO_SIG_ERROR path=%s', request.path)
        return view_func(request, *args, **kwargs)
    return wrapper
