"""Alfred by text (maps/alfred_text.py), against a stand-in for the model.

The stand-in reads the rep's text by fixed rules, and takes "now" and the
appointment's time from the context the endpoint sent — so if that context
stops reaching the model, the date tests fail.

    python3 manage.py test maps.test_alfred_text
"""
import datetime as dt
import json
import re
from unittest import mock
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import connection
from django.test import Client, TestCase, override_settings

from . import alfred_text
from .models import APIRateWindow, APITenant, GHLWebhookLog, Lead, Organization, Rep, VoiceCallLog, WebhookConfig
from .test_hub_integration import _ImmediateTimers

EASTERN = ZoneInfo('America/New_York')
URL = '/api/v1/alfred/text/'
# Tuesday 2026-10-20, 9 PM Eastern; the appointment was at 6 PM that day.
NOW = dt.datetime(2026, 10, 20, 21, 0, tzinfo=EASTERN)
APPOINTMENT = dt.datetime(2026, 10, 20, 18, 0, tzinfo=EASTERN)


class StandInAlfred:
    """Stands in for gpt-4o-mini. Records every call."""

    def __init__(self, fixed=None, raises=None, side_effect=None):
        self.calls = []
        self.fixed, self.raises, self.side_effect = fixed, raises, side_effect

    def __call__(self, messages, tool):
        self.calls.append({'messages': messages, 'tool': tool})
        if self.side_effect:
            self.side_effect()
        if self.raises:
            raise self.raises
        if self.fixed is not None:
            return dict(self.fixed)
        system, text = messages[0]['content'], messages[1]['content'].lower()
        today = self._iso(system, 'Now')
        appt_day, appt_time = self._iso(system, 'Appointment', with_time=True)

        def next_weekday(weekday):           # Monday = 0
            ahead = (weekday - today.weekday()) % 7 or 7
            return (today + dt.timedelta(days=ahead)).isoformat()

        if 'asdf' in text:
            return {'understood': False, 'clarifying_question': "Sorry, didn't catch that. Did you sit the appointment?"}
        if 'went fine' in text:
            return {'understood': True, 'sat': True, 'clarifying_question': 'Did you run credit?'}
        if 'move it to thursday at 6' in text:
            return {'understood': True, 'appointment_moved_to_date': next_weekday(3), 'appointment_moved_to_time': '18:00'}
        if 'same time tomorrow' in text:
            return {'understood': True, 'disposition': 'follow_up', 'sat': True,
                    'call_notes': 'Needed to talk to wife', 'follow_up_date': (today + dt.timedelta(days=1)).isoformat(),
                    'follow_up_time': appt_time}
        if 'friday at 2' in text:
            return {'understood': True, 'disposition': 'follow_up', 'sat': True, 'call_notes': 'Wants to compare quotes',
                    'follow_up_date': next_weekday(4), 'follow_up_time': '14:00'}
        if 'two months' in text:
            return {'understood': True, 'disposition': 'follow_up', 'sat': True, 'call_notes': 'Buying after the holidays',
                    'follow_up_date': (today + dt.timedelta(days=60)).isoformat(), 'follow_up_time': '10:00'}
        if text.startswith('sold'):
            return {'understood': True, 'disposition': 'sale', 'sat': True,
                    'call_notes': 'Sold, credit passed and all contracts signed'}
        if 'roof too shaded' in text:
            return {'understood': True, 'disposition': 'dq', 'sat': True, 'call_notes': 'DQ: roof too shaded for solar'}
        if 'totally dead' in text:
            return {'understood': True, 'disposition': 'no_sale', 'sat': True, 'call_notes': 'Dead, wife hated the price'}
        if 'battery' in text:
            return {'understood': True, 'adders': 'Battery', 'monthly_cost': '$189'}
        if 'no idea on the total' in text:
            return {'understood': True, 'declined_fields': ['total_cost']}
        if 'nothing else' in text:
            return {'understood': True, 'declined_fields': ['post_appt_notes']}
        return {'understood': False}

    @staticmethod
    def _iso(system, label, with_time=False):
        m = re.search(rf'^{label}: .*?\((\d{{4}}-\d{{2}}-\d{{2}})\) at .*?\((\d{{2}}:\d{{2}})\)', system, re.M)
        assert m, f'no {label} date in the prompt'
        day = dt.date.fromisoformat(m.group(1))
        return (day, m.group(2)) if with_time else day


@override_settings(OPENAI_API_KEY='sk-test')
class AlfredTextBase(TestCase):
    stub_webhooks = True

    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.sunshine = Organization.objects.get(slug='team-sunshine')
        cls.v_rep = Rep.all_objects.create(
            name='Val Rep', phone_number='(617) 555-0001', home_address='1 A St', organization=cls.ventana)
        cls.v_rep2 = Rep.all_objects.create(
            name='Other Rep', phone_number='6175550003', home_address='3 C St', organization=cls.ventana)
        cls.s_rep = Rep.all_objects.create(
            name='Sun Rep', phone_number='6175550002', home_address='2 B St', organization=cls.sunshine)
        cls.v_tenant = APITenant.objects.create(name='Wednesday', organization=cls.ventana)
        cls.v_tenant2 = APITenant.objects.create(name='Marketing Canvas', organization=cls.ventana)
        cls.s_tenant = APITenant.objects.create(name='Team Sunshine', organization=cls.sunshine)
        cls.legacy_tenant = APITenant.objects.create(name='Legacy', organization=None)
        cls.lead = Lead.all_objects.create(
            address='10 Ventana Way', homeowner_name='Vera Smith', phone_number='6175551111',
            appointment_type='hvac', appointment_datetime=APPOINTMENT, organization=cls.ventana)
        cls.others_lead = Lead.all_objects.create(
            address='11 Ventana Way', homeowner_name='Otto Other', phone_number='6175551112',
            appointment_datetime=APPOINTMENT, rep=cls.v_rep2, organization=cls.ventana)
        cls.s_lead = Lead.all_objects.create(
            address='20 Sunshine Rd', homeowner_name='Sam Sunshine', phone_number='6175552222',
            appointment_datetime=APPOINTMENT, rep=cls.s_rep, organization=cls.sunshine)

    def setUp(self):
        super().setUp()
        self.now = self.start(mock.patch('maps.alfred_text.timezone.now', return_value=NOW))
        self.model = StandInAlfred()
        self.start(mock.patch('maps.alfred_text._complete', self.model))
        if self.stub_webhooks:
            self.fired = self.start(mock.patch('maps.alfred_text.fire_webhooks'))

    def start(self, patcher):
        started = patcher.start()
        self.addCleanup(patcher.stop)
        return started

    def text(self, text, tenant=None, context=None, **body):
        body = {'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'text': text, **body}
        if context is not None:
            body['context'] = context
        key = (tenant or self.v_tenant).api_key
        return Client().post(URL, data=json.dumps(body), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {key}')

    def fresh(self, lead=None):
        return Lead.all_objects.get(pk=(lead or self.lead).pk)

    def assertNothingWritten(self, before):
        after = self.fresh()
        for field in ('disposition', 'sat', 'call_notes', 'follow_up_date', 'follow_up_time', 'monthly_cost',
                      'total_cost', 'adders', 'post_appt_notes', 'call_transcript', 'appointment_datetime'):
            self.assertEqual(getattr(after, field), getattr(before, field), field)


class OutcomeTests(AlfredTextBase):
    def test_sold(self):
        r = self.text('Sold it! Credit passed, everything signed')
        self.assertEqual(r.status_code, 200, r.content)
        out = r.json()
        self.assertEqual(out['set'], {'disposition': 'sale', 'sat': True,
                                      'call_notes': 'Sold, credit passed and all contracts signed'})
        self.assertEqual(out['summary'], 'Sold, credit passed and all contracts signed')
        self.assertEqual(out['next_question'], 'Any adders on this one?')
        self.assertEqual(out['still_empty'], ['adders', 'monthly_cost', 'total_cost', 'post_appt_notes'])
        self.assertIsNone(out['appointment_move'])
        self.assertTrue(out['understood'])

        lead = self.fresh()
        self.assertEqual((lead.disposition, lead.sat, lead.call_notes),
                         ('sale', True, 'Sold, credit passed and all contracts signed'))
        self.assertIn('Val Rep: Sold it! Credit passed, everything signed', lead.call_transcript)
        self.assertIn('Alfred: Any adders on this one?', lead.call_transcript)
        self.assertIsNone(lead.rep)     # the call never assigns a rep; neither does a text

        self.fired.assert_called_once()
        self.assertEqual(self.fired.call_args.args[0], 'disposition_changed')
        self.assertEqual(self.fired.call_args.args[1].pk, self.lead.pk)

        log = VoiceCallLog.all_objects.get()
        self.assertEqual((log.channel, log.rep, log.organization_id, log.caller_number),
                         ('text', self.v_rep, self.ventana.id, '6175550001'))
        self.assertIn(f'Lead #{self.lead.id} (Vera Smith)', log.transcript)
        self.assertEqual(log.summary, 'Sold, credit passed and all contracts signed')

    def test_deal_details_come_one_at_a_time_in_the_calls_order(self):
        self.text('Sold it!')
        r = self.text('battery, about 189 a month', context={
            'last_summary': 'Sold, credit passed and all contracts signed',
            'last_question': 'Any adders on this one?',
            'empty_fields': ['adders', 'monthly_cost', 'total_cost', 'post_appt_notes']})
        out = r.json()
        self.assertEqual(out['set'], {'adders': 'Battery', 'monthly_cost': '$189'})
        self.assertEqual(out['next_question'], 'And the total?')
        self.assertEqual(out['summary'], 'Sold, credit passed and all contracts signed')
        self.assertIn('Last summary: Sold, credit passed and all contracts signed',
                      self.model.calls[-1]['messages'][0]['content'])
        self.assertIn("Alfred's last question: Any adders on this one?",
                      self.model.calls[-1]['messages'][0]['content'])

        out = self.text('no idea on the total yet', context={
            'last_summary': out['summary'], 'last_question': out['next_question'],
            'empty_fields': out['still_empty']}).json()
        self.assertEqual(out['set'], {})
        self.assertEqual(out['next_question'], 'Anything else worth noting?')
        out = self.text('nothing else', context={
            'last_summary': out['summary'], 'last_question': out['next_question'],
            'empty_fields': out['still_empty']}).json()
        self.assertIsNone(out['next_question'])
        self.assertEqual(out['still_empty'], [])

        lead = self.fresh()
        self.assertEqual((lead.disposition, lead.adders, lead.monthly_cost, lead.total_cost),
                         ('sale', 'Battery', '$189', ''))
        self.assertEqual(lead.call_notes, 'Sold, credit passed and all contracts signed')
        self.assertEqual(lead.call_transcript.count('[Text '), 2)    # each exchange that wrote is kept
        self.assertEqual(self.fired.call_count, 2)
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 4)

    def test_dead_with_reason(self):
        out = self.text('No credit run, totally dead, wife hated the price').json()
        self.assertEqual(out['set'], {'disposition': 'no_sale', 'sat': True, 'call_notes': 'Dead, wife hated the price'})
        self.assertEqual(out['summary'], 'Dead, wife hated the price')
        self.assertIsNone(out['next_question'])     # no deal questions on a dead deal
        self.assertEqual(out['still_empty'], [])
        self.assertEqual(self.fresh().disposition, 'no_sale')

    def test_dq_with_reason(self):
        out = self.text('DQ, roof too shaded').json()
        self.assertEqual(out['set']['disposition'], 'dq')
        self.assertEqual(out['summary'], 'DQ: roof too shaded for solar')
        self.assertIsNone(out['next_question'])

    def test_outcome_not_settled_asks_the_decision_tree(self):
        before = self.fresh()
        out = self.text('It went fine, they liked it').json()
        self.assertEqual(out['next_question'], 'Did you run credit?')
        self.assertEqual(out['set'], {'sat': True})
        self.assertEqual(self.fresh().disposition, '')
        self.assertEqual(before.disposition, '')
        self.fired.assert_not_called()      # no disposition yet, nothing to announce


class ChainTests(AlfredTextBase):
    """Passing back each response's summary, next_question and still_empty."""

    def chain(self, *texts):
        out = None
        for text in texts:
            context = None if out is None else {'last_summary': out['summary'], 'last_question': out['next_question'],
                                                'empty_fields': out['still_empty']}
            out = self.text(text, context=context).json()
        return out

    def test_an_outcome_settled_later_still_asks_its_questions(self):
        # "It went fine" leaves the outcome open (still_empty []); the outcome
        # that follows brings its own questions in.
        out = self.chain('It went fine, they liked it', 'Sold it!')
        self.assertEqual(out['next_question'], 'Any adders on this one?')
        self.model.fixed = {'understood': True, 'disposition': 'follow_up', 'call_notes': 'Wants to think, no credit'}
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='', adders='', monthly_cost='')
        out = self.chain('It went fine, they liked it', 'no credit, still life')
        self.assertEqual(out['next_question'], 'When would be a good time to follow up with the homeowner?')
        self.assertEqual(out['still_empty'], ['follow_up_date', 'follow_up_time', 'adders', 'monthly_cost',
                                              'total_cost', 'post_appt_notes'])

    def test_an_outcome_that_changes_brings_in_its_questions(self):
        # A sale's deal questions are under way; it turns out to be a cpfu.
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='sale', sat=True, adders='Battery')
        self.model.fixed = {'understood': True, 'disposition': 'cpfu', 'call_notes': 'Credit passed, not signed'}
        out = self.text('actually they did not sign yet', context={
            'last_summary': 'Sold', 'last_question': "What's the monthly looking like?",
            'empty_fields': ['monthly_cost', 'total_cost', 'post_appt_notes']}).json()
        self.assertEqual(out['next_question'], 'When would be a good time to follow up with the homeowner?')
        self.assertEqual(out['still_empty'], ['follow_up_date', 'follow_up_time', 'monthly_cost', 'total_cost',
                                              'post_appt_notes'])

    def test_a_detail_the_rep_said_nothing_for_is_not_asked_again(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='sale', sat=True)
        out = self.chain('no idea on the total yet')
        self.assertEqual(out['still_empty'], ['adders', 'monthly_cost', 'post_appt_notes'])
        out = self.text('battery, about 189 a month', context={
            'last_summary': '', 'last_question': out['next_question'], 'empty_fields': out['still_empty']}).json()
        self.assertEqual(out['next_question'], 'Anything else worth noting?')     # not the total again


class WindowTests(AlfredTextBase):
    def test_a_cancelled_appointment_is_refused(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(cancelled=True)
        r = self.text('Sold it!')
        self.assertEqual((r.status_code, r.json()['error']), (409, 'That appointment is cancelled.'))
        self.assertEqual(self.model.calls, [])

    def test_only_recent_appointments(self):
        for days, status in ((-20, 409), (-14, 200), (3, 200), (4, 409)):
            Lead.all_objects.filter(pk=self.lead.pk).update(
                appointment_datetime=NOW.replace(hour=12) + dt.timedelta(days=days), disposition='')
            self.assertEqual(self.text('Sold it!').status_code, status, days)
        Lead.all_objects.filter(pk=self.lead.pk).update(appointment_datetime=None)
        self.assertEqual(self.text('Sold it!').status_code, 409)

    def test_a_follow_up_coming_due_long_after_the_appointment(self):
        for follow_up, status in ((dt.date(2026, 10, 20), 200), (dt.date(2026, 10, 1), 409), (None, 409)):
            Lead.all_objects.filter(pk=self.lead.pk).update(
                appointment_datetime=APPOINTMENT - dt.timedelta(days=40), disposition='cpfu', follow_up_date=follow_up)
            self.assertEqual(self.text('Sold it!').status_code, status, follow_up)


class DateTests(AlfredTextBase):
    def test_a_time_days_out(self):
        out = self.text('Follow up, no credit run, call him Friday at 2').json()
        self.assertEqual(out['set']['disposition'], 'follow_up')
        self.assertEqual((out['set']['follow_up_date'], out['set']['follow_up_time']), ('2026-10-23', '14:00'))
        self.assertIsNone(out['appointment_move'])
        self.assertEqual(out['next_question'], 'Any adders on this one?')
        lead = self.fresh()
        self.assertEqual((lead.follow_up_date, lead.follow_up_time), (dt.date(2026, 10, 23), dt.time(14, 0)))
        self.assertEqual(lead.appointment_datetime, APPOINTMENT)

    def test_more_than_a_month_out_is_future_contact(self):
        out = self.text('follow up in two months').json()
        self.assertEqual(out['set']['disposition'], 'future_contact')
        self.assertEqual(out['set']['follow_up_date'], '2026-12-19')

    def test_a_near_follow_up_on_a_future_contact_lead_asks_the_outcome(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='future_contact',
                                                        follow_up_date=dt.date(2027, 1, 15))
        self.model.fixed = {'understood': True, 'follow_up_date': '2026-10-23', 'follow_up_time': '10:00'}
        out = self.text('actually call her friday at 10').json()
        self.assertEqual(out['next_question'], 'Did you run credit?')
        self.assertNotIn('follow_up_date', out['set'])
        lead = self.fresh()
        self.assertEqual((lead.disposition, lead.follow_up_date), ('future_contact', dt.date(2027, 1, 15)))
        # With the outcome, it is written.
        self.model.fixed = {'understood': True, 'disposition': 'cpfu', 'follow_up_date': '2026-10-23',
                            'follow_up_time': '10:00'}
        out = self.text('credit passed, call her friday at 10').json()
        self.assertEqual((out['set']['disposition'], out['set']['follow_up_date']), ('cpfu', '2026-10-23'))

    def test_a_follow_up_lead_with_no_date_is_asked_the_date(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='follow_up', sat=True)
        self.model.fixed = {'understood': True, 'adders': 'Battery'}
        out = self.text('battery').json()
        self.assertEqual(out['next_question'], 'When would be a good time to follow up with the homeowner?')

    def test_a_later_date_turns_an_earlier_follow_up_into_future_contact(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='follow_up')
        self.model.fixed = {'understood': True, 'follow_up_date': '2027-01-15', 'follow_up_time': '10:00'}
        out = self.text('make it mid January, 10am').json()
        self.assertEqual(out['set']['disposition'], 'future_contact')

    def test_same_time_tomorrow(self):
        out = self.text('Follow up same time tomorrow, she needs to talk to her husband').json()
        # Tomorrow, at the appointment's time of day — which the stand-in can
        # only know from the prompt.
        self.assertEqual((out['set']['follow_up_date'], out['set']['follow_up_time']), ('2026-10-21', '18:00'))
        self.assertIsNone(out['appointment_move'])
        system = self.model.calls[0]['messages'][0]['content']
        self.assertIn('Now: Tuesday, October 20, 2026 (2026-10-20) at 09:00 PM (21:00), Eastern', system)
        self.assertIn('Appointment: Vera Smith, Tuesday, October 20, 2026 (2026-10-20) at 06:00 PM (18:00) (hvac)', system)

    def test_an_appointment_move_is_returned_not_written(self):
        before = self.fresh()
        out = self.text('Homeowner asked to move it to Thursday at 6').json()
        self.assertEqual(out['appointment_move'], {'date': '2026-10-22', 'time': '18:00'})
        self.assertEqual(out['set'], {})
        self.assertNotIn('follow_up_date', out['set'])
        self.assertIsNone(out['next_question'])
        self.assertNothingWritten(before)
        self.fired.assert_not_called()
        # the ruling (l): nothing at all — not even the text's call log — until the rep confirms
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 0)
        self.assertTrue(out['pending']['token'])

    def test_a_move_with_no_time_asks_for_one(self):
        self.model.fixed = {'understood': True, 'appointment_moved_to_date': '2026-10-22'}
        out = self.text('he wants to move it to Thursday').json()
        self.assertIsNone(out['appointment_move'])
        self.assertEqual(out['next_question'], 'What day and time does the homeowner want instead?')

    def test_a_follow_up_date_with_no_time_asks_what_time(self):
        self.model.fixed = {'understood': True, 'disposition': 'follow_up', 'sat': True,
                            'call_notes': 'Wants to think', 'follow_up_date': '2026-10-23'}
        out = self.text('follow up friday').json()
        self.assertEqual(out['next_question'], 'And what time on Friday, October 23?')
        self.assertEqual(out['still_empty'][0], 'follow_up_time')

    def test_a_follow_up_with_no_date_asks_when(self):
        self.model.fixed = {'understood': True, 'disposition': 'cpfu', 'sat': True, 'call_notes': 'Credit passed, not signed'}
        out = self.text('credit passed but they did not sign').json()
        self.assertEqual(out['next_question'], 'When would be a good time to follow up with the homeowner?')


class NeverGuessTests(AlfredTextBase):
    def test_an_unreadable_reply_writes_nothing_and_asks(self):
        before = self.fresh()
        out = self.text('asdf ok ??').json()
        self.assertFalse(out['understood'])
        self.assertEqual(out['set'], {})
        self.assertEqual(out['next_question'], "Sorry, didn't catch that. Did you sit the appointment?")
        self.assertNothingWritten(before)
        self.fired.assert_not_called()
        log = VoiceCallLog.all_objects.get()
        self.assertIn("Alfred: Sorry, didn't catch that.", log.transcript)

    def test_an_empty_answer_is_unreadable(self):
        self.model.fixed = {'understood': True}
        out = self.text('k').json()
        self.assertFalse(out['understood'])
        self.assertEqual(out['next_question'],
                         "Sorry, I couldn't make that out — how did the appointment with Vera go?")

    def test_fields_the_model_got_wrong_are_not_written(self):
        self.model.fixed = {'understood': True, 'disposition': 'won', 'sat': 'yes', 'follow_up_date': '2026-10-01',
                            'follow_up_time': '25:99', 'monthly_cost': '$189', 'appointment_moved_to_date': 'Thursday'}
        out = self.text('won it, 189 a month, follow up the 1st').json()
        self.assertEqual(out['set'], {'monthly_cost': '$189'})
        self.assertEqual(out['next_question'], 'Did you sit the appointment?')
        self.assertIsNone(out['appointment_move'])
        lead = self.fresh()
        self.assertEqual((lead.disposition, lead.sat, lead.follow_up_date, lead.follow_up_time),
                         ('', None, None, None))

    def test_call_note_is_kept_under_20_words(self):
        self.model.fixed = {'understood': True, 'disposition': 'no_sale', 'sat': True,
                            'call_notes': ' '.join(['word'] * 40)}
        out = self.text('long story').json()
        self.assertEqual(len(out['set']['call_notes'].split()), 19)     # under 20

    def test_details_with_no_outcome_ask_for_the_outcome(self):
        self.model.fixed = {'understood': True, 'call_notes': 'Nice couple'}
        self.assertEqual(self.text('nice couple').json()['next_question'], 'Did you sit the appointment?')
        self.model.fixed = {'understood': True, 'sat': True}
        self.assertEqual(self.text('sat with them').json()['next_question'], 'Did you run credit?')
        self.model.fixed = {'understood': True, 'declined_fields': ['adders']}
        self.assertEqual(self.text('no adders').json()['next_question'], 'Did you run credit?')   # sat is on the lead now

    def test_a_rep_who_did_not_sit_is_asked_what_happened(self):
        self.model.fixed = {'understood': True, 'sat': False}
        self.assertEqual(self.text('no').json()['next_question'],
                         'Was the homeowner not home, or did they cancel at the door?')

    def test_half_an_emoji_is_not_an_error(self):
        r = self.text('Sold it! \ud83d')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIn('Val Rep: Sold it! ?', self.fresh().call_transcript)

    def test_a_disposition_that_is_not_a_string(self):
        self.model.fixed = {'understood': True, 'disposition': ['sale'], 'sat': True}
        r = self.text('sold')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['next_question'], 'Did you sit the appointment?')
        self.assertEqual(self.fresh().disposition, '')

    def test_nul_characters_are_dropped(self):
        self.model.fixed = {'understood': True, 'disposition': 'no_sale', 'sat': True, 'call_notes': 'Dead\x00 deal'}
        r = self.text('dead\x00 deal')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.fresh().call_notes, 'Dead deal')
        self.assertNotIn('\x00', self.fresh().call_transcript)

    def test_the_tool_never_asks_the_model_to_fill_a_date(self):
        props = alfred_text.text_tool()['function']['parameters']['properties']
        for field in ('follow_up_date', 'follow_up_time'):
            self.assertNotIn('Required', props[field]['description'])
            self.assertIn('never fill one in', props[field]['description'])

    def test_the_model_sees_what_is_on_the_lead(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(disposition='sale', sat=True, call_notes='Sold',
                                                        adders='Battery', monthly_cost='$189')
        self.model.fixed = {'understood': True, 'adders': 'Battery, panel upgrade'}
        out = self.text('also a panel upgrade').json()
        self.assertEqual(out['set'], {'adders': 'Battery, panel upgrade'})
        self.assertIn('On the lead now: call note "Sold"; sat yes; follow-up empty; adders "Battery"; '
                      'monthly "$189"; total empty; post-appointment notes empty',
                      self.model.calls[0]['messages'][0]['content'])
        self.assertEqual(self.fresh().call_notes, 'Sold')

    def test_two_texts_at_once_both_keep_their_transcript(self):
        Lead.all_objects.filter(pk=self.lead.pk).update(call_transcript='Earlier call')
        # Another text on this lead lands while the model is reading this one.
        self.model.side_effect = lambda: Lead.all_objects.filter(pk=self.lead.pk).update(
            call_transcript='Earlier call\n\n[Text] the other text')
        self.text('Sold it!')
        transcript = self.fresh().call_transcript
        self.assertTrue(transcript.startswith('Earlier call\n\n[Text] the other text\n\n[Text 10/20 09:00 PM] Val Rep: Sold it!'),
                        transcript)

    def test_a_failed_model_call_saves_nothing(self):
        self.model.raises = RuntimeError('timeout')
        before = self.fresh()
        r = self.text('Sold it!')
        self.assertEqual(r.status_code, 502)
        self.assertNothingWritten(before)
        self.assertFalse(VoiceCallLog.all_objects.exists())

    @override_settings(OPENAI_API_KEY='')
    def test_no_openai_key(self):
        r = self.text('Sold it!')
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.model.calls, [])

    def test_a_lead_that_changes_hands_mid_read_is_not_written(self):
        self.model.side_effect = lambda: Lead.all_objects.filter(pk=self.lead.pk).update(rep=self.v_rep2)
        r = self.text('Sold it!')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.fresh().disposition, '')
        self.assertFalse(VoiceCallLog.all_objects.exists())


class OrgLockTests(AlfredTextBase):
    def assertRefusedUntouched(self, r, status=403):
        self.assertEqual(r.status_code, status, r.content)
        self.assertEqual(self.model.calls, [])
        self.assertFalse(VoiceCallLog.all_objects.exists())
        self.assertEqual(self.fresh().disposition, '')
        self.assertEqual(self.fresh(self.s_lead).disposition, '')
        self.fired.assert_not_called()

    def test_a_team_sunshine_key_is_refused(self):
        r = self.text('Sold it!', tenant=self.s_tenant)
        self.assertRefusedUntouched(r)
        self.assertEqual(r.json(), {'error': 'Alfred by text is only for Ventana; this key is not a Ventana key.'})
        # …even for Team Sunshine's own rep and lead.
        r = self.text('Sold it!', tenant=self.s_tenant, rep_id=self.s_rep.id, lead_id=self.s_lead.id)
        self.assertRefusedUntouched(r)
        self.assertFalse(APIRateWindow.objects.exists())

    def test_a_key_with_no_org_is_refused(self):
        # api_key_required would run this key as the default inbound org
        # (Team Sunshine); the endpoint refuses it instead.
        r = self.text('Sold it!', tenant=self.legacy_tenant, rep_id=self.s_rep.id, lead_id=self.s_lead.id)
        self.assertRefusedUntouched(r)
        self.assertFalse(APIRateWindow.objects.exists())

    def test_no_key_or_a_dead_key(self):
        r = Client().post(URL, data='{}', content_type='application/json')
        self.assertEqual(r.status_code, 401)
        APITenant.objects.filter(pk=self.v_tenant.pk).update(is_active=False)
        self.assertRefusedUntouched(self.text('Sold it!'), status=401)

    def test_team_sunshine_rows_are_invisible_to_a_ventana_key(self):
        self.assertRefusedUntouched(self.text('Sold it!', lead_id=self.s_lead.id), status=404)
        self.assertRefusedUntouched(self.text('Sold it!', rep_id=self.s_rep.id), status=404)
        self.assertRefusedUntouched(self.text('Sold it!', rep_id=None, rep_phone='6175550002'), status=404)

    def test_another_reps_lead_is_refused(self):
        self.assertRefusedUntouched(self.text('Sold it!', lead_id=self.others_lead.id))

    def test_the_rep_by_phone(self):
        r = self.text('Sold it!', rep_id=None, rep_phone='+1 617-555-0001')
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(VoiceCallLog.all_objects.get().rep, self.v_rep)
        r = self.text('Sold it!', rep_phone='6175550003')
        self.assertEqual(r.status_code, 400)

    def test_an_inactive_rep_is_refused(self):
        Rep.all_objects.filter(pk=self.v_rep.pk).update(is_active=False)
        self.assertRefusedUntouched(self.text('Sold it!'), status=404)

    def test_bad_bodies(self):
        key = self.v_tenant.api_key
        for body in ('not json', '[]', json.dumps({'rep_id': self.v_rep.id, 'text': 'x'}),
                     json.dumps({'lead_id': self.lead.id, 'text': 'x'}),
                     json.dumps({'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'text': '  '}),
                     json.dumps({'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'text': 'x' * 1601}),
                     json.dumps({'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'text': 'x', 'context': []}),
                     json.dumps({'rep_id': self.v_rep.id, 'lead_id': '²', 'text': 'x'}),
                     json.dumps({'rep_id': '٣', 'lead_id': self.lead.id, 'text': 'x'}),
                     json.dumps({'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'text': '\x00'})):
            r = Client().post(URL, data=body, content_type='application/json', HTTP_AUTHORIZATION=f'Bearer {key}')
            self.assertEqual(r.status_code, 400, body[:60])
        self.assertEqual(self.model.calls, [])
        r = Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {key}')
        self.assertEqual(r.status_code, 405)


class RateLimitTests(AlfredTextBase):
    @mock.patch('maps.alfred_text.TEXTS_PER_HOUR', 2)
    def test_per_key(self):
        self.assertEqual(self.text('Sold it!').status_code, 200)
        self.assertEqual(self.text('Sold it!').status_code, 200)
        r = self.text('Sold it!')
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r['Retry-After'], '3601')     # 9:00 PM → the next hour
        self.assertEqual(len(self.model.calls), 2)
        # Another key has its own count.
        self.assertEqual(self.text('Sold it!', tenant=self.v_tenant2).status_code, 200)
        # The next hour opens a new window.
        self.now.return_value = NOW + dt.timedelta(hours=1)
        self.assertEqual(self.text('Sold it!').status_code, 200)

    def test_a_keys_own_lower_rate_limit_applies(self):
        APITenant.objects.filter(pk=self.v_tenant.pk).update(rate_limit=1)
        self.assertEqual(self.text('Sold it!').status_code, 200)
        self.assertEqual(self.text('Sold it!').status_code, 429)

    def test_bad_requests_count_too(self):
        APITenant.objects.filter(pk=self.v_tenant.pk).update(rate_limit=1)
        self.assertEqual(self.text('Sold it!', lead_id=999999).status_code, 404)
        self.assertEqual(self.text('Sold it!').status_code, 429)


class SameAsTheCallTests(AlfredTextBase):
    def test_the_calls_instructions_and_fields(self):
        from voice_ws import DISPOSITION_TOOL, SYSTEM_PROMPT
        self.text('Sold it!')
        messages, tool = self.model.calls[0]['messages'], self.model.calls[0]['tool']
        self.assertTrue(messages[0]['content'].startswith(SYSTEM_PROMPT))
        self.assertEqual(messages[1], {'role': 'user', 'content': 'Sold it!'})
        props = tool['function']['parameters']['properties']
        call_fields = set(DISPOSITION_TOOL['parameters']['properties']) - {'homeowner_name'}
        self.assertLessEqual(call_fields, set(props))
        self.assertEqual(props['disposition']['enum'], DISPOSITION_TOOL['parameters']['properties']['disposition']['enum'])
        self.assertEqual(len(props['disposition']['enum']), 10)
        # The follow-up time and the appointment move are different fields.
        self.assertIn('follow_up_time', props)
        self.assertIn('appointment_moved_to_time', props)
        # Alfred's questions are the call's own words.
        for question in alfred_text.QUESTIONS.values():
            self.assertIn(question, SYSTEM_PROMPT)
        self.assertIn(alfred_text.ASK_OUTCOME, SYSTEM_PROMPT)

    def test_voice_logs_stay_voice(self):
        log = VoiceCallLog.all_objects.create(rep=self.v_rep, caller_number='6175550001', organization=self.ventana)
        self.assertEqual(VoiceCallLog.all_objects.get(pk=log.pk).channel, 'voice')

    def test_old_code_can_still_log_a_call(self):
        # Mid-deploy, the old container's INSERT names no channel column; the
        # database default fills it.
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO maps_voicecalllog (caller_number, twilio_call_sid, transcript, summary, created_at, '
                'organization_id) VALUES (%s, %s, %s, %s, %s, %s)',
                ['6175550001', 'CA1', '', '', NOW, self.ventana.id])
        self.assertEqual(VoiceCallLog.all_objects.get(twilio_call_sid='CA1').channel, 'voice')


class RealCompletionTests(TestCase):
    """The one function the stand-in replaces, with OpenAI's client faked."""

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_one_forced_tool_call(self):
        tool = alfred_text.text_tool()
        fake = mock.MagicMock()
        fake.return_value.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(
            tool_calls=[mock.Mock(function=mock.Mock(arguments='{"understood": true, "disposition": "sale"}'))]))]
        with mock.patch('openai.OpenAI', fake):
            args = alfred_text._complete([{'role': 'user', 'content': 'Sold it!'}], tool)
        self.assertEqual(args, {'understood': True, 'disposition': 'sale'})
        fake.assert_called_once_with(api_key='sk-test', timeout=20, max_retries=0)  # the ruling (n): no client retry
        kwargs = fake.return_value.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs['model'], 'gpt-4o-mini')
        self.assertEqual(kwargs['tools'], [tool])
        self.assertEqual(kwargs['tool_choice'], {'type': 'function', 'function': {'name': 'read_rep_text'}})

    @override_settings(OPENAI_API_KEY='sk-test')
    def test_no_tool_call_reads_as_nothing(self):
        fake = mock.MagicMock()
        fake.return_value.chat.completions.create.return_value.choices = [mock.Mock(message=mock.Mock(tool_calls=None))]
        with mock.patch('openai.OpenAI', fake):
            self.assertEqual(alfred_text._complete([], alfred_text.text_tool()), {})


class WebhookTests(AlfredTextBase):
    """The org's disposition webhooks fire, through the real fire_webhooks."""
    stub_webhooks = False

    def test_only_ventanas_configs_fire(self):
        WebhookConfig.objects.create(name='Ventana dispo', trigger='disposition_changed', organization=self.ventana,
                                     url='https://hub.example.invalid/ventana', fields=['id', 'disposition', 'call_notes'])
        WebhookConfig.objects.create(name='TS dispo', trigger='disposition_changed', organization=self.sunshine,
                                     url='https://hub.example.invalid/sunshine', fields=['id', 'disposition'])
        timers = _ImmediateTimers()
        with mock.patch('maps.views.threading.Timer', timers), \
                mock.patch('maps.views.urllib.request.urlopen') as urlopen:
            urlopen.return_value.status = 200
            urlopen.return_value.read.return_value = b'ok'
            self.assertEqual(self.text('Sold it!').status_code, 200)
            timers.run()
        urls = [c.args[0].full_url for c in urlopen.call_args_list]
        self.assertEqual(urls, ['https://hub.example.invalid/ventana'])
        self.assertEqual(json.loads(urlopen.call_args.args[0].data),
                         {'id': str(self.lead.id), 'disposition': 'Sale',
                          'call_notes': 'Sold, credit passed and all contracts signed'})
        self.assertEqual(GHLWebhookLog.objects.filter(webhook_type='disposition_changed').count(), 1)


class PendingMoveTests(AlfredTextBase):
    """The owner's ruling (l), 2026-10-25: a text whose answer contains an
    appointment move writes nothing; the whole exchange waits in a signed
    token, applied exactly once when the rep confirms the move."""

    MOVE = {'understood': True, 'disposition': 'cancel_door', 'sat': False,
            'call_notes': 'Did not sit, wants another day', 'appointment_moved_to_date': '2026-10-22',
            'appointment_moved_to_time': '18:00'}

    def confirm(self, token, **body):
        body = {'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'confirm_move': token, **body}
        return Client().post(URL, data=json.dumps(body), content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {self.v_tenant.api_key}')

    def pending(self):
        self.model.fixed = dict(self.MOVE)
        return self.text("didn't sit, they cancelled, wants Thursday at 6").json()

    def test_a_move_with_an_outcome_writes_nothing_and_returns_the_whole_exchange_pending(self):
        before = self.fresh()
        out = self.pending()
        self.assertEqual(out['set'], {})
        self.assertEqual(out['appointment_move'], {'date': '2026-10-22', 'time': '18:00'})
        self.assertEqual(out['pending']['set'], {'disposition': 'cancel_door', 'sat': False,
                                                 'call_notes': 'Did not sit, wants another day'})
        self.assertEqual(out['pending']['expires_at'], (NOW + dt.timedelta(hours=72)).isoformat())
        self.assertNothingWritten(before)
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 0)
        self.fired.assert_not_called()

    def test_the_confirmation_applies_the_exchange_once(self):
        before = self.fresh()
        token = self.pending()['pending']['token']
        out = self.confirm(token).json()
        self.assertEqual((out['confirmed'], out['already_applied']), (True, False))
        self.assertEqual(out['set'], {'disposition': 'cancel_door', 'sat': False, 'call_notes': 'Did not sit, wants another day'})
        self.assertEqual(out['appointment_move'], {'date': '2026-10-22', 'time': '18:00'})
        lead = self.fresh()
        self.assertEqual((lead.disposition, lead.sat, lead.call_notes), ('cancel_door', False, 'Did not sit, wants another day'))
        self.assertIn("Val Rep: didn't sit, they cancelled, wants Thursday at 6", lead.call_transcript)
        self.assertEqual(lead.appointment_datetime, before.appointment_datetime)  # a move is never written
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 1)
        self.fired.assert_called_once()
        # sent again: nothing more
        again = self.confirm(token).json()
        self.assertEqual((again['already_applied'], again['set']), (True, {}))
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 1)
        self.assertEqual(self.fresh().call_transcript.count("wants Thursday at 6"), 1)
        self.fired.assert_called_once()

    def test_a_move_alone_confirms_its_exchange_into_the_call_log(self):
        self.model.fixed = {'understood': True, 'appointment_moved_to_date': '2026-10-22', 'appointment_moved_to_time': '18:00'}
        token = self.text('Homeowner asked to move it to Thursday at 6').json()['pending']['token']
        out = self.confirm(token).json()
        self.assertEqual((out['set'], out['confirmed']), ({}, True))
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 1)
        self.assertEqual(self.confirm(token).json()['already_applied'], True)

    def test_a_token_from_elsewhere_applies_nothing(self):
        before = self.fresh()
        self.assertEqual(self.confirm('not-a-token').status_code, 400)
        forged = signing.dumps({'v': 1, 'lead': self.lead.id, 'rep': self.v_rep.id, 'writes': {'disposition': 'sale'},
                                'exchange': 'x', 'summary': 'x', 'move': None}, salt='another.salt', compress=True)
        self.assertEqual(self.confirm(forged).status_code, 400)
        token = self.pending()['pending']['token']
        self.assertEqual(self.confirm(token, rep_id=self.v_rep2.id).status_code, 409)  # another rep's confirmation
        self.assertNothingWritten(before)

    def test_a_token_for_another_lead_or_rep_is_refused(self):
        token = self.pending()['pending']['token']
        Lead.all_objects.filter(id=self.others_lead.id).update(rep=None)
        res = self.confirm(token, lead_id=self.others_lead.id)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 0)

    def test_an_expired_token_is_refused(self):
        token = self.pending()['pending']['token']
        with mock.patch('maps.alfred_text.PENDING_MAX_AGE', -1):
            res = self.confirm(token)
        self.assertEqual(res.status_code, 410)
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 0)

    def test_a_tampered_payload_is_refused(self):
        token = signing.dumps({'v': 1, 'lead': self.lead.id, 'rep': self.v_rep.id, 'writes': {'appointment_datetime': 'x'},
                               'exchange': 'x', 'summary': 'x', 'move': None},
                              salt=alfred_text.PENDING_SALT, compress=True)
        self.assertEqual(self.confirm(token).status_code, 400)  # a field a text cannot write

    # ── the review of (l): the token is checked as if untrusted ──

    def sign(self, **over):
        payload = {'v': 2, 'pid': '0123456789abcdef', 'lead': self.lead.id, 'rep': self.v_rep.id,
                   'issued': NOW.isoformat(), 'writes': {}, 'exchange': 'x', 'summary': 'x',
                   'move': {'date': '2026-10-22', 'time': '18:00'}, 'next_question': None, 'still_empty': [], **over}
        return signing.dumps(payload, key=alfred_text._pending_key(), salt=alfred_text.PENDING_SALT, compress=True)

    def test_a_token_signed_with_secret_key_is_refused(self):
        payload = {'v': 2, 'pid': '0123456789abcdef', 'lead': self.lead.id, 'rep': self.v_rep.id, 'issued': NOW.isoformat(),
                   'writes': {'disposition': 'sale'}, 'exchange': 'x', 'summary': 'x',
                   'move': {'date': '2026-10-22', 'time': '18:00'}, 'next_question': None, 'still_empty': []}
        forged = signing.dumps(payload, salt=alfred_text.PENDING_SALT, compress=True)   # SECRET_KEY, the right salt
        self.assertEqual(self.confirm(forged).status_code, 400)
        self.assertEqual(self.fresh().disposition, '')

    def test_bad_values_under_the_real_key_are_refused(self):
        before = self.fresh()
        for over in ({'writes': {'disposition': 'zzz'}}, {'writes': {'call_notes': 'x' * 201}},
                     {'writes': {'sat': 'yes'}}, {'writes': {'follow_up_date': '2026-13-40'}},
                     {'writes': {'follow_up_time': '6pm'}}, {'writes': {'adders': 7}}, {'writes': {'appointment_datetime': 'x'}},
                     {'v': 1}, {'pid': 'nope'}, {'move': {'date': '2026-10-22'}}, {'still_empty': ['not_a_field']},
                     {'exchange': ''}, {'summary': 'x' * 301}, {'issued': 'yesterday'}):
            self.assertEqual(self.confirm(self.sign(**over)).status_code, 400, over)
        self.assertNothingWritten(before)
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 0)

    def test_text_with_a_confirmation_is_refused(self):
        token = self.pending()['pending']['token']
        self.assertEqual(self.confirm(token, text='and it went fine').status_code, 400)
        self.assertEqual(self.confirm('x' * (alfred_text.PENDING_TOKEN_MAX + 1)).status_code, 400)
        self.assertEqual(self.confirm(123).status_code, 400)

    def test_a_newer_text_since_the_move_refuses_the_confirmation(self):
        token = self.pending()['pending']['token']
        self.model.fixed = {'understood': True, 'disposition': 'sale', 'sat': True, 'call_notes': 'Sold after all'}
        self.text('sold it after all')
        res = self.confirm(token)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.fresh().disposition, 'sale')   # the newer answer stands

    def test_the_confirmation_hands_back_what_to_ask_next(self):
        self.model.fixed = {'understood': True, 'disposition': 'follow_up', 'sat': True, 'call_notes': 'Wants to think',
                            'appointment_moved_to_date': '2026-10-22', 'appointment_moved_to_time': '18:00'}
        out = self.text('follow up, and move it to thursday at 6').json()
        confirmed = self.confirm(out['pending']['token']).json()
        self.assertEqual((confirmed['next_question'], confirmed['still_empty']), (out['next_question'], out['still_empty']))
        self.assertTrue(confirmed['still_empty'])

    def test_a_confirmation_counts_against_the_rate_limit_and_never_calls_the_model(self):
        token = self.pending()['pending']['token']
        calls = len(self.model.calls)
        with mock.patch('maps.alfred_text.TEXTS_PER_HOUR', 1):
            self.assertEqual(self.confirm(token).status_code, 429)
        self.assertEqual(len(self.model.calls), calls)
        with override_settings(OPENAI_API_KEY='', ALFRED_PENDING_KEY='own-key'):
            own = self.sign(writes={'sat': False})
            self.assertEqual(self.confirm(own).status_code, 200)   # no model, no OpenAI key needed with its own key
        with override_settings(OPENAI_API_KEY='', ALFRED_PENDING_KEY=''):
            self.assertEqual(self.confirm(own).status_code, 503)
        self.assertEqual(len(self.model.calls), calls)

    def test_a_lead_that_changed_hands_before_the_confirmation_is_refused(self):
        token = self.pending()['pending']['token']
        Lead.all_objects.filter(id=self.lead.id).update(rep=self.v_rep2)
        self.assertEqual(self.confirm(token).status_code, 403)   # _find_lead: now another rep's
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 0)

    def test_a_replay_after_a_newer_text_still_reads_already_applied(self):
        token = self.pending()['pending']['token']
        self.assertEqual(self.confirm(token).json()['already_applied'], False)
        self.model.fixed = {'understood': True, 'post_appt_notes': 'Called back later'}
        self.text('called back later')
        self.assertEqual(self.confirm(token).json()['already_applied'], True)   # not 409: it was applied

    def test_secret_key_fallbacks_never_sign_a_confirmation(self):
        payload = {'v': 2, 'pid': '0123456789abcdef', 'lead': self.lead.id, 'rep': self.v_rep.id, 'issued': NOW.isoformat(),
                   'writes': {'disposition': 'sale'}, 'exchange': 'x', 'summary': 'x',
                   'move': {'date': '2026-10-22', 'time': '18:00'}, 'next_question': None, 'still_empty': []}
        old = 'an-old-public-key'
        forged = signing.dumps(payload, key=old, salt=alfred_text.PENDING_SALT, compress=True)
        with override_settings(SECRET_KEY_FALLBACKS=[old]):
            self.assertEqual(self.confirm(forged).status_code, 400)
        self.assertEqual(self.fresh().disposition, '')


class StandInComposer:
    """Stands in for gpt-4o-mini in compose mode. Records every call."""

    def __init__(self):
        self.calls, self.reply, self.raises = [], 'Hey Val, how did it go with Sam?', None

    def __call__(self, messages):
        self.calls.append(messages)
        if self.raises:
            raise self.raises
        return self.reply


CUSTOMER = {'name': 'Sam Example', 'product': 'HVAC', 'phone': '617-555-0101', 'quote': '$189/mo, $9,800 total',
            'summary': 'Needs to talk to wife', 'appointment': 'Tue Oct 20 6:00 PM', 'follow_up': 'Thu Oct 22 2:00 PM'}
OWNERS_INSTRUCTIONS = ("You're texting a coworker. 1–2 sentences, casual, first names, one question at a time, ask "
                       "only for what's missing, at most 3 questions per update then wrap up. Never use the words "
                       "stop, cancel, end, quit, unsubscribe. Never mention another rep's customer.")
REAL_COMPOSE = alfred_text._complete_compose      # before any test replaces it


class ComposeTests(AlfredTextBase):
    """Compose mode (2026-10-27): Alfred writes the rep loop's next text. Same
    endpoint, lock, key and hourly count; no lead, no write."""

    def setUp(self):
        super().setUp()
        self.composer = StandInComposer()
        self.start(mock.patch('maps.alfred_text._complete_compose', self.composer))

    def post(self, payload, tenant=None):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        return Client().post(URL, data=data, content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {(tenant or self.v_tenant).api_key}')

    def compose(self, tenant=None, body=None, **fields):
        compose = {'objective': 'first_nudge', 'rep_first_name': 'Val', 'customers': [dict(CUSTOMER)], **fields}
        return self.post({'rep_id': self.v_rep.id, 'compose': compose, **(body or {})}, tenant)

    def system(self, call=-1):
        return self.composer.calls[call][0]['content']

    def test_a_composed_text(self):
        r = self.compose()
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {'message': 'Hey Val, how did it go with Sam?', 'objective': 'first_nudge'})
        self.assertEqual(len(self.composer.calls), 1)
        self.assertEqual(self.model.calls, [])          # never the text reader

    # ── the lock, the hour, the rep ──

    def test_only_ventana_keys(self):
        for tenant, body in ((self.s_tenant, None), (self.s_tenant, {'rep_id': self.s_rep.id}),
                             (self.legacy_tenant, {'rep_id': self.s_rep.id})):
            r = self.compose(tenant=tenant, body=body)
            self.assertEqual(r.status_code, 403, r.content)
            self.assertEqual(r.json(), {'error': 'Alfred by text is only for Ventana; this key is not a Ventana key.'})
        self.assertFalse(APIRateWindow.objects.exists())      # refused before the hour is counted
        no_key = Client().post(URL, data=json.dumps({'rep_id': self.v_rep.id, 'compose': {}}),
                               content_type='application/json')
        self.assertEqual(no_key.status_code, 401)
        self.assertEqual(Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {self.v_tenant.api_key}').status_code, 405)
        self.assertEqual(self.composer.calls, [])

    def test_a_compose_is_one_count_in_the_texts_hour(self):
        APITenant.objects.filter(pk=self.v_tenant.pk).update(rate_limit=1)
        self.assertEqual(self.compose().status_code, 200)
        r = self.text('Sold it!')
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r['Retry-After'], '3601')
        self.assertEqual(self.model.calls, [])
        # …and the other way round, in the next hour.
        self.now.return_value = NOW + dt.timedelta(hours=1)
        self.assertEqual(self.text('Sold it!').status_code, 200)
        r = self.compose()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()['error'], 'Too many texts on this key this hour.')
        self.assertEqual(len(self.composer.calls), 1)
        self.assertEqual(list(APIRateWindow.objects.filter(tenant=self.v_tenant).values_list('scope', 'count')),
                         [('alfred_text', 1), ('alfred_text', 1)])

    @mock.patch('maps.alfred_text.TEXTS_PER_HOUR', 3)
    def test_compose_and_texts_share_the_hours_limit(self):
        self.assertEqual(self.compose().status_code, 200)
        self.assertEqual(self.text('Sold it!').status_code, 200)
        self.assertEqual(self.compose(objective='bogus').status_code, 400)     # a bad compose counts too
        self.assertEqual(self.compose().status_code, 429)
        self.assertEqual(self.text('Sold it!').status_code, 429)
        self.assertEqual(APIRateWindow.objects.get(tenant=self.v_tenant).count, 3)
        self.assertEqual(self.compose(tenant=self.v_tenant2).status_code, 200)  # another key, its own count

    def test_the_rep_as_today(self):
        self.assertEqual(self.compose(body={'rep_id': None, 'rep_phone': '+1 617-555-0001'}).status_code, 200)
        self.assertEqual(self.compose(body={'rep_phone': '617-555-0001'}).status_code, 200)    # both, agreeing
        r = self.compose(body={'rep_phone': '6175550003'})
        self.assertEqual((r.status_code, r.json()['error']), (400, "rep_id and rep_phone don't name the same rep."))
        for body in ({'rep_id': 999999}, {'rep_id': self.s_rep.id}, {'rep_id': None, 'rep_phone': '6175550002'},
                     {'rep_id': None, 'rep_phone': '6175559999'}):
            self.assertEqual(self.compose(body=body).status_code, 404, body)            # Team Sunshine's are invisible
        Rep.all_objects.filter(pk=self.v_rep2.pk).update(is_active=False)
        self.assertEqual(self.compose(body={'rep_id': self.v_rep2.id}).status_code, 404)
        Rep.all_objects.create(name='Twin Rep', phone_number='617-555-0001', home_address='4 D St',
                               organization=self.ventana)
        r = self.compose(body={'rep_id': None, 'rep_phone': '6175550001'})
        self.assertEqual((r.status_code, r.json()['error']),
                         (409, 'More than one active Ventana rep has that phone number; send rep_id.'))
        self.assertEqual(self.compose().status_code, 200)        # by rep_id it is still one rep
        self.assertEqual(len(self.composer.calls), 3)

    # ── the body ──

    @mock.patch('maps.alfred_text.TEXTS_PER_HOUR', 500)
    def test_every_bad_body_is_a_400(self):
        good = {'objective': 'first_nudge', 'rep_first_name': 'Val', 'customers': [dict(CUSTOMER)]}
        rep = {'rep_id': self.v_rep.id}

        def body(**over):
            return {**rep, 'compose': {**good, **over}}

        def customer(**over):
            return body(customers=[{**CUSTOMER, **over}])

        def said(**over):
            return body(history=[{'from': 'rep', 'text': 'went well', 'at': '2026-10-20T21:00:00Z', **over}])

        bad = [
            'not json', '[]', {**rep, 'compose': 'first_nudge'}, {**rep, 'compose': []}, {**rep, 'compose': {}},
            {**rep, 'compose': None},                                       # no compose: a text, without its lead_id
            {**body(), 'text': 'went well'}, {**body(), 'context': {}}, {**body(), 'confirm_move': 'token'},
            {'compose': good}, {'rep_id': 'abc', 'compose': good}, {'rep_phone': 'no digits', 'compose': good},
            body(objective=None), body(objective='nudge'), body(objective=['first_nudge']),
            body(rep_first_name=None), body(rep_first_name='  '), body(rep_first_name='x' * 41), body(rep_first_name=7),
            body(customers=None), body(customers=[]), body(customers=dict(CUSTOMER)), body(customers=['Sam Example']),
            body(customers=[CUSTOMER, CUSTOMER]),                           # two, but not which_customer
            body(objective='which_customer', customers=[CUSTOMER] * 9),
            customer(name=None), customer(name=' '), customer(name='x' * 81), customer(product='x' * 201),
            customer(phone=6175550101), customer(quote=['$189/mo']), customer(follow_up='x' * 201),
            body(ask=7), body(ask='x' * 301),
            body(history={}), body(history=['went well']), body(history=[{'from': 'rep', 'text': 'x'}] * 13),
            said(**{'from': 'customer'}), said(**{'from': None}), said(text=None), said(text='x' * 601), said(at=1),
        ]
        for payload in bad:
            r = self.post(payload)
            self.assertEqual(r.status_code, 400, str(payload)[:120])
        self.assertEqual(self.composer.calls, [])
        # Each limit is inclusive; every customer field but name may be null or left out.
        fine = [
            body(rep_first_name='x' * 40), customer(name='x' * 80, summary='x' * 200, product=None, phone=None),
            body(customers=[{'name': 'Sam Example'}]), body(ask='x' * 300), body(ask=None), body(history=None),
            body(history=[{'from': 'alfred', 'text': 'x' * 600}] * 12), said(at=None), said(at='not a time'),
            said(at='0001-01-01T00:00:00+23:59'), said(text=''),
            body(objective='which_customer', customers=[CUSTOMER] * 8),
            {**body(), 'text': None, 'context': None, 'confirm_move': None},
        ]
        for payload in fine:
            r = self.post(payload)
            self.assertEqual(r.status_code, 200, str(payload)[:120])

    def test_which_customer_takes_several_and_the_others_exactly_one(self):
        two = [dict(CUSTOMER), {**CUSTOMER, 'name': 'Dana Sample', 'phone': '617-555-0102'}]
        for objective in alfred_text.OBJECTIVES:
            r = self.compose(objective=objective, customers=two)
            if objective == 'which_customer':
                self.assertEqual(r.status_code, 200, r.content)
                self.assertIn('Customers:\n- Sam Example (product HVAC; phone 617-555-0101;', self.system())
                self.assertIn('\n- Dana Sample (product HVAC; phone 617-555-0102;', self.system())
            else:
                self.assertEqual(r.json(), {'error': 'compose.customers must hold exactly one customer, '
                                                     'except for which_customer.'}, objective)
            self.assertEqual(self.compose(objective=objective).status_code, 200, objective)
        self.assertEqual(len(self.composer.calls), len(alfred_text.OBJECTIVES) + 1)

    # ── no lead, no write ──

    def test_compose_never_needs_or_reads_a_lead(self):
        with mock.patch('maps.alfred_text._find_lead', side_effect=AssertionError('compose read a lead')):
            for body in ({}, {'lead_id': self.lead.id}, {'lead_id': self.s_lead.id}, {'lead_id': self.others_lead.id},
                         {'lead_id': 999999}, {'lead_id': 'not an id'}):
                self.assertEqual(self.compose(body=body).status_code, 200, body)
        Lead.all_objects.filter(pk=self.lead.pk).update(cancelled=True, appointment_datetime=None)
        self.assertEqual(self.compose(body={'lead_id': self.lead.id}).status_code, 200)
        for call in self.composer.calls:
            self.assertNotIn('Vera', call[0]['content'])        # nothing of any lead reaches the model

    def test_nothing_is_written(self):
        leads = list(Lead.all_objects.order_by('pk').values())
        for objective in alfred_text.OBJECTIVES:
            customers = [dict(CUSTOMER)] * (2 if objective == 'which_customer' else 1)
            r = self.compose(objective=objective, customers=customers, ask='Thu Oct 22 2:00 PM',
                             body={'lead_id': self.lead.id})
            self.assertEqual((r.status_code, r.json()['objective']), (200, objective))
        self.assertEqual(list(Lead.all_objects.order_by('pk').values()), leads)
        self.assertFalse(VoiceCallLog.all_objects.exists())
        self.assertFalse(GHLWebhookLog.objects.exists())
        self.fired.assert_not_called()
        self.assertEqual(APIRateWindow.objects.get(tenant=self.v_tenant).count, len(alfred_text.OBJECTIVES))

    # ── what the model is sent ──

    def test_what_the_model_is_sent(self):
        history = [{'from': 'alfred', 'text': 'Hey Val, how did it go with Sam?', 'at': '2026-10-20T22:30:00Z'},
                   {'from': 'rep', 'text': 'went well,\n needs to talk to wife, try Thursday 2',
                    'at': '2026-10-20T23:00:00+00:00'},
                   {'from': 'alfred', 'text': 'Got it. Any adders on this one?'}]
        self.compose(objective='deal_details', ask="What's the monthly looking like?", history=history)
        messages = self.composer.calls[0]
        self.assertEqual([m['role'] for m in messages], ['system', 'user'])
        system = messages[0]['content']
        self.assertIn(OWNERS_INSTRUCTIONS, system)                  # verbatim
        self.assertEqual(alfred_text.COMPOSE_RULES, OWNERS_INSTRUCTIONS)
        self.assertIn('Write the next text message to Val, a sales rep.', system)
        self.assertIn('no signature, no emoji, plain characters', system)
        self.assertIn('This text: ' + alfred_text.COMPOSE_GOALS['deal_details'], system)
        self.assertIn('Now: Tuesday, October 20, 9:00 PM Eastern', system)
        self.assertIn('Customer: Sam Example (product HVAC; phone 617-555-0101; quote $189/mo, $9,800 total; '
                      'appointment Tue Oct 20 6:00 PM; follow-up Thu Oct 22 2:00 PM; '
                      'last update: Needs to talk to wife)', system)
        self.assertIn("Get across: What's the monthly looking like?", system)
        self.assertIn('Recent texts with Val, oldest first:\n'
                      '[Tue Oct 20 6:30 PM] Alfred: Hey Val, how did it go with Sam?\n'
                      '[Tue Oct 20 7:00 PM] Val: went well, needs to talk to wife, try Thursday 2\n'
                      'Alfred: Got it. Any adders on this one?', system)
        # With no ask, no history and a bare customer: none of it is made up.
        self.compose(customers=[{'name': 'Sam Example', 'product': None}])
        system = self.system()
        self.assertIn('Customer: Sam Example\n', system)
        self.assertNotIn('Get across', system)
        self.assertTrue(system.endswith('Recent texts with Val: none'), system[-200:])

    def test_each_objective_gets_its_own_guidance(self):
        goals = alfred_text.COMPOSE_GOALS
        self.assertEqual(alfred_text.OBJECTIVES, ('first_nudge', 're_ask', 'reminder', 'deal_details', 'which_customer',
                                                  'move_confirmation', 'dead_reason', 'clarify', 'wrap_up'))
        self.assertEqual(set(goals), set(alfred_text.OBJECTIVES))
        for objective in alfred_text.OBJECTIVES:
            customers = [dict(CUSTOMER)] * (2 if objective == 'which_customer' else 1)
            self.assertEqual(self.compose(objective=objective, customers=customers).status_code, 200)
            system = self.system()
            self.assertIn(f'This text: {goals[objective]}', system)
            for other in set(alfred_text.OBJECTIVES) - {objective}:
                self.assertNotIn(goals[other], system)
        for objective in ('first_nudge', 're_ask', 'reminder'):
            self.assertIn('sold, dead, or a day and time to follow up', goals[objective])
        self.assertIn("phone and product, and the quote when given", goals['reminder'])
        for objective in ('deal_details', 'clarify'):
            self.assertIn('"Get across" line in your own words, as one question', goals[objective])
        self.assertIn('ONE short sentence asking which customer', goals['which_customer'])
        self.assertIn('both words "move" and "remind"', goals['move_confirmation'])
        self.assertIn('price, competitor, no decision, wrong fit, or other', goals['dead_reason'])
        self.assertIn('no question. A short thanks that confirms what was recorded', goals['wrap_up'])

    def test_the_prompt_is_not_the_calls(self):
        from voice_ws import SYSTEM_PROMPT
        self.compose()
        system = self.system()
        self.assertNotIn(SYSTEM_PROMPT, system)
        self.assertNotIn(alfred_text.TEXT_MODE.splitlines()[0], system)
        sentences = [s.strip() for s in re.split(r'[.!?\n]', SYSTEM_PROMPT) if len(s.strip()) >= 20]
        self.assertGreater(len(sentences), 50)
        for sentence in sentences:
            self.assertNotIn(sentence, system)
        for word in ('British', '60-year-old', 'read_rep_text', 'update_disposition', 'clarifying_question'):
            self.assertNotIn(word, system)

    # ── the answer ──

    def test_the_text_is_tidied(self):
        for reply, message in (
                ('  "Hey Val,\n\n  how did it go   with Sam?"  ', 'Hey Val, how did it go with Sam?'),
                ('“Hey Val, how’d it go with Sam?”', 'Hey Val, how’d it go with Sam?'),
                ("'Hey Val, how'd it go?'", "Hey Val, how'd it go?"),
                ('‘Hey Val’', 'Hey Val'),
                ('""Hey Val""', '"Hey Val"'),                       # one pair only
                ('"Hey Val, how did it go?', '"Hey Val, how did it go?'),      # not a pair
                ('"Hey Val\'', '"Hey Val\''),
                ('Did Sam say "yes"', 'Did Sam say "yes"'),
                ('Hey\x00 Val', 'Hey Val'),
                ('x' * 700, 'x' * 600)):
            self.composer.reply = reply
            r = self.compose()
            self.assertEqual((r.status_code, r.json()), (200, {'message': message, 'objective': 'first_nudge'}), reply)
        self.composer.reply = 'word ' * 200
        message = self.compose().json()['message']
        self.assertLessEqual(len(message), 600)
        self.assertEqual(message, message.strip())

    def test_a_failed_or_empty_model_answer_is_a_502(self):
        self.composer.raises = RuntimeError('timeout')
        r = self.compose()
        self.assertEqual((r.status_code, r.json()),
                         (502, {'error': "Alfred couldn't write that text just now; nothing was sent. Try again."}))
        self.composer.raises = None
        for reply in ('', '  \n ', '""', '“ ”', None, 42):
            self.composer.reply = reply
            self.assertEqual(self.compose().status_code, 502, repr(reply))
        self.assertFalse(VoiceCallLog.all_objects.exists())
        self.assertEqual(APIRateWindow.objects.get(tenant=self.v_tenant).count, 7)    # each one counted

    @override_settings(OPENAI_API_KEY='')
    def test_no_openai_key_is_a_503(self):
        r = self.compose()
        self.assertEqual((r.status_code, r.json()),
                         (503, {'error': 'Alfred is not configured (no OpenAI key); nothing was sent.'}))
        self.assertEqual(self.composer.calls, [])
        self.assertEqual(self.compose(body={'rep_id': 999999}).status_code, 404)      # the rep is checked first
        self.assertEqual(self.compose(objective='bogus').status_code, 400)

    def test_the_real_call_is_one_plain_completion(self):
        fake = mock.MagicMock()
        create = fake.return_value.chat.completions.create
        create.return_value.choices = [mock.Mock(message=mock.Mock(content=' "Hey Val, how did it go with Sam?" '))]
        with mock.patch('maps.alfred_text._complete_compose', REAL_COMPOSE), mock.patch('openai.OpenAI', fake):
            r = self.compose()
        self.assertEqual(r.json(), {'message': 'Hey Val, how did it go with Sam?', 'objective': 'first_nudge'})
        fake.assert_called_once_with(api_key='sk-test', timeout=20, max_retries=0)    # no client retry
        kwargs = create.call_args.kwargs
        self.assertEqual(set(kwargs), {'model', 'messages', 'temperature', 'max_tokens'})     # no tools
        self.assertEqual((kwargs['model'], kwargs['temperature'], kwargs['max_tokens']), ('gpt-4o-mini', 0.4, 160))
        self.assertIn(OWNERS_INSTRUCTIONS, kwargs['messages'][0]['content'])
        create.return_value.choices = [mock.Mock(message=mock.Mock(content=None))]
        with mock.patch('openai.OpenAI', fake):
            self.assertEqual(REAL_COMPOSE([]), '')
