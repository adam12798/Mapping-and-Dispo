"""Report Twilio signature-verification results from the database.

This REPLACES the daily log grep:

    railway logs --service Sutton | grep -E "TWILIO_SIG_(FAIL|SKIP|ERROR)"

which cannot be trusted — Railway's application-log stream stopped ingesting
for 9+ hours on 2026-08-31 while the app ran normally, and an empty grep
cannot distinguish "no failures" from "no traffic". Stage 1 now records every
verification attempt as a GHLWebhookLog row, so the gate is a DB query.

Usage:
    python manage.py signature_watch              # last 7 days
    python manage.py signature_watch --days 1     # since yesterday
    python manage.py signature_watch --verbose    # list every row

Exit codes (so it can gate automation):
    0 = PASS            genuine signed traffic seen, zero mismatches
    1 = FAIL            at least one signature mismatch — do NOT ship stage 2
    2 = INSUFFICIENT    no genuine signed traffic yet — keep waiting
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from maps.models import GHLWebhookLog
from maps.twilio_security import FAIL_MISMATCH, FAIL_UNSIGNED, SIGNATURE_LOG_SOURCE


class Command(BaseCommand):
    help = 'Report Twilio signature verification results (replaces the daily log grep).'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7,
                            help='How many days back to report on (default 7).')
        parser.add_argument('--verbose', action='store_true',
                            help='List every recorded verification, not just the summary.')

    def handle(self, *args, **options):
        days = options['days']
        since = timezone.now() - timedelta(days=days)
        rows = GHLWebhookLog.objects.filter(
            source=SIGNATURE_LOG_SOURCE, created_at__gte=since,
        ).order_by('created_at')

        verified, unsigned, mismatched, skipped = [], [], [], []
        for row in rows:
            msg = row.error_message or ''
            if row.success:
                verified.append(row)
            elif FAIL_UNSIGNED in msg:
                unsigned.append(row)
            elif FAIL_MISMATCH in msg:
                mismatched.append(row)
            else:
                skipped.append(row)

        self.stdout.write(f'Twilio signature verifications, last {days} day(s):')
        self.stdout.write(f'  total recorded          : {rows.count()}')
        self.stdout.write(f'  VERIFIED (genuine, ok)  : {len(verified)}')
        self.stdout.write(f'  MISMATCH (must be zero) : {len(mismatched)}')
        self.stdout.write(f'  unsigned (scanner junk) : {len(unsigned)}')
        self.stdout.write(f'  skipped (no auth token) : {len(skipped)}')

        if options['verbose']:
            self.stdout.write('')
            for row in rows:
                state = 'VERIFIED' if row.success else 'FAILED  '
                self.stdout.write(
                    f'  {row.created_at:%Y-%m-%d %H:%M:%S} {state} {row.url[:70]}'
                )
                if row.error_message:
                    self.stdout.write(f'      {row.error_message[:120]}')

        if mismatched:
            self.stdout.write('')
            self.stdout.write('MISMATCHES — a genuinely signed request failed to validate:')
            for row in mismatched[:10]:
                self.stdout.write(f'  {row.created_at:%Y-%m-%d %H:%M:%S}  {row.error_message[:160]}')
                self.stdout.write(f'      payload: {row.payload[:200]}')

        self.stdout.write('')
        if mismatched:
            self.stdout.write(
                'VERDICT: FAIL — do NOT enable 403 enforcement. A signed request did not '
                'validate; compare the signed_url above with the URL configured in the '
                'Twilio console (proxy proto/host mismatch is the usual cause).'
            )
            return_code = 1
        elif not verified:
            self.stdout.write(
                'VERDICT: INSUFFICIENT — no genuine signed traffic recorded yet. An empty '
                'result is not a pass. Keep waiting, or send one test SMS to the 833.'
            )
            return_code = 2
        else:
            self.stdout.write(
                f'VERDICT: PASS — {len(verified)} genuine signed request(s) verified, zero '
                'mismatches. Unsigned scanner hits are expected and are exactly what stage 2 '
                'will reject. Safe to ship 403 enforcement.'
            )
            return_code = 0

        self.stdout.write(f'(exit {return_code})')
        raise SystemExit(return_code)
