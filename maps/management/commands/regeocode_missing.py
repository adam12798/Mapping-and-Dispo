"""Give coordinates back to leads that lost them while geocoding was down.

From about 2026-10-02 geocoding failed for every lead created in either org —
most likely Nominatim refusing the production container — and those leads came
back with no latitude/longitude, off the map and the daily view. This geocodes
them again, the same way creation does (address, plus ", city, MA" when there
is a city), and writes the two coordinates and NOTHING ELSE: a QuerySet
.update() of latitude/longitude, only while they are still empty and the
address is still the one geocoded, so it fires no signals, no webhooks and no
chatter. (The updated_at trigger, migration 0042, still stamps the row:
coordinates are not on its ignored list, which is how the hub learns them.)

Dry run unless --apply. Both orgs. Reports, per org, what was re-geocoded, what
still fails, and what someone changed while it ran (left alone), by lead id.

It runs in its own process, so it does not share the web worker's one request
a second: run it at a quiet time.

Usage:
    python manage.py regeocode_missing --since 2026-10-01            # dry run
    python manage.py regeocode_missing --since 2026-10-01 --apply
"""
from collections import defaultdict
from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from maps.models import Lead
from maps.views import geocode


class Command(BaseCommand):
    help = 'Re-geocode leads created since a date that have no coordinates (default: dry run)'

    def add_arguments(self, parser):
        parser.add_argument('--since', required=True, help='YYYY-MM-DD: leads created on or after this day (Eastern)')
        parser.add_argument('--apply', action='store_true', help='Write the coordinates; otherwise only report')

    def handle(self, *args, **options):
        try:
            day = datetime.strptime(options['since'], '%Y-%m-%d').date()
        except ValueError:
            raise CommandError('--since must be YYYY-MM-DD')
        since = timezone.make_aware(datetime.combine(day, time.min))
        apply = options['apply']

        leads = (Lead.all_objects.select_related('organization')
                 .filter(created_at__gte=since, latitude__isnull=True)
                 .exclude(address='').order_by('id'))
        found, failed, left = defaultdict(list), defaultdict(list), defaultdict(list)
        for lead in leads:
            org = lead.organization.name if lead.organization_id else '(no org)'
            query = f'{lead.address}, {lead.city}, MA' if lead.city else lead.address
            try:
                lat, lng = geocode(query)
            except Exception as e:
                self.stderr.write(f'lead {lead.id} ({org}): {type(e).__name__}: {e}')
                failed[org].append(lead.id)
                continue
            if lat is None:
                failed[org].append(lead.id)
                continue
            # Only while still empty and still the address geocoded: a person
            # who fixed the lead in the meantime keeps what their edit produced.
            if apply and not (Lead.all_objects
                              .filter(pk=lead.pk, latitude__isnull=True, address=lead.address, city=lead.city)
                              .update(latitude=lat, longitude=lng)):
                left[org].append(lead.id)
                self.stdout.write(f'left lead {lead.id} ({org}) alone: changed while this ran')
                continue
            found[org].append(lead.id)
            self.stdout.write(f'{"set" if apply else "would set"} lead {lead.id} ({org}): {lat}, {lng}')

        self.stdout.write(f'\n{"APPLIED" if apply else "DRY RUN"} — leads created since {day} with no coordinates:')
        for org in sorted(set(found) | set(failed) | set(left)):
            line = f'  {org}: re-geocoded {len(found[org])} {found[org]}; still failing {len(failed[org])} {failed[org]}'
            if left[org]:
                line += f'; changed meanwhile, left alone {len(left[org])} {left[org]}'
            self.stdout.write(line)
        if not found and not failed and not left:
            self.stdout.write('  none')
