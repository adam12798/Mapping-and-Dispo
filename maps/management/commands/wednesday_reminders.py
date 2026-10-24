"""Turn L12 on or off for one organization: Wednesday's rep loop reminds its
reps, and Sutton's reminder worker sends them nothing.

    python manage.py wednesday_reminders ventana          # show
    python manage.py wednesday_reminders ventana on       # Sutton's reminders stop for Ventana
    python manage.py wednesday_reminders ventana off      # Sutton's reminders resume

It touches that one column (Organization.reminders_by_wednesday) of that one
organization and nothing else; it prints what it was and what it is now.
"""
from django.core.management.base import BaseCommand, CommandError

from maps.models import Organization


class Command(BaseCommand):
    help = "Show or set whether Wednesday's rep loop (not Sutton) reminds an organization's reps"

    def add_arguments(self, parser):
        parser.add_argument('slug')
        parser.add_argument('state', nargs='?', choices=['on', 'off'])

    def handle(self, *args, **options):
        org = Organization.objects.filter(slug=options['slug']).first()
        if org is None:
            raise CommandError(f"no organization with slug {options['slug']!r}")
        before = org.reminders_by_wednesday
        if options['state'] is not None:
            org.reminders_by_wednesday = options['state'] == 'on'
            if org.reminders_by_wednesday != before:
                org.save(update_fields=['reminders_by_wednesday'])
        self.stdout.write(
            f"{org.name}: reminders_by_wednesday was {'on' if before else 'off'}, "
            f"now {'on' if org.reminders_by_wednesday else 'off'}"
        )
