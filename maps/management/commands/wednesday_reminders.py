"""Turn L12 on or off for one organization: Wednesday's rep loop reminds its
reps, and Sutton's reminder worker sends them nothing.

    python manage.py wednesday_reminders ventana          # show
    python manage.py wednesday_reminders ventana on       # Sutton's reminders stop for Ventana
    python manage.py wednesday_reminders ventana off      # Sutton's reminders resume

It touches that one column (Organization.reminders_by_wednesday) of that one
organization and nothing else; it prints what it was and what it is now.

Turning it OFF hands reminders back to Sutton, which catches up at its next
run (every 15 minutes): every lead due a reminder today is reminded at once.
So turn Wednesday's rep loop off first, and prefer doing it after 9 PM New
York. "off" prints the ids of the leads Sutton's next run would remind.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError

from maps.models import Organization
from maps.tenancy import org_context


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
        if before and not org.reminders_by_wednesday:
            from maps.management.commands.check_dispo_reminders import Command as Reminders
            with org_context(org.id):
                dispo, followup = Reminders().due_ids(datetime.now(ZoneInfo('America/New_York')))
            self.stdout.write(
                "Sutton's next reminder run will remind — "
                f'dispo: {dispo or "none"}; follow-up: {followup or "none"}'
            )
