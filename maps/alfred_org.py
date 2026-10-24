"""Which organization a call to Alfred (the voice assistant) belongs to, and
whether Alfred answers it.

Each Organization has `alfred_declines` (off by default). While no active
organization has it on, nothing here runs: every call is answered exactly as
before (voice_ws.get_rep_context resolves the caller as it always has).

Once any active organization has it on, every call is resolved STRICTLY, at
answer time, before Alfred says a word:

  * the caller's phone is looked up among active reps (then, when no rep
    matches, among managers), across all organizations;
  * a phone that belongs to people in more than one organization is declined
    politely and logged — Alfred never guesses which business it is;
  * a phone that matches nobody belongs to the default inbound organization,
    as it always has;
  * the organization found is declined politely, and logged, when it has
    `alfred_declines` on.

The organization fixed here travels with the call (a stream parameter), and
the call session uses it for everything it reads and writes — it is never
re-resolved mid-call. A reminder call (the worker's outbound call about one
lead) belongs to that lead's organization. A declined call reaches neither
the AI nor the database: nothing is read, written or logged in any
organization's tables.
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
        org_ids = {
            r.organization_id for r in Rep.all_objects.filter(is_active=True, organization__isnull=False)
            if clean_phone(r.phone_number) == clean
        }
        if not org_ids:
            org_ids = {
                m.organization_id for m in Manager.all_objects.filter(organization__isnull=False)
                if clean_phone(m.phone_number) == clean
            }
    if len(org_ids) > 1:
        return {'org_id': None, 'decline': 'both_orgs'}
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


def log_decline(kind, number, result):
    """One line per declined call: never the full number, never a name."""
    logger.warning(
        'ALFRED_DECLINED kind=%s caller=%s reason=%s org=%s',
        kind, _masked(number), result.get('decline'), result.get('org_id'),
    )


def decline_text(result):
    return DECLINE_BOTH if result.get('decline') == 'both_orgs' else DECLINE_ORG
