"""Tenancy tests: fail-closed isolation between organizations, org routing
for the unauthenticated automations, and the audited superuser org-switch.

The 0040 data migration creates the two real organizations, so tests reuse
them: 'ventana' (owner) and 'team-sunshine' (default inbound).
"""
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase

from .models import (
    APITenant, Lead, Manager, Organization, OrgSwitchAudit, Rep,
    TimeOffRequest, UserProfile, WebhookConfig,
)
from .tenancy import get_current_org_id, org_context


class TenancyBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.sunshine = Organization.objects.get(slug='team-sunshine')

        cls.v_manager = User.objects.create_user('v_manager', password='pw')
        UserProfile.all_objects.create(user=cls.v_manager, role='manager', organization=cls.ventana)
        cls.s_manager = User.objects.create_user('s_manager', password='pw')
        UserProfile.all_objects.create(user=cls.s_manager, role='manager', organization=cls.sunshine)

        cls.owner = User.objects.create_user('owner', password='pw', is_superuser=True)
        UserProfile.all_objects.create(user=cls.owner, role='manager', organization=cls.ventana)

        cls.v_rep = Rep.all_objects.create(
            name='V Rep', phone_number='6175550001', home_address='1 A St', organization=cls.ventana)
        cls.s_rep = Rep.all_objects.create(
            name='S Rep', phone_number='6175550002', home_address='2 B St', organization=cls.sunshine)

        cls.v_lead = Lead.all_objects.create(
            address='10 Ventana Way', homeowner_name='Vera Ventana',
            phone_number='6175551111', organization=cls.ventana)
        cls.s_lead = Lead.all_objects.create(
            address='20 Sunshine Rd', homeowner_name='Sam Sunshine',
            phone_number='6175552222', organization=cls.sunshine)


class ScopedManagerTests(TenancyBase):
    def test_no_context_is_fail_closed(self):
        self.assertIsNone(get_current_org_id())
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(Rep.objects.count(), 0)
        self.assertEqual(Lead.all_objects.count(), 2)

    def test_context_scopes_queries(self):
        with org_context(self.ventana):
            self.assertEqual(list(Lead.objects.all()), [self.v_lead])
        with org_context(self.sunshine):
            self.assertEqual(list(Lead.objects.all()), [self.s_lead])

    def test_create_stamps_active_org(self):
        with org_context(self.ventana):
            lead = Lead.objects.create(address='30 New St')
            self.assertEqual(lead.organization_id, self.ventana.id)

    def test_save_stamps_active_org(self):
        with org_context(self.sunshine):
            lead = Lead(address='40 Save St')
            lead.save()
            self.assertEqual(lead.organization_id, self.sunshine.id)

    def test_null_org_rows_are_invisible(self):
        orphan = Lead.all_objects.create(address='50 Orphan St', organization=None)
        for org in (self.ventana, self.sunshine):
            with org_context(org):
                self.assertNotIn(orphan, Lead.objects.all())


class ViewIsolationTests(TenancyBase):
    def _leads_api_ids(self, user):
        client = Client()
        client.force_login(user)
        resp = client.get('/api/leads/')
        self.assertEqual(resp.status_code, 200)
        return {row['id'] for row in resp.json()}

    def test_leads_api_scoped_per_manager(self):
        self.v_lead.latitude = self.v_lead.longitude = 42.0
        self.v_lead.save()
        self.s_lead.latitude = self.s_lead.longitude = 42.0
        self.s_lead.save()
        self.assertEqual(self._leads_api_ids(self.v_manager), {self.v_lead.id})
        self.assertEqual(self._leads_api_ids(self.s_manager), {self.s_lead.id})

    def test_cannot_edit_other_orgs_lead(self):
        client = Client()
        client.force_login(self.v_manager)
        resp = client.put(
            f'/api/leads/{self.s_lead.id}/',
            data=json.dumps({'homeowner_name': 'Hacked'}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 404)
        self.s_lead.refresh_from_db()
        self.assertEqual(self.s_lead.homeowner_name, 'Sam Sunshine')

    def test_cannot_delete_other_orgs_lead_via_bulk(self):
        client = Client()
        client.force_login(self.v_manager)
        resp = client.post(
            '/api/leads/bulk-delete/',
            data=json.dumps({'ids': [self.s_lead.id]}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Lead.all_objects.filter(id=self.s_lead.id).exists())

    def test_users_api_scoped(self):
        client = Client()
        client.force_login(self.v_manager)
        usernames = {u['username'] for u in client.get('/api/users/').json()}
        self.assertIn('v_manager', usernames)
        self.assertNotIn('s_manager', usernames)

    def test_dashboard_scoped(self):
        client = Client()
        client.force_login(self.s_manager)
        summary = client.get('/api/dashboard/').json()['summary']
        self.assertEqual(summary['total'], 1)

    def test_user_without_org_sees_nothing(self):
        noorg = User.objects.create_user('noorg', password='pw')
        UserProfile.all_objects.create(user=noorg, role='manager', organization=None)
        client = Client()
        client.force_login(noorg)
        resp = client.get('/api/leads/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])


class SmsWebhookRoutingTests(TenancyBase):
    def _post_sms(self, from_number, body):
        return Client().post('/sms/', {'From': from_number, 'Body': body})

    def test_unknown_sender_routes_to_default_inbound_org(self):
        resp = self._post_sms('+19995550000', 'Name: Test Homeowner\nPhone: 6175559999')
        self.assertEqual(resp.status_code, 200)
        lead = Lead.all_objects.get(homeowner_name='Test Homeowner')
        self.assertEqual(lead.organization_id, self.sunshine.id)

    def test_rep_time_off_lands_in_reps_org(self):
        resp = self._post_sms('+16175550001', 'V Rep\nOff Friday')
        self.assertEqual(resp.status_code, 200)
        tor = TimeOffRequest.all_objects.get(rep=self.v_rep)
        self.assertEqual(tor.organization_id, self.ventana.id)

    def test_manager_approve_only_sees_own_orgs_requests(self):
        Manager.all_objects.create(
            name='V Boss', phone_number='6175550009', organization=self.ventana)
        with org_context(self.sunshine):
            s_tor = TimeOffRequest.objects.create(
                rep=self.s_rep, start_date='2026-08-10', status='pending')
        resp = self._post_sms('+16175550009', 'APPROVE')
        self.assertEqual(resp.status_code, 200)
        s_tor.refresh_from_db()
        self.assertEqual(s_tor.status, 'pending')


class ApiKeyOrgTests(TenancyBase):
    def test_v1_leads_scoped_to_tenant_org(self):
        tenant = APITenant.objects.create(name='V Key', organization=self.ventana)
        resp = Client().get('/api/v1/leads/', HTTP_AUTHORIZATION=f'Bearer {tenant.api_key}')
        self.assertEqual(resp.status_code, 200)
        ids = {row['id'] for row in resp.json()['leads']}
        self.assertEqual(ids, {self.v_lead.id})

    def test_orgless_tenant_falls_back_to_default_inbound(self):
        tenant = APITenant.objects.create(name='Legacy Key', organization=None)
        resp = Client().get('/api/v1/leads/', HTTP_AUTHORIZATION=f'Bearer {tenant.api_key}')
        self.assertEqual(resp.status_code, 200)
        ids = {row['id'] for row in resp.json()['leads']}
        self.assertEqual(ids, {self.s_lead.id})


class OrgSwitchTests(TenancyBase):
    def test_superuser_switch_is_scoped_and_audited(self):
        client = Client()
        client.force_login(self.owner)
        self.v_lead.latitude = self.v_lead.longitude = 42.0
        self.v_lead.save()
        self.s_lead.latitude = self.s_lead.longitude = 42.0
        self.s_lead.save()

        ids = {row['id'] for row in client.get('/api/leads/').json()}
        self.assertEqual(ids, {self.v_lead.id})

        resp = client.post(
            '/org/switch/', data=json.dumps({'org_id': self.sunshine.id}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 302)

        ids = {row['id'] for row in client.get('/api/leads/').json()}
        self.assertEqual(ids, {self.s_lead.id})

        audit = OrgSwitchAudit.objects.get()
        self.assertEqual(audit.user, self.owner)
        self.assertEqual(audit.from_organization_id, self.ventana.id)
        self.assertEqual(audit.to_organization_id, self.sunshine.id)

        resp = client.post(
            '/org/switch/', data=json.dumps({'org_id': self.ventana.id}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 302)
        ids = {row['id'] for row in client.get('/api/leads/').json()}
        self.assertEqual(ids, {self.v_lead.id})
        self.assertEqual(OrgSwitchAudit.objects.count(), 2)

    def test_non_superuser_cannot_switch(self):
        client = Client()
        client.force_login(self.s_manager)
        resp = client.post(
            '/org/switch/', data=json.dumps({'org_id': self.ventana.id}),
            content_type='application/json')
        self.assertEqual(resp.status_code, 403)


class WebhookConfigOrgTests(TenancyBase):
    def test_webhooks_fire_only_for_their_org(self):
        from .models import GHLWebhookLog
        from .views import _do_fire_webhooks
        WebhookConfig.objects.create(
            name='S hook', trigger='disposition_changed',
            url='http://invalid.localdomain/hook', organization=self.sunshine)
        # A Ventana lead must match zero configs (the hook above is Team Sunshine's)
        _do_fire_webhooks('disposition_changed', self.v_lead.id, self.ventana.id)
        self.assertEqual(
            GHLWebhookLog.objects.filter(webhook_type='disposition_changed').count(), 0)


class WorkerScopingTests(TenancyBase):
    def test_reminder_worker_runs_per_org(self):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command('check_dispo_reminders', stdout=out)
        output = out.getvalue()
        self.assertIn('Ventana Heating and Cooling', output)
        self.assertIn('Team Sunshine Construction', output)
