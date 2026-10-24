"""U2: Alfred answers only the organizations that have not asked it to
decline (maps/alfred_org.py, Organization.alfred_declines). Guardrail 12's
three cases — a phone in both organizations, an unknown phone, a
Ventana-only phone — and that the default changes nothing. Fictional 555
numbers only."""
from datetime import datetime
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase

from .alfred_org import DECLINE_BOTH, DECLINE_ORG, resolve_caller, resolve_reminder, strict_mode
from .models import Lead, Manager, Organization, Rep, VoiceCallLog
from .tenancy import reset_current_org, set_current_org
from .tests import TenancyBase

EASTERN = ZoneInfo('America/New_York')
BOTH = '6175550009'  # a rep in both organizations
V_ONLY = '6175550001'  # TenancyBase's Ventana rep
S_ONLY = '6175550002'  # TenancyBase's Team Sunshine rep
UNKNOWN = '6175550099'


class AlfredBase(TenancyBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.v_both = Rep.all_objects.create(name='Both V', phone_number=BOTH, home_address='3 C St', organization=cls.ventana)
        cls.s_both = Rep.all_objects.create(name='Both S', phone_number=BOTH, home_address='4 D St', organization=cls.sunshine)

    def answer(self, caller):
        return Client().post('/voice/answer/', {'From': f'+1{caller}', 'CallSid': 'CA0000000000000000000000000000000a'})

    def reminder(self, lead_id, to=V_ONLY):
        return Client().post(f'/voice/reminder-call/?lead_id={lead_id}',
                             {'To': f'+1{to}', 'CallSid': 'CA0000000000000000000000000000000b'})

    def decline_sunshine(self):
        Organization.objects.filter(id=self.sunshine.id).update(alfred_declines=True)


class DefaultChangesNothingTests(AlfredBase):
    def test_off_by_default(self):
        self.assertFalse(Organization.objects.filter(alfred_declines=True).exists())
        self.assertFalse(strict_mode())

    def test_every_call_is_connected_as_before(self):
        for caller in (V_ONLY, S_ONLY, BOTH, UNKNOWN):
            body = self.answer(caller).content.decode()
            self.assertIn('<Connect>', body)
            self.assertIn(f'<Parameter name="callerNumber" value="+1{caller}" />', body)
            self.assertNotIn('orgId', body)
            self.assertNotIn('<Say>', body)
        body = self.reminder(self.s_lead.id).content.decode()
        self.assertIn(f'<Parameter name="reminderLeadId" value="{self.s_lead.id}" />', body)
        self.assertNotIn('orgId', body)

    def test_the_reminder_worker_calls_as_before(self):
        sent = run_worker(self)
        self.assertEqual(sorted(sent), sorted([self.v_lead.id, self.s_lead.id]))


class StrictResolutionTests(AlfredBase):
    """Team Sunshine has asked Alfred to decline it."""

    def setUp(self):
        self.decline_sunshine()

    def test_strict_once_any_org_declines(self):
        self.assertTrue(strict_mode())

    def test_a_ventana_only_phone_is_answered_in_ventana(self):
        self.assertEqual(resolve_caller(f'+1{V_ONLY}'), {'org_id': self.ventana.id, 'decline': None})
        body = self.answer(V_ONLY).content.decode()
        self.assertIn('<Connect>', body)
        self.assertIn(f'<Parameter name="orgId" value="{self.ventana.id}" />', body)

    def test_a_phone_in_both_orgs_is_declined_and_logged_never_guessed(self):
        self.assertEqual(resolve_caller(f'+1{BOTH}')['decline'], 'both_orgs')
        with self.assertLogs('maps.alfred_org', level='WARNING') as logs:
            body = self.answer(BOTH).content.decode()
        self.assertIn(f'<Say>{DECLINE_BOTH}</Say>', body)
        self.assertIn('<Hangup />', body)
        self.assertNotIn('<Connect>', body)
        self.assertIn('reason=both_orgs', logs.output[0])
        self.assertIn('caller=…0009', logs.output[0])
        self.assertNotIn(BOTH, logs.output[0])

    def test_an_unknown_phone_is_the_default_inbound_orgs_and_it_declines(self):
        self.assertEqual(resolve_caller(f'+1{UNKNOWN}'), {'org_id': self.sunshine.id, 'decline': 'org_declines'})
        body = self.answer(UNKNOWN).content.decode()
        self.assertIn(f'<Say>{DECLINE_ORG}</Say>', body)
        self.assertNotIn('<Connect>', body)

    def test_a_team_sunshine_phone_is_declined(self):
        self.assertEqual(resolve_caller(f'+1{S_ONLY}'), {'org_id': self.sunshine.id, 'decline': 'org_declines'})
        self.assertIn(f'<Say>{DECLINE_ORG}</Say>', self.answer(S_ONLY).content.decode())

    def test_a_manager_is_found_when_no_rep_matches(self):
        Manager.all_objects.create(name='V Mgr', phone_number='6175550077', organization=self.ventana)
        self.assertEqual(resolve_caller('+16175550077'), {'org_id': self.ventana.id, 'decline': None})
        Manager.all_objects.create(name='S Mgr', phone_number='6175550077', organization=self.sunshine)
        self.assertEqual(resolve_caller('+16175550077')['decline'], 'both_orgs')

    def test_a_declined_call_writes_nothing(self):
        before = VoiceCallLog.all_objects.count()
        for caller in (BOTH, UNKNOWN, S_ONLY):
            self.answer(caller)
        self.assertEqual(VoiceCallLog.all_objects.count(), before)

    def test_a_reminder_call_belongs_to_its_leads_org(self):
        self.assertEqual(resolve_reminder(self.v_lead.id), {'org_id': self.ventana.id, 'decline': None})
        body = self.reminder(self.v_lead.id).content.decode()
        self.assertIn(f'<Parameter name="orgId" value="{self.ventana.id}" />', body)
        self.assertIn(f'<Say>{DECLINE_ORG}</Say>', self.reminder(self.s_lead.id, to=S_ONLY).content.decode())
        self.assertEqual(resolve_reminder('not-a-number')['decline'], 'org_declines')
        self.assertEqual(resolve_reminder(999999)['decline'], 'org_declines')

    def test_the_session_reuses_the_fixed_org(self):
        """A phone in both organizations, fixed to Ventana at answer time:
        the session finds Ventana's rep and runs in Ventana."""
        from voice_ws import get_rep_context
        # get_rep_context sets the active organization, and asgiref carries
        # that back to this thread: put it back for the tests that follow.
        token = set_current_org(None)
        try:
            ctx = async_to_sync(get_rep_context)(f'+1{BOTH}', org_id=self.ventana.id)
            self.assertEqual(ctx['rep'].id, self.v_both.id)
            self.assertEqual(ctx['org_id'], self.ventana.id)
            ctx = async_to_sync(get_rep_context)(f'+1{BOTH}', org_id=self.sunshine.id)
            self.assertEqual(ctx['rep'].id, self.s_both.id)
        finally:
            reset_current_org(token)

    def test_the_reminder_worker_makes_no_call_for_a_declining_org(self):
        sent = run_worker(self)
        self.assertEqual(sent, [self.v_lead.id])


class SwitchTests(TestCase):
    def test_the_command_sets_one_column_of_one_org(self):
        out = StringIO()
        call_command('alfred_declines', 'team-sunshine', 'on', stdout=out)
        self.assertIn('was off, now on', out.getvalue())
        self.assertTrue(Organization.objects.get(slug='team-sunshine').alfred_declines)
        self.assertFalse(Organization.objects.get(slug='ventana').alfred_declines)
        call_command('alfred_declines', 'team-sunshine', 'off', stdout=out)
        self.assertFalse(Organization.objects.get(slug='team-sunshine').alfred_declines)

    def test_the_admin_is_superusers_only_and_edits_nothing_else(self):
        from django.contrib import admin
        from django.test import RequestFactory
        model_admin = admin.site._registry[Organization]
        boss = User.objects.create_user('boss', password='pw', is_superuser=True, is_staff=True)
        staff = User.objects.create_user('staff', password='pw', is_staff=True)
        request = RequestFactory().get('/')
        request.user = boss
        self.assertTrue(model_admin.has_change_permission(request))
        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_delete_permission(request))
        self.assertEqual(set(model_admin.get_fields(request)) - set(model_admin.get_readonly_fields(request)),
                         {'alfred_declines'})
        request.user = staff
        self.assertFalse(model_admin.has_module_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))


def run_worker(test):
    """check_dispo_reminders at 15:00 New York on a day when both orgs' leads
    were at 10:00 and were texted at 13:00: returns the lead ids it called."""
    today = datetime(2026, 11, 2, 15, 0, tzinfo=EASTERN)
    for lead, rep in ((test.v_lead, test.v_rep), (test.s_lead, test.s_rep)):
        Lead.all_objects.filter(id=lead.id).update(
            rep=rep, disposition='', cancelled=False,
            appointment_datetime=datetime(2026, 11, 2, 10, 0, tzinfo=EASTERN),
            dispo_reminder_sent_at=datetime(2026, 11, 2, 13, 0, tzinfo=EASTERN), dispo_call_made_at=None)
    calls = []

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return today
    target = 'maps.management.commands.check_dispo_reminders'
    with mock.patch(f'{target}.datetime', Fixed), \
            mock.patch(f'{target}.send_sms'), \
            mock.patch(f'{target}.make_outbound_call', side_effect=lambda to, lead_id: calls.append(lead_id)):
        call_command('check_dispo_reminders', stdout=StringIO())
    return calls
