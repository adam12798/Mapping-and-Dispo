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
        self.assertEqual(VoiceCallLog.all_objects.filter(channel='text').count(), 1)

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
        self.assertEqual(len(out['set']['call_notes'].split()), 20)

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
                     json.dumps({'rep_id': self.v_rep.id, 'lead_id': self.lead.id, 'text': 'x', 'context': []})):
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
        fake.assert_called_once_with(api_key='sk-test', timeout=20, max_retries=1)
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
