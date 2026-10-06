"""Give coordinates back to leads that lost them while geocoding was down.

From about 2026-10-02 Nominatim refused the production container, and every
lead created in either org came back with no latitude/longitude — off the map
and the daily view. This geocodes those leads again, the same way creation does
(address, plus ", city, MA" when there is a city), and writes the two
coordinates and NOTHING ELSE: a QuerySet .update() of latitude/longitude, only
while they are still empty, so it fires no signals, no webhooks and no chatter.
(The updated_at trigger, migration 0042, still stamps the row: coordinates are
not on its ignored list, which is how the hub learns them.)

Dry run unless --apply. Both orgs. Reports, per org, what was re-geocoded and
what still fails, by lead id.

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
        found, failed = defaultdict(list), defaultdict(list)
        for lead in leads:
            org = lead.organization.name if lead.organization_id else '(no org)'
            query = f'{lead.address}, {lead.city}, MA' if lead.city else lead.address
            lat, lng = geocode(query)
            if lat is None:
                failed[org].append(lead.id)
                continue
            if apply:
                # Only while still empty: a person who fixed the address in the
                # meantime keeps what their edit produced.
                Lead.all_objects.filter(pk=lead.pk, latitude__isnull=True).update(latitude=lat, longitude=lng)
            found[org].append(lead.id)
            self.stdout.write(f'{"set" if apply else "would set"} lead {lead.id} ({org}): {lat}, {lng}')

        self.stdout.write(f'\n{"APPLIED" if apply else "DRY RUN"} — leads created since {day} with no coordinates:')
        for org in sorted(set(found) | set(failed)):
            self.stdout.write(f'  {org}: re-geocoded {len(found[org])} {found[org]}; still failing {len(failed[org])} {failed[org]}')
        if not found and not failed:
            self.stdout.write('  none')
