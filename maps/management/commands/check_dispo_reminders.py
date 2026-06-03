"""
Management command that checks for un-dispositioned appointments and sends reminders.

Schedule: runs every 15 minutes via worker process.

Flow:
  - 3 hours after appointment: SMS reminder to rep
  - 4 hours after appointment (1 hr after SMS): outbound call via Alfred
  - Skips reps who are currently in another appointment (window: -1hr to +2hr)
"""
import base64
import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand

from maps.models import Lead

logger = logging.getLogger(__name__)
EASTERN = ZoneInfo('America/New_York')


def send_sms(to, body):
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return
    url = f'https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json'
    data = urllib.parse.urlencode({
        'To': to,
        'From': settings.TWILIO_PHONE_NUMBER,
        'Body': body,
    }).encode()
    req = urllib.request.Request(url, data=data)
    credentials = f'{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}'
    auth = base64.b64encode(credentials.encode()).decode()
    req.add_header('Authorization', f'Basic {auth}')
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.error(f'SMS send failed to {to}: {e}')


def make_outbound_call(to, lead_id):
    """Initiate an outbound Twilio call that connects to Alfred via WebSocket."""
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return
    # Railway app host — voice_reminder_call endpoint returns TwiML
    app_host = 'lavish-reflection-production-1e5f.up.railway.app'
    callback_url = f'https://{app_host}/voice/reminder-call/?lead_id={lead_id}'

    url = f'https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Calls.json'
    data = urllib.parse.urlencode({
        'To': to,
        'From': settings.TWILIO_PHONE_NUMBER,
        'Url': callback_url,
    }).encode()
    req = urllib.request.Request(url, data=data)
    credentials = f'{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}'
    auth = base64.b64encode(credentials.encode()).decode()
    req.add_header('Authorization', f'Basic {auth}')
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        call_data = json.loads(resp.read())
        logger.info(f'Outbound call initiated to {to}: SID={call_data.get("sid")}')
    except Exception as e:
        logger.error(f'Outbound call failed to {to}: {e}')


class Command(BaseCommand):
    help = 'Check for un-dispositioned appointments and send SMS/call reminders'

    def handle(self, *args, **options):
        now = datetime.now(EASTERN)
        three_hours_ago = now - timedelta(hours=3)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Find leads needing reminders: past appointment, no dispo, has rep with phone
        overdue_leads = Lead.objects.filter(
            appointment_datetime__isnull=False,
            appointment_datetime__gte=today_start,
            appointment_datetime__lte=three_hours_ago,
            disposition='',
            cancelled=False,
            rep__isnull=False,
            rep__is_active=True,
            rep__phone_number__gt='',
        ).select_related('rep')

        if not overdue_leads.exists():
            self.stdout.write('No overdue leads found.')
            return

        self.stdout.write(f'Found {overdue_leads.count()} overdue lead(s)')

        for lead in overdue_leads:
            rep = lead.rep
            appt_time = lead.appointment_datetime.astimezone(EASTERN)

            # Check if rep is currently in another appointment
            has_future_appt = Lead.objects.filter(
                rep=rep,
                cancelled=False,
                appointment_datetime__gt=now - timedelta(hours=1),
                appointment_datetime__lte=now + timedelta(hours=2),
            ).exclude(id=lead.id).exists()

            if has_future_appt:
                self.stdout.write(
                    f'  Skipping {lead.homeowner_name} — {rep.name} is in/near another appointment'
                )
                continue

            # Phase 1: SMS reminder (3+ hours, no SMS sent yet)
            if not lead.dispo_reminder_sent_at:
                name = lead.homeowner_name or 'your appointment'
                time_str = appt_time.strftime('%I:%M %p').lstrip('0')
                body = (
                    f"Hey {rep.name.split()[0]}, you haven't updated your "
                    f"{time_str} appointment with {name} yet. "
                    f"Call Alfred at {settings.TWILIO_PHONE_NUMBER} to update it!"
                )
                self.stdout.write(f'  SMS → {rep.name} re: {lead.homeowner_name}')
                send_sms(rep.phone_number, body)
                lead.dispo_reminder_sent_at = now
                lead.save(update_fields=['dispo_reminder_sent_at'])
                continue

            # Phase 2: Outbound call (1+ hour after SMS, no call yet)
            sms_age = now - lead.dispo_reminder_sent_at
            if sms_age >= timedelta(hours=1) and not lead.dispo_call_made_at:
                self.stdout.write(f'  CALL → {rep.name} re: {lead.homeowner_name}')
                make_outbound_call(rep.phone_number, lead.id)
                lead.dispo_call_made_at = now
                lead.save(update_fields=['dispo_call_made_at'])

        self.stdout.write('Done.')
