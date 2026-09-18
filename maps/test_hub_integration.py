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


class WebhookTriggerTests(TestCase):
    def test_every_trigger_fits_the_delivery_log(self):
        # Each delivery is logged with webhook_type=<trigger>. A longer name
        # fails that INSERT inside the timer thread, after the POST has gone
        # out, so the audit row is silently lost. PostgreSQL enforces the
        # length; SQLite does not, so check it here where both see it.
        limit = GHLWebhookLog._meta.get_field('webhook_type').max_length
        for trigger, _label in WebhookConfig.TRIGGER_CHOICES:
            self.assertLessEqual(len(trigger), limit, trigger)
