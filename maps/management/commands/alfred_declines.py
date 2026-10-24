"""Turn Alfred's decline on or off for one organization (U2).

    python manage.py alfred_declines team-sunshine          # show
    python manage.py alfred_declines team-sunshine on       # Alfred declines its callers
    python manage.py alfred_declines team-sunshine off

The same switch as the Django admin's (Organization.alfred_declines). It
touches that one column of that one organization and nothing else; it prints
what it was and what it is now.
"""
from django.core.management.base import BaseCommand, CommandError

from maps.models import Organization


class Command(BaseCommand):
    help = "Show or set whether Alfred declines an organization's callers"

    def add_arguments(self, parser):
        parser.add_argument('slug')
        parser.add_argument('state', nargs='?', choices=['on', 'off'])

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options['slug']).first()
        if org is None:
            raise CommandError(f"no organization with slug {options['slug']!r}")
        before = org.alfred_declines
        if options['state'] is not None:
            org.alfred_declines = options['state'] == 'on'
            if org.alfred_declines != before:
                org.save(update_fields=['alfred_declines'])
        self.stdout.write(
            f"{org.name}: alfred_declines was {'on' if before else 'off'}, "
            f"now {'on' if org.alfred_declines else 'off'}"
        )
