"""Which organization a call to Alfred (the voice assistant) belongs to, and
whether Alfred answers it.

Each Organization has `alfred_declines` (off by default). While no active
organization has it on, nothing here runs: every call is answered exactly as
before (voice_ws.get_rep_context resolves the caller as it always has).

Once any active organization has it on, every call is resolved STRICTLY, at
answer time, before Alfred says a word:

  * the caller's phone is looked up among active reps (then, when no rep
    matches, among managers), across all organizations;
  * a phone that belongs to people in more than one organization is that
    organization's when exactly one of them has Alfred enabled (active, not
    declining — the owner's ruling of 2026-10-24); when none or several do,
    the call is declined politely and logged — Alfred never guesses;
  * a phone that matches nobody belongs to the default inbound organization,
    as it always has;
  * the organization found is declined politely, and logged, when it has
    `alfred_declines` on.

The organization fixed here travels with the call (a stream parameter), and
the call session uses it for everything it reads and writes — it is never
re-resolved mid-call. The stream checks it once more when it starts
(session_org): a stream with no orgId, or one whose organization no longer
answers or no longer matches the caller, is refused before any lookup. A
reminder call (the worker's outbound call about one lead) belongs to that
lead's organization. Resolving a call reads only the phone numbers and
organizations of reps and managers (and a reminder lead's organization); a
declined call reaches neither the AI nor any organization's tables — nothing
is written.

While no organization declines, voice_answer makes this one query
(strict_mode) and nothing else changes.
"""
import logging

from .models import Lead, Manager, Organization, Rep

logger = logging.getLogger(__name__)

# What a declined caller hears (Twilio <Say>), then the call ends.
DECLINE_BOTH = (
    "Sorry, this phone number is set up with more than one company here, "
    "so Alfred can't tell which one you're calling about. "
    "Please contact your manager. Goodbye."
)
DECLINE_ORG = (
    "Sorry, Alfred isn't available on this line. "
    "Please contact your manager. Goodbye."
)


def clean_phone(number):
    """voice_ws.clean_phone's normalization, so both read a number alike."""
    return (number or '').replace('+1', '').replace('+', '').replace('-', '') \
        .replace(' ', '').replace('(', '').replace(')', '')


def strict_mode():
    """True once any active organization has asked Alfred to decline it."""
    return Organization.objects.filter(is_active=True, alfred_declines=True).exists()


def _answers(org_id):
    org = Organization.objects.filter(id=org_id, is_active=True).first()
    return org is not None and not org.alfred_declines


def _masked(number):
    digits = clean_phone(number)
    return f'…{digits[-4:]}' if len(digits) >= 4 else '(none)'


def resolve_caller(caller_number):
    """An inbound call: {'org_id', 'decline'}, decline None or a reason
    ('both_orgs', 'org_declines'). Cross-organization by design — the phone
    number is the only identity a call has (Rep/Manager .all_objects)."""
    clean = clean_phone(caller_number)
    org_ids = set()
    if clean:
        # Only the phone number and the organization of each person is read.
        org_ids = {
            org_id for org_id, phone in Rep.all_objects.filter(is_active=True, organization__isnull=False)
            .values_list('organization_id', 'phone_number')
            if clean_phone(phone) == clean
        }
        if not org_ids:
            org_ids = {
                org_id for org_id, phone in Manager.all_objects.filter(organization__isnull=False)
                .values_list('organization_id', 'phone_number')
                if clean_phone(phone) == clean
            }
    if len(org_ids) > 1:
        # More than one organization: the one with Alfred enabled, when it is
        # the only one; none enabled, or several, is declined.
        answering = sorted(o for o in org_ids if _answers(o))
        if len(answering) == 1:
            return {'org_id': answering[0], 'decline': None}
        return {'org_id': None, 'decline': 'org_declines' if not answering else 'both_orgs'}
    org_id = next(iter(org_ids)) if org_ids else Organization.default_inbound_id()
    if org_id is None or not _answers(org_id):
        return {'org_id': org_id, 'decline': 'org_declines'}
    return {'org_id': org_id, 'decline': None}


def resolve_reminder(lead_id):
    """A reminder call about one lead: that lead's organization."""
    try:
        lead = Lead.all_objects.filter(id=int(lead_id)).only('id', 'organization_id').first()
    except (TypeError, ValueError):
        lead = None
    org_id = lead.organization_id if lead else None
    if org_id is None or not _answers(org_id):
        return {'org_id': org_id, 'decline': 'org_declines'}
    return {'org_id': org_id, 'decline': None}


def session_org(caller_number, reminder_lead_id, org_id):
    """The stream's own check when it starts. While no organization declines:
    {'org_id': org_id as given, 'decline': None, 'strict': False} — the call
    runs as it always has. Otherwise the call is resolved again exactly as it
    was answered; it is refused when that declines, or when the stream's
    orgId (from the answer's TwiML) is missing or names another organization —
    a stream opened without our TwiML, or one answered before the switch moved."""
    if not strict_mode():
        return {'org_id': org_id, 'decline': None, 'strict': False}
    result = resolve_reminder(reminder_lead_id) if reminder_lead_id else resolve_caller(caller_number)
    if not result['decline'] and result['org_id'] != org_id:
        result = {'org_id': result['org_id'], 'decline': 'org_unconfirmed'}
    return {**result, 'strict': True}


def log_decline(kind, number, result):
    """One line per declined call: never the full number, never a name."""
    logger.warning(
        'ALFRED_DECLINED kind=%s caller=%s reason=%s org=%s',
        kind, _masked(number), result.get('decline'), result.get('org_id'),
    )


def decline_text(result):
    return DECLINE_BOTH if result.get('decline') == 'both_orgs' else DECLINE_ORG
