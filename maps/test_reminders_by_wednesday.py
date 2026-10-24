"""L12: an organization whose reps Wednesday's rep loop reminds gets no
reminder from Sutton — no 3-hour SMS, no 4-hour Alfred call, no follow-up
reminder — and its run lists the lead ids it skipped. Off by default, which
changes nothing. Fictional 555 numbers only."""
from datetime import date, datetime, time
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.management import call_command

from .models import Lead, Organization
from .tests import TenancyBase

EASTERN = ZoneInfo('America/New_York')
TARGET = 'maps.management.commands.check_dispo_reminders'


class RemindersByWednesdayTests(TenancyBase):
    def setUp(self):
        # Each org: one lead at 10:00 with no disposition (due its 3-hour SMS at
        # 15:00) and one CPFU whose follow-up is today at 14:00.
        for org, lead, rep in ((self.ventana, self.v_lead, self.v_rep), (self.sunshine, self.s_lead, self.s_rep)):
            Lead.all_objects.filter(id=lead.id).update(
                rep=rep, disposition='', cancelled=False, dispo_reminder_sent_at=None, dispo_call_made_at=None,
                appointment_datetime=datetime(2026, 11, 2, 10, 0, tzinfo=EASTERN))
            Lead.all_objects.create(
                address='5 F St', homeowner_name='Follow Up', organization=org, rep=rep, disposition='cpfu',
                follow_up_date=date(2026, 11, 2), follow_up_time=time(14, 0))

    def run_worker(self, at=datetime(2026, 11, 2, 15, 0, tzinfo=EASTERN)):
        sms, calls, out = [], [], StringIO()

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return at
        with mock.patch(f'{TARGET}.datetime', Fixed), \
                mock.patch(f'{TARGET}.send_sms', side_effect=lambda to, body: sms.append(to)), \
                mock.patch(f'{TARGET}.make_outbound_call', side_effect=lambda to, lead_id: calls.append(lead_id)):
            call_command('check_dispo_reminders', stdout=out)
        return sms, calls, out.getvalue()

    def test_off_by_default_both_orgs_are_reminded_as_before(self):
        self.assertFalse(Organization.objects.filter(reminders_by_wednesday=True).exists())
        sms, _calls, out = self.run_worker()
        # each org: the 3-hour SMS and the follow-up reminder
        self.assertEqual(sorted(sms), sorted(['6175550001', '6175550001', '6175550002', '6175550002']))
        self.assertNotIn('Reminders by Wednesday', out)

    def test_on_for_ventana_sutton_sends_ventana_nothing_and_lists_the_skipped_ids(self):
        Organization.objects.filter(id=self.ventana.id).update(reminders_by_wednesday=True)
        v_follow = Lead.all_objects.get(organization=self.ventana, disposition='cpfu')
        sms, calls, out = self.run_worker()
        self.assertEqual(sorted(sms), ['6175550002', '6175550002'])  # Team Sunshine's, unchanged
        self.assertEqual(calls, [])
        self.assertIn('Reminders by Wednesday (L12): Sutton sends none for this organization. '
                      f'Would have reminded — dispo: [{self.v_lead.id}]; follow-up: [{v_follow.id}]', out)
        # nothing is written on a skipped lead
        v = Lead.all_objects.get(id=self.v_lead.id)
        self.assertIsNone(v.dispo_reminder_sent_at)
        self.assertIsNone(Lead.all_objects.get(id=v_follow.id).follow_up_reminder_sent_at)
        ventana_part = out.split('--- Team Sunshine')[0]
        for name in ('Follow Up', 'Vera Ventana'):
            self.assertNotIn(name, ventana_part)  # ids only, never a name

    def test_on_also_skips_the_alfred_call(self):
        Organization.objects.filter(id=self.ventana.id).update(reminders_by_wednesday=True)
        for lead in (self.v_lead, self.s_lead):
            Lead.all_objects.filter(id=lead.id).update(
                dispo_reminder_sent_at=datetime(2026, 11, 2, 13, 0, tzinfo=EASTERN))
        _sms, calls, _out = self.run_worker()
        self.assertEqual(calls, [self.s_lead.id])

    def test_the_switch_command_touches_one_column_of_one_org(self):
        out = StringIO()
        call_command('wednesday_reminders', 'ventana', 'on', stdout=out)
        self.assertIn('reminders_by_wednesday was off, now on', out.getvalue())
        self.assertTrue(Organization.objects.get(id=self.ventana.id).reminders_by_wednesday)
        self.assertFalse(Organization.objects.get(id=self.sunshine.id).reminders_by_wednesday)
        out = StringIO()
        call_command('wednesday_reminders', 'ventana', stdout=out)
        self.assertIn('was on, now on', out.getvalue())
        call_command('wednesday_reminders', 'ventana', 'off', stdout=StringIO())
        self.assertFalse(Organization.objects.get(id=self.ventana.id).reminders_by_wednesday)
