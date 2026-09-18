"""Outbound SMS From numbers (maps/sms_numbers.py).

The 978's inbound is routed to MarketingCanvas, so a text Sutton sends from it
can never be answered into Sutton. Every org sends from the 833, whose inbound
is Sutton's /sms/.
"""
from unittest import mock
from urllib.parse import parse_qs

from django.test import TestCase, override_settings
from django.utils import timezone

from .models import Lead, Organization, Rep
from .sms_numbers import sms_from_number
from .tenancy import org_context

N833 = '+18330000833'
N978 = '+19780000978'


@override_settings(TWILIO_ACCOUNT_SID='ACtest', TWILIO_AUTH_TOKEN='token',
                   TWILIO_PHONE_NUMBER=N833, TWILIO_SMS_FROM_NUMBER=N978)
class SmsFromNumberTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.sunshine = Organization.objects.get(slug='team-sunshine')

    def sent_from(self, send, *args):
        with mock.patch('urllib.request.urlopen') as urlopen:
            urlopen.return_value.read.return_value = b'{}'
            send(*args)
        return parse_qs(urlopen.call_args.args[0].data.decode())['From'][0]

    def test_every_org_sends_from_the_833(self):
        self.assertEqual(sms_from_number(self.ventana.id), N833)
        self.assertEqual(sms_from_number(self.sunshine.id), N833)
        self.assertEqual(sms_from_number(None), N833)  # no org resolvable

    def test_ventana_sends_through_every_sender_use_the_833(self):
        from maps.management.commands.check_dispo_reminders import send_sms as reminder_sms
        from maps.views import send_sms, send_sms_with_result
        with org_context(self.ventana):
            self.assertEqual(self.sent_from(send_sms, '+16175550001', 'hi'), N833)
            self.assertEqual(self.sent_from(send_sms_with_result, '+16175550001', 'hi'), N833)
            self.assertEqual(self.sent_from(reminder_sms, '+16175550001', 'hi'), N833)

    def test_ventana_textblast_goes_out_on_the_833(self):
        # A blast is the text whose replies (the claims) most need to reach /sms/.
        from maps.views import send_textblast
        Rep.all_objects.create(name='V Rep', phone_number='+16175550001', home_address='1 A St',
                               textblast_eligible=True, is_active=True, organization=self.ventana)
        with org_context(self.ventana):
            lead = Lead.objects.create(address='1 Blast St', city='Andover', appointment_type='hvac',
                                       appointment_datetime=timezone.now())
            with mock.patch('urllib.request.urlopen') as urlopen:
                urlopen.return_value.read.return_value = b'{}'
                result = send_textblast([lead])
        self.assertEqual(result['sent'], 1, result)
        self.assertEqual(parse_qs(urlopen.call_args.args[0].data.decode())['From'][0], N833)
