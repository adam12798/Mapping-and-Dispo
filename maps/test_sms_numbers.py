"""Outbound SMS From numbers (maps/sms_numbers.py).

Neither number both delivers and hears replies: the 978 delivers but its
inbound goes to MarketingCanvas; the 833's inbound is Sutton's /sms/ but its
outbound SMS is disabled. Interim (2026-09-18): Ventana sends from the 978,
Team Sunshine and unresolved sends stay on the 833, and Team Sunshine's
traffic never rides Ventana's A2P registration.
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

    def test_numbers_per_org(self):
        self.assertEqual(sms_from_number(self.ventana.id), N978)
        self.assertEqual(sms_from_number(self.sunshine.id), N833)  # never Ventana's registration
        self.assertEqual(sms_from_number(None), N833)  # no org resolvable: never the 978

    def test_every_sender_follows_the_org(self):
        from maps.management.commands.check_dispo_reminders import send_sms as reminder_sms
        from maps.views import send_sms, send_sms_with_result
        for org, expected in ((self.ventana, N978), (self.sunshine, N833)):
            with org_context(org):
                self.assertEqual(self.sent_from(send_sms, '+16175550001', 'hi'), expected)
                self.assertEqual(self.sent_from(send_sms_with_result, '+16175550001', 'hi'), expected)
                self.assertEqual(self.sent_from(reminder_sms, '+16175550001', 'hi'), expected)

    def test_ventana_textblast_goes_out_on_the_978(self):
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
        self.assertEqual(parse_qs(urlopen.call_args.args[0].data.decode())['From'][0], N978)
