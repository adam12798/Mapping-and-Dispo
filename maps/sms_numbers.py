"""Per-organization outbound SMS From numbers.

The org → number mapping lives here in code (keyed by org slug — deliberately
no DB field, so no migration). Two numbers exist:

  - TWILIO_SMS_FROM_NUMBER (the 978): A2P-registered to VENTANA. Only orgs
    listed in A2P_SMS_ORG_SLUGS send from it — putting another org's traffic
    on it would ride Ventana's carrier registration.
  - TWILIO_PHONE_NUMBER (the 833): what everyone sent from historically.
    Team Sunshine stays here, unchanged, as does any send where the org
    can't be resolved (fail to the old behavior, never to Ventana's number).
"""
from django.conf import settings

from maps.tenancy import get_current_org_id

# Orgs whose outbound SMS uses the A2P-registered 978 number.
A2P_SMS_ORG_SLUGS = {'ventana'}


def sms_from_number(org_id=None):
    """From number for outbound SMS for the given org (default: active org)."""
    from maps.models import Organization  # deferred: keep module import-safe
    if org_id is None:
        org_id = get_current_org_id()
    slug = None
    if org_id is not None:
        slug = (
            Organization.objects.filter(pk=org_id)
            .values_list('slug', flat=True).first()
        )
    if slug in A2P_SMS_ORG_SLUGS and settings.TWILIO_SMS_FROM_NUMBER:
        return settings.TWILIO_SMS_FROM_NUMBER
    return settings.TWILIO_PHONE_NUMBER
