"""Show or set slot claims (maps/availability.py) for Ventana.

    python manage.py slot_claims ventana                   # show
    python manage.py slot_claims ventana on                # the availability endpoints answer; v1 lead creates are checked
    python manage.py slot_claims ventana off               # endpoints 403 again; v1 lead creates as before
    python manage.py slot_claims ventana --ttl-hours 24    # how long an unconsumed claim holds its spot

It touches those two columns (Organization.slot_claims_enabled,
slot_claim_ttl_hours) of Ventana's row and nothing else, and prints what they
were and what they are now. Any other organization is refused: slot claims
are Ventana's alone (views.SLOT_CLAIMS_ORG_SLUG), and no other organization's
row is ever written. Turning it off keeps every claim; they stop mattering.
"""
from django.core.management.base import BaseCommand, CommandError

from maps.models import Organization
from maps.views import SLOT_CLAIMS_ORG_SLUG


class Command(BaseCommand):
    help = 'Show or set slot claims (Lurch availability) for Ventana'

    def add_arguments(self, parser):
        parser.add_argument('slug')
        parser.add_argument('state', nargs='?', choices=['on', 'off'])
        parser.add_argument('--ttl-hours', type=int)

    def handle(self, *args, **options):
        if options['slug'] != SLOT_CLAIMS_ORG_SLUG:
            raise CommandError(f'slot claims are only for {SLOT_CLAIMS_ORG_SLUG!r}; nothing was changed')
        ttl = options['ttl_hours']
        if ttl is not None and not 1 <= ttl <= 24 * 14:
            raise CommandError('--ttl-hours must be from 1 to 336; nothing was changed')
        org = Organization.objects.filter(slug=options['slug']).first()
        if org is None:
            raise CommandError(f"no organization with slug {options['slug']!r}")
        before = (org.slot_claims_enabled, org.slot_claim_ttl_hours)
        if options['state'] is not None:
            org.slot_claims_enabled = options['state'] == 'on'
        if ttl is not None:
            org.slot_claim_ttl_hours = ttl
        if (org.slot_claims_enabled, org.slot_claim_ttl_hours) != before:
            org.save(update_fields=['slot_claims_enabled', 'slot_claim_ttl_hours'])

        def show(enabled, hours):
            return f"{'on' if enabled else 'off'}, claims hold {hours} h"

        self.stdout.write(f'{org.name}: slot claims were {show(*before)}, '
                          f'now {show(org.slot_claims_enabled, org.slot_claim_ttl_hours)}')
