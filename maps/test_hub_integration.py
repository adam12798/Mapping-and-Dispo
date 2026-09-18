"""Hub integration (INTEGRATION.md): the updated_at change feed, hub_customer_id,
the booking events — and proof that Team Sunshine's live webhooks are
byte-for-byte what they were before any of it.

The trigger tests need PostgreSQL (production's engine); on SQLite they skip.
Run the suite against both:
    python3 manage.py test maps
    DATABASE_URL=postgres://… python3 manage.py test maps
"""
import datetime as dt
import json
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client, TestCase
from django.utils import timezone

from .models import APITenant, GHLWebhookLog, Lead, Organization, Rep, UserProfile, WebhookConfig
from .tenancy import org_context
from .views import _do_fire_webhooks

EASTERN = ZoneInfo('America/New_York')

requires_trigger = unittest.skipUnless(
    connection.vendor == 'postgresql', 'the updated_at trigger exists only on PostgreSQL')

# Team Sunshine's two live configs, fields exactly as in production (read
# 2026-09-18). They differ from what 0038 seeded.
TS_APPT_FIELDS = ['phone_number', 'appointment_type', 'appointment_datetime']
TS_DISPO_FIELDS = [
    'phone_number', 'disposition', 'call_transcript', 'call_notes', 'sat',
    'follow_up_date', 'follow_up_time', 'appt_notes', 'post_appt_notes',
    'monthly_cost', 'total_cost', 'adders',
]


class HubBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.sunshine = Organization.objects.get(slug='team-sunshine')

        cls.v_manager = User.objects.create_user('v_manager', password='pw')
        UserProfile.all_objects.create(user=cls.v_manager, role='manager', organization=cls.ventana)

        cls.v_rep = Rep.all_objects.create(
            name='V Rep', phone_number='6175550001', home_address='1 A St', organization=cls.ventana)
        cls.s_rep = Rep.all_objects.create(
            name='S Rep', phone_number='6175550002', home_address='2 B St', organization=cls.sunshine)

        cls.v_key = APITenant.objects.create(name='Hub', organization=cls.ventana).api_key
        cls.s_key = APITenant.objects.create(name='GHL', organization=cls.sunshine).api_key

        cls.v_leads = [
            Lead.all_objects.create(
                address=f'{n} Ventana Way', homeowner_name=f'Vera {n}', phone_number=f'617555100{n}',
                rep=cls.v_rep, organization=cls.ventana)
            for n in range(3)
        ]
        cls.s_lead = Lead.all_objects.create(
            address='20 Sunshine Rd', homeowner_name='Sam Sunshine', phone_number='6175552222',
            rep=cls.s_rep, organization=cls.sunshine)

    def api(self, method, path, key, body=None):
        client = Client()
        kwargs = {'HTTP_AUTHORIZATION': f'Bearer {key}'}
        if body is not None:
            kwargs.update(data=json.dumps(body), content_type='application/json')
        return getattr(client, method)(path, **kwargs)

    def manager_post(self, path, body):
        client = Client()
        client.force_login(self.v_manager)
        return client.post(path, data=json.dumps(body), content_type='application/json')

    @staticmethod
    def stamp(lead):
        return Lead.all_objects.values_list('updated_at', flat=True).get(pk=lead.pk)


@requires_trigger
@mock.patch('maps.views.fire_webhooks')
class UpdatedAtTriggerTests(HubBase):
    """updated_at must move on every write path that changes what a hub
    mirrors — including the ones auto_now cannot see."""

    def assertBumped(self, lead, before):
        self.assertGreater(self.stamp(lead), before)

    def assertNotBumped(self, lead, before):
        self.assertEqual(self.stamp(lead), before)

    def test_insert_sets_it(self, _fire):
        lead = Lead.all_objects.create(address='1 New St', organization=self.ventana)
        self.assertIsNotNone(self.stamp(lead))

    def test_insert_that_omits_the_column_still_works(self, _fire):
        # What pre-migration code sends if the deploy is rolled back without
        # reversing 0042: an INSERT that names none of the new columns.
        cols = [f.column for f in Lead._meta.concrete_fields
                if f.column not in ('id', 'updated_at', 'hub_customer_id')]
        col_sql = ', '.join(f'"{c}"' for c in cols)
        with connection.cursor() as cur:
            cur.execute(
                f'INSERT INTO maps_lead ({col_sql}) SELECT {col_sql} FROM maps_lead WHERE id = %s RETURNING id',
                [self.s_lead.id])
            new_id = cur.fetchone()[0]
        self.assertIsNotNone(Lead.all_objects.get(pk=new_id).updated_at)

    def test_save_with_a_change_bumps(self, _fire):
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        lead.homeowner_name = 'Vera Renamed'
        lead.save()
        self.assertBumped(lead, before)

    def test_save_with_no_change_does_not_bump(self, _fire):
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        Lead.all_objects.get(pk=lead.pk).save()
        self.assertNotBumped(lead, before)

    def test_alfreds_queryset_update_bumps(self, _fire):
        # voice_ws.py update_disposition: Lead.objects.filter(id=…, rep=…).update(**kwargs)
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        with org_context(self.ventana):
            updated = Lead.objects.filter(id=lead.id, rep=self.v_rep).update(
                disposition='sale', call_notes='Signed', call_transcript='…')
        self.assertEqual(updated, 1)
        self.assertBumped(lead, before)

    def test_bulk_edit_bumps_every_lead(self, _fire):
        before = {lead.id: self.stamp(lead) for lead in self.v_leads}
        resp = self.manager_post('/api/leads/bulk-update/', {
            'ids': [lead.id for lead in self.v_leads], 'fields': {'disposition': 'no_sale'}})
        self.assertEqual(resp.status_code, 200)
        for lead in self.v_leads:
            self.assertBumped(lead, before[lead.id])

    def test_bulk_edit_that_changes_nothing_does_not_bump(self, _fire):
        Lead.all_objects.filter(id__in=[lead.id for lead in self.v_leads]).update(sat=True)
        before = {lead.id: self.stamp(lead) for lead in self.v_leads}
        self.manager_post('/api/leads/bulk-update/', {
            'ids': [lead.id for lead in self.v_leads], 'fields': {'sat': 'true'}})
        for lead in self.v_leads:
            self.assertNotBumped(lead, before[lead.id])

    def test_route_confirm_and_clear_bump(self, _fire):
        lead = self.v_leads[0]
        Lead.all_objects.filter(pk=lead.pk).update(
            rep=None, appointment_datetime=dt.datetime(2026, 9, 21, 10, tzinfo=EASTERN))
        before = self.stamp(lead)
        resp = self.manager_post('/api/confirm-assignments/', {'assignments': {str(lead.id): self.v_rep.id}})
        self.assertEqual(resp.status_code, 200)
        self.assertBumped(lead, before)

        before = self.stamp(lead)
        resp = self.manager_post('/api/clear-assignments/', {'date': '2026-09-21'})
        self.assertEqual(resp.json()['cleared'], 1)
        self.assertBumped(lead, before)

    def test_update_fields_saves_bump(self, _fire):
        # ghl_disposition and ghl_cancel save with update_fields, which
        # leaves auto_now columns out of the UPDATE entirely.
        lead, before = self.s_lead, self.stamp(self.s_lead)
        resp = self.api('post', '/api/v1/ghl/disposition/', self.s_key, {
            'customData': {'Name': 'Sam Sunshine', 'Phone': '6175552222', 'Disposition': 'Sale'}})
        self.assertEqual(resp.status_code, 200)
        self.assertBumped(lead, before)

        before = self.stamp(lead)
        resp = self.api('post', '/api/v1/ghl/cancel/', self.s_key, {
            'customData': {'Name': 'Sam Sunshine', 'Phone': '6175552222'}})
        self.assertEqual(resp.status_code, 200)
        self.assertBumped(lead, before)

    def test_reminder_and_textblast_stamps_do_not_bump(self, _fire):
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        Lead.all_objects.filter(pk=lead.pk).update(textblast_sent_at=timezone.now())
        row = Lead.all_objects.get(pk=lead.pk)
        row.dispo_reminder_sent_at = row.dispo_call_made_at = timezone.now()
        row.follow_up_reminder_sent_at = timezone.now()
        row.save(update_fields=['dispo_reminder_sent_at', 'dispo_call_made_at', 'follow_up_reminder_sent_at'])
        self.assertNotBumped(lead, before)

    def test_textblast_claim_bumps(self, _fire):
        # The claim clears the stamp AND assigns a rep; the rep is what counts.
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        row = Lead.all_objects.get(pk=lead.pk)
        row.rep, row.textblast_sent_at = None, None
        row.save(update_fields=['rep', 'textblast_sent_at'])
        self.assertBumped(lead, before)

    def test_raw_sql_bumps(self, _fire):
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        with connection.cursor() as cur:
            cur.execute("UPDATE maps_lead SET call_notes = 'from psql' WHERE id = %s", [lead.id])
        self.assertBumped(lead, before)

    def test_cannot_be_backdated(self, _fire):
        lead, before = self.v_leads[0], self.stamp(self.v_leads[0])
        Lead.all_objects.filter(pk=lead.pk).update(updated_at=dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc))
        self.assertNotBumped(lead, before)


def ghl_body(**custom):
    base = {'Name': 'Nina New', 'Phone': '(978) 555-0101', 'Address': '5 Elm St', 'City': 'Lowell',
            'Day and Time': '2026-09-22 10:00 AM', 'Product Type': 'Hvac', 'Meeting Type': 'In Person'}
    base.update(custom)
    return {'customData': base}


@mock.patch('maps.views.send_sms')
@mock.patch('maps.views.geocode', return_value=(42.6, -71.3))
@mock.patch('maps.views.fire_webhooks')
class BookingEventTests(HubBase):
    """Which events the inbound booking paths now emit."""

    def triggers(self, fire):
        return [c.args[0] for c in fire.call_args_list]

    def test_ghl_new_booking_fires_lead_created(self, fire, *_):
        resp = self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body())
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(self.triggers(fire), ['lead_created'])
        self.assertEqual(fire.call_args.args[1].id, resp.json()['id'])

    def test_ghl_booking_that_moves_an_existing_lead_fires_rescheduled(self, fire, *_):
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body())
        fire.reset_mock()
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body(**{'Day and Time': '2026-09-23 3:00 PM'}))
        self.assertEqual(self.triggers(fire), ['appt_rescheduled'])

    def test_ghl_resend_of_the_same_booking_fires_nothing(self, fire, *_):
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body())
        fire.reset_mock()
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body())
        self.assertEqual(self.triggers(fire), [])

    def test_ghl_cancel_and_reconfirm(self, fire, *_):
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body())
        fire.reset_mock()
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body(Status='cancelled'))
        self.assertEqual(self.triggers(fire), [])
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body(Status='confirmed'))
        self.assertEqual(self.triggers(fire), ['appt_rescheduled'])

    def test_ghl_reschedule_fires_rescheduled_only_when_something_moved(self, fire, *_):
        self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body())
        fire.reset_mock()
        moved = ghl_body(**{'Day and Time': '2026-09-24 9:00 AM'})
        self.assertEqual(self.api('post', '/api/v1/ghl/reschedule/', self.v_key, moved).status_code, 200)
        self.assertEqual(self.triggers(fire), ['appt_rescheduled'])
        fire.reset_mock()
        self.api('post', '/api/v1/ghl/reschedule/', self.v_key, moved)
        self.assertEqual(self.triggers(fire), [])

    def test_no_ghl_path_fires_appointment_changed(self, fire, *_):
        # appointment_changed is the trigger Team Sunshine's live GHL config listens on.
        self.api('post', '/api/v1/ghl/appointment/', self.s_key, ghl_body())
        self.api('post', '/api/v1/ghl/appointment/', self.s_key, ghl_body(**{'Day and Time': '2026-09-25 1:00 PM'}))
        self.api('post', '/api/v1/ghl/reschedule/', self.s_key, ghl_body(**{'Day and Time': '2026-09-26 1:00 PM'}))
        self.assertNotIn('appointment_changed', self.triggers(fire))
        self.assertEqual(self.triggers(fire), ['lead_created', 'appt_rescheduled', 'appt_rescheduled'])

    def test_setter_sms_new_lead_fires_lead_created(self, fire, *_):
        Client().post('/sms/', {'From': '+19995550000',
                                'Body': 'Name: Sally Setter\nPhone: 6175559999\nAddress: 7 Oak St'})
        lead = Lead.all_objects.get(homeowner_name='Sally Setter')
        self.assertEqual([(c.args[0], c.args[1].id) for c in fire.call_args_list], [('lead_created', lead.id)])

    def test_ghl_format_sms_new_appointment_fires_lead_created(self, fire, *_):
        Client().post('/sms/', {'From': '+19995550000', 'Body': (
            'NEW APPOINTMENT\nName: Greg Ghl\nPhone: 6175558888\nAddress: 8 Pine St\n'
            'Day and Time: 2026-09-22 11:00 AM')})
        lead = Lead.all_objects.get(homeowner_name='Greg Ghl')
        self.assertEqual([(c.args[0], c.args[1].id) for c in fire.call_args_list], [('lead_created', lead.id)])

    def test_unmatched_sms_cancellation_is_not_a_new_lead_event(self, fire, *_):
        Client().post('/sms/', {'From': '+19995550000',
                                'Body': 'APPOINTMENT CANCELLED\nName: Nobody Known\nPhone: 6175557777'})
        self.assertTrue(Lead.all_objects.filter(homeowner_name='Nobody Known', cancelled=True).exists())
        self.assertEqual(self.triggers(fire), [])


class _ImmediateTimers:
    """Stand-in for threading.Timer that records the call instead of waiting
    60 s. Run the recorded calls after the request (fire_webhooks holds its
    lock while starting a timer, so running inline would deadlock)."""

    def __init__(self):
        self.pending = []

    def __call__(self, _delay, fn, args=()):
        timers = self

        class _Timer:
            daemon = False

            def start(self):
                timers.pending.append((fn, args))

            def cancel(self):
                timers.pending.remove((fn, args))
        return _Timer()

    def run(self):
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)


class TeamSunshinePayloadTests(HubBase):
    """Engine changes must be additive: Team Sunshine's two live configs
    produce exactly the bytes they produced before this branch."""

    # Captured from the engine at 0e743b6 (before this branch) for the same
    # fixture leads. Do not regenerate these from new code.
    GOLDEN = {
        ('full', 'appointment_changed'):
            b'{"phone_number": "(781) 555-0142", "appointment_type": "hvac", "appointment_datetime": "09-21-2026 02:30 PM"}',
        ('full', 'disposition_changed'):
            b'{"phone_number": "(781) 555-0142", "disposition": "Needs_Reschedule", "call_transcript": '
            b'"Rep: sat with them\\nAlfred: noted \\u2014 \\"quotes\\" & caf\\u00e9", "call_notes": "Wants a quote", '
            b'"sat": "False", "follow_up_date": "2026-10-02", "follow_up_time": "10:15:00", "appt_notes": "", '
            b'"post_appt_notes": "Spouse home", "monthly_cost": "$150/mo", "total_cost": "", "adders": "Panel upgrade"}',
        ('empty', 'appointment_changed'):
            b'{"phone_number": "", "appointment_type": "", "appointment_datetime": ""}',
        ('empty', 'disposition_changed'):
            b'{"phone_number": "", "disposition": "Cpfu", "call_transcript": "", "call_notes": "", "sat": "", '
            b'"follow_up_date": "", "follow_up_time": "", "appt_notes": "", "post_appt_notes": "", '
            b'"monthly_cost": "", "total_cost": "", "adders": ""}',
    }
    URLS = {'appointment_changed': 'https://hooks.example.invalid/appt',
            'disposition_changed': 'https://hooks.example.invalid/dispo'}

    def setUp(self):
        # 0038 seeds the historical versions of these two configs into every
        # fresh database; replace them with production's current shape.
        WebhookConfig.objects.filter(organization=self.sunshine).delete()
        WebhookConfig.objects.create(
            name='Datetime Changed send to GHL', trigger='appointment_changed',
            url=self.URLS['appointment_changed'], fields=TS_APPT_FIELDS, headers=[], organization=self.sunshine)
        WebhookConfig.objects.create(
            name='Dispo changed to GHL', trigger='disposition_changed',
            url=self.URLS['disposition_changed'], fields=TS_DISPO_FIELDS, headers=[], organization=self.sunshine)

    def sent(self, trigger, lead):
        with mock.patch('maps.views.urllib.request.urlopen') as urlopen:
            urlopen.return_value.status = 200
            urlopen.return_value.read.return_value = b'ok'
            _do_fire_webhooks(trigger, lead.id, self.sunshine.id)
        self.assertEqual(urlopen.call_count, 1)
        return urlopen.call_args.args[0]

    def test_live_config_payloads_are_byte_identical(self):
        full = Lead.all_objects.create(
            organization=self.sunshine, rep=self.s_rep, address='20 Sunshine Rd', homeowner_name='Sam Sunshine',
            phone_number='(781) 555-0142', appointment_type='hvac',
            appointment_datetime=dt.datetime(2026, 9, 21, 14, 30, tzinfo=EASTERN),
            disposition='needs_reschedule', call_transcript='Rep: sat with them\nAlfred: noted — "quotes" & café',
            call_notes='Wants a quote', sat=False, follow_up_date=dt.date(2026, 10, 2),
            follow_up_time=dt.time(10, 15), appt_notes='', post_appt_notes='Spouse home',
            monthly_cost='$150/mo', total_cost='', adders='Panel upgrade', hub_customer_id='cus_should_not_leak')
        empty = Lead.all_objects.create(organization=self.sunshine, address='21 Sunshine Rd', disposition='cpfu', sat=None)
        for name, lead in (('full', full), ('empty', empty)):
            for trigger in ('appointment_changed', 'disposition_changed'):
                req = self.sent(trigger, lead)
                self.assertEqual(req.data, self.GOLDEN[(name, trigger)], (name, trigger))
                self.assertEqual(req.get_method(), 'POST')
                self.assertEqual(req.full_url, self.URLS[trigger])
                self.assertEqual(sorted(req.header_items()), [('Content-type', 'application/json')])

    @mock.patch('maps.views.geocode', return_value=(42.6, -71.3))
    def test_their_ghl_bookings_still_send_them_nothing(self, _geocode):
        # A new GHL booking, a move, and a reschedule on Team Sunshine's key:
        # all three now emit events, and none may reach their GHL.
        timers = _ImmediateTimers()
        with mock.patch('maps.views.threading.Timer', timers), \
                mock.patch('maps.views.urllib.request.urlopen') as urlopen:
            self.api('post', '/api/v1/ghl/appointment/', self.s_key, ghl_body())
            self.api('post', '/api/v1/ghl/appointment/', self.s_key, ghl_body(**{'Day and Time': '2026-09-25 1:00 PM'}))
            self.api('post', '/api/v1/ghl/reschedule/', self.s_key, ghl_body(**{'Day and Time': '2026-09-26 1:00 PM'}))
            # The two reschedules share a debounce key, so they collapse to one.
            self.assertEqual(sorted(args[0] for _fn, args in timers.pending),
                             ['appt_rescheduled', 'lead_created'])
            timers.run()
        urlopen.assert_not_called()
        self.assertFalse(GHLWebhookLog.objects.filter(direction='outbound').exists())

    @mock.patch('maps.views.geocode', return_value=(42.6, -71.3))
    def test_a_ventana_config_would_receive_the_events(self, _geocode):
        # Positive control for the test above: the same flow with (test-only)
        # Ventana configs does deliver, carrying the new id fields, and each
        # delivery leaves its audit row.
        for trigger in ('lead_created', 'appt_rescheduled'):
            WebhookConfig.objects.create(
                name=f'Hub {trigger}', trigger=trigger, url=f'https://hub.example.invalid/{trigger}',
                fields=['id', 'rep_id', 'hub_customer_id', 'homeowner_name'], organization=self.ventana)
        timers = _ImmediateTimers()
        with mock.patch('maps.views.threading.Timer', timers), \
                mock.patch('maps.views.urllib.request.urlopen') as urlopen:
            urlopen.return_value.status = 200
            urlopen.return_value.read.return_value = b'ok'
            lead_id = self.api('post', '/api/v1/ghl/appointment/', self.v_key, ghl_body()).json()['id']
            self.api('post', '/api/v1/ghl/reschedule/', self.v_key, ghl_body(**{'Day and Time': '2026-09-24 9:00 AM'}))
            timers.run()
        sent = {req.full_url.rsplit('/', 1)[1]: json.loads(req.data)
                for req in (c.args[0] for c in urlopen.call_args_list)}
        expected = {'id': str(lead_id), 'rep_id': '', 'hub_customer_id': '', 'homeowner_name': 'Nina New'}
        self.assertEqual(sent, {'lead_created': expected, 'appt_rescheduled': expected})
        logged = GHLWebhookLog.objects.filter(direction='outbound', organization=self.ventana, success=True)
        self.assertEqual(sorted(logged.values_list('webhook_type', flat=True)), ['appt_rescheduled', 'lead_created'])


class WebhookTriggerTests(TestCase):
    def test_every_trigger_fits_the_delivery_log(self):
        # Each delivery is logged with webhook_type=<trigger>. A longer name
        # fails that INSERT inside the timer thread, after the POST has gone
        # out, so the audit row is silently lost. PostgreSQL enforces the
        # length; SQLite does not, so check it here where both see it.
        limit = GHLWebhookLog._meta.get_field('webhook_type').max_length
        for trigger, _label in WebhookConfig.TRIGGER_CHOICES:
            self.assertLessEqual(len(trigger), limit, trigger)
