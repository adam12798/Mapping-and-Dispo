"""Assign any rows that still have organization=NULL to an organization.

Needed once after the tenancy deploy: while the new container runs
migrations, the old app code may still serve a few requests and insert
rows without an organization. Those rows are invisible to everyone
(fail-closed) until adopted. Safe to re-run any time; it only touches
NULL-org rows.

Usage:
    python manage.py backfill_org_orphans            # dry run, prints counts
    python manage.py backfill_org_orphans --apply    # adopt into default inbound org
    python manage.py backfill_org_orphans --apply --org ventana
"""
from django.core.management.base import BaseCommand, CommandError

from maps.models import (
    APITenant, GHLWebhookLog, Lead, LeadMessage, LeadUpdate, Manager,
    Organization, Rep, RepCountDefault, RepCountOverride, TimeOffRequest,
    UserProfile, VoiceCallLog, WebhookConfig,
)

OWNED_MODELS = [
    Lead, Rep, Manager, TimeOffRequest, VoiceCallLog, LeadMessage,
    LeadUpdate, RepCountDefault, RepCountOverride, UserProfile,
    APITenant, WebhookConfig, GHLWebhookLog,
]


class Command(BaseCommand):
    help = 'Adopt organization-less rows into an organization (default: dry run)'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Actually write; otherwise dry run')
        parser.add_argument('--org', help='Target org slug (default: the default-inbound org)')

    def handle(self, *args, **options):
        if options['org']:
            org = Organization.objects.filter(slug=options['org']).first()
            if org is None:
                raise CommandError(f"No organization with slug '{options['org']}'")
            org_id = org.id
        else:
            org_id = Organization.default_inbound_id()
            if org_id is None:
                raise CommandError('No default inbound organization configured')
        org = Organization.objects.get(id=org_id)

        total = 0
        for model in OWNED_MODELS:
            manager = getattr(model, 'all_objects', model.objects)
            qs = manager.filter(organization__isnull=True)
            count = qs.count()
            total += count
            if count:
                if options['apply']:
                    qs.update(organization_id=org_id)
                    self.stdout.write(f'{model.__name__}: adopted {count} row(s) into {org.name}')
                else:
                    self.stdout.write(f'{model.__name__}: {count} orphan row(s)')
        if total == 0:
            self.stdout.write('No orphan rows.')
        elif not options['apply']:
            self.stdout.write(f'Dry run — re-run with --apply to adopt {total} row(s) into {org.name}.')
