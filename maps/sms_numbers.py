"""Per-organization outbound SMS From numbers.

The org → number mapping lives here in code (keyed by org slug — deliberately
no DB field, so no migration). Two numbers exist:

  - TWILIO_SMS_FROM_NUMBER (the 978): A2P-registered to VENTANA and the only
    number here that DELIVERS. But its INBOUND is routed by its Messaging
    Service to MarketingCanvas's webhook, not to Sutton's /sms/, so a reply
    to anything Sutton sends from it never reaches Sutton.
  - TWILIO_PHONE_NUMBER (the 833): inbound points at Sutton's /sms/, but its
    OUTBOUND messaging is disabled — its toll-free registration was never
    completed, and every send from it on record is Undelivered (Twilio
    console, 2026-09-18). Team Sunshine and any unresolved org send from it.

Neither number both delivers and hears replies. INTERIM (2026-09-18): Ventana
sends from the 978 — texts arrive, and replies land in MarketingCanvas. The
fix is either the 833's toll-free registration or MarketingCanvas relaying
rep-matched 978 inbounds to Sutton; see INTEGRATION.md §6.4.
"""
from django.conf import settings

from maps.tenancy import get_current_org_id

# Orgs whose outbound SMS uses the 978. Interim choice: see the docstring.
A2P_SMS_ORG_SLUGS = {'ventana'}

# Orgs where TextBlast is switched off. Team Sunshine's blasts would go out on
# the carrier-filtered 833 (their traffic must not ride Ventana's A2P
# registration), and replies to a blast are handled at /sms/ on the 833 — which
# is why their two 2026-06-23 blasts produced zero claims. Disabled with their
# owner's agreement on 2026-08-31 rather than left silently broken. To re-enable
# for an org, give it its own A2P-registered number first.
TEXTBLAST_DISABLED_ORG_SLUGS = {'team-sunshine'}


def _org_slug(org_id=None):
    """Slug of the given org, or of the active org when none is passed."""
    from maps.models import Organization  # deferred: keep module import-safe
    if org_id is None:
        org_id = get_current_org_id()
    if org_id is None:
        return None
    return (
        Organization.objects.filter(pk=org_id)
        .values_list('slug', flat=True).first()
    )


def sms_from_number(org_id=None):
    """From number for outbound SMS for the given org (default: active org)."""
    slug = _org_slug(org_id)
    if slug in A2P_SMS_ORG_SLUGS and settings.TWILIO_SMS_FROM_NUMBER:
        return settings.TWILIO_SMS_FROM_NUMBER
    return settings.TWILIO_PHONE_NUMBER


def textblast_enabled(org_id=None):
    """Is TextBlast switched on for this org? (default: active org)"""
    return _org_slug(org_id) not in TEXTBLAST_DISABLED_ORG_SLUGS
