"""GET /api/v1/availability/ (maps/availability_read.py): the Provider
Portal's block numbers for Lurch, read only, Ventana keys only. Fictional
555 numbers only."""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from .models import APITenant, Lead, Organization, RepCountDefault, RepCountOverride, UserProfile
from .tests import TenancyBase

EASTERN = ZoneInfo('America/New_York')
WEEK = '2026-12-07'
URL = '/api/v1/availability/'
ROW_KEYS = {'date', 'time_block', 'label', 'start', 'end', 'rep_count', 'booked', 'open'}


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=EASTERN)


class AvailabilityBase(TenancyBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        v, s = cls.ventana, cls.sunshine
        for n, (when, cancelled) in enumerate([
            (at(2026, 12, 7, 9, 30), False),    # morning
            (at(2026, 12, 7, 11, 59), False),   # morning
            (at(2026, 12, 7, 12), False),       # midday
            (at(2026, 12, 7, 13), True),        # cancelled: not booked
            (at(2026, 12, 8, 18), False),       # evening
            (at(2026, 12, 8, 21), False),       # in no block
            (at(2026, 12, 10, 8), False),       # in no block
            (at(2026, 12, 9, 15), False),       # afternoon x4 against an override of 2
            (at(2026, 12, 9, 16), False),
            (at(2026, 12, 9, 17), False),
            (at(2026, 12, 9, 17, 30), False),
            (at(2026, 12, 15, 10), False),      # second week, morning
        ]):
            Lead.all_objects.create(address=f'{n} V St', phone_number=f'617555{3000 + n}',
                                    appointment_datetime=when, cancelled=cancelled, organization=v)
        # Team Sunshine's own bookings and rep counts in the same blocks
        for n in range(2):
            Lead.all_objects.create(address=f'{n} S St', phone_number=f'617555{4000 + n}',
                                    appointment_datetime=at(2026, 12, 7, 10), organization=s)
        RepCountOverride.all_objects.create(date='2026-12-07', time_block='morning', count=9, organization=s)
        RepCountDefault.all_objects.create(time_block='midday', count=7, organization=s)
        # Ventana's rep counts
        RepCountDefault.all_objects.create(time_block='midday', count=5, organization=v)
        RepCountOverride.all_objects.create(date='2026-12-08', time_block='morning', count=0, organization=v)
        RepCountOverride.all_objects.create(date='2026-12-09', time_block='afternoon', count=2, organization=v)

        cls.v_key = APITenant.objects.create(name='V Lurch', organization=v)
        cls.s_key = APITenant.objects.create(name='S Key', organization=s)
        cls.orgless_key = APITenant.objects.create(name='Legacy Key', organization=None)

    def get(self, key=None, **params):
        headers = {'HTTP_AUTHORIZATION': f'Bearer {key}'} if key else {}
        return Client().get(URL, {'week_start': WEEK, **params}, **headers)

    def portal(self, org, week_start):
        user = User.objects.create_user(f'provider-{uuid.uuid4().hex[:12]}', password='pw')
        UserProfile.all_objects.create(user=user, role='provider', organization=org)
        client = Client()
        client.force_login(user)
        return client.get('/api/provider/availability/', {'week_start': week_start})


class NumbersTests(AvailabilityBase):
    def test_shape_and_counts(self):
        resp = self.get(self.v_key.api_key)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(set(body), {'week_start', 'days', 'timezone', 'availability'})
        self.assertEqual((body['week_start'], body['days'], body['timezone']), (WEEK, 7, 'America/New_York'))
        rows = body['availability']
        self.assertEqual(len(rows), 28)
        self.assertTrue(all(set(row) == ROW_KEYS for row in rows))
        by = {(r['date'], r['time_block']): r for r in rows}
        self.assertEqual(by[(WEEK, 'morning')], {
            'date': WEEK, 'time_block': 'morning', 'label': '9-12 PM',
            'start': '2026-12-07T09:00:00-05:00', 'end': '2026-12-07T12:00:00-05:00',
            'rep_count': 3, 'booked': 2, 'open': 1})
        counts = lambda d, b: (by[(d, b)]['rep_count'], by[(d, b)]['booked'], by[(d, b)]['open'])
        self.assertEqual(counts(WEEK, 'midday'), (5, 1, 4))           # default 5; the cancelled lead is not booked
        self.assertEqual(counts(WEEK, 'evening'), (3, 0, 3))
        self.assertEqual(counts('2026-12-08', 'morning'), (0, 0, 0))   # override 0
        self.assertEqual(counts('2026-12-08', 'evening'), (3, 1, 2))   # 21:00 is in no block
        self.assertEqual(counts('2026-12-09', 'afternoon'), (2, 4, 0))  # open never below 0
        self.assertEqual(sum(r['booked'] for r in rows), 8)
        self.assertEqual([r['label'] for r in rows[:4]], ['9-12 PM', '12-3 PM', '3-6 PM', '6-9 PM'])

    def test_same_numbers_as_the_provider_portal(self):
        def portal_rows(week_start):
            resp = self.portal(self.ventana, week_start)
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.json()['hourly'])
            return resp.json()['availability']

        def ours(rows):
            return [{k: r[k] for k in ('date', 'time_block', 'rep_count', 'booked', 'open')} for r in rows]

        rows = self.get(self.v_key.api_key).json()['availability']
        self.assertEqual(ours(rows), portal_rows(WEEK))
        rows = self.get(self.v_key.api_key, days=14).json()['availability']
        self.assertEqual(len(rows), 56)
        self.assertEqual(ours(rows), portal_rows(WEEK) + portal_rows('2026-12-14'))
        self.assertEqual(rows[32]['booked'], 1)  # 2026-12-15 morning

    def test_days_and_dst(self):
        rows = self.get(self.v_key.api_key, week_start='2027-03-13', days=2).json()['availability']
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]['start'], '2027-03-13T09:00:00-05:00')
        self.assertEqual(rows[4]['start'], '2027-03-14T09:00:00-04:00')
        self.assertEqual(rows[7]['end'], '2027-03-14T21:00:00-04:00')

    def test_bad_requests(self):
        key = self.v_key.api_key
        self.assertEqual(Client().get(URL, HTTP_AUTHORIZATION=f'Bearer {key}').status_code, 400)
        for params in ({'week_start': '12/07/2026'}, {'days': 0}, {'days': 15}, {'days': 'x'},
                       {'week_start': '9999-12-31', 'days': 2}, {'week_start': '1999-12-31'}):
            self.assertEqual(self.get(key, **params).status_code, 400, params)
        self.assertEqual(Client().post(f'{URL}?week_start={WEEK}', HTTP_AUTHORIZATION=f'Bearer {key}').status_code,
                         405)


class AuthTests(AvailabilityBase):
    def assert_refused_untouched(self, resp, status, key=None):
        self.assertEqual(resp.status_code, status)
        self.assertNotIn('availability', resp.json())
        if key is not None:
            key.refresh_from_db()
            self.assertIsNone(key.last_used_at)  # api_key_required never ran

    def test_a_ventana_key_is_answered(self):
        self.assertEqual(self.get(self.v_key.api_key).status_code, 200)
        self.v_key.refresh_from_db()
        self.assertIsNotNone(self.v_key.last_used_at)

    def test_missing_unknown_and_malformed_keys_get_401(self):
        self.assert_refused_untouched(self.get(), 401)
        self.assert_refused_untouched(self.get('00000000-0000-0000-0000-000000000000'), 401)
        self.assert_refused_untouched(self.get('not-a-key'), 401)
        APITenant.objects.filter(id=self.v_key.id).update(is_active=False)
        self.assert_refused_untouched(self.get(self.v_key.api_key), 401, self.v_key)

    def test_a_team_sunshine_key_gets_403_every_way_it_is_sent(self):
        key = str(self.s_key.api_key)
        for resp in (self.get(key),
                     Client().get(URL, {'week_start': WEEK, 'api_key': key}),
                     Client().get(URL, {'week_start': WEEK}, HTTP_X_API_KEY=key),
                     Client().post(f'{URL}?week_start={WEEK}', HTTP_AUTHORIZATION=f'Bearer {key}')):
            self.assert_refused_untouched(resp, 403, self.s_key)

    def test_a_key_with_no_org_gets_403_never_the_default_inbound_org(self):
        self.assertEqual(Organization.default_inbound_id(), self.sunshine.id)
        self.assert_refused_untouched(self.get(self.orgless_key.api_key), 403, self.orgless_key)

    def test_an_inactive_ventana_org_gets_403(self):
        Organization.objects.filter(id=self.ventana.id).update(is_active=False)
        self.assert_refused_untouched(self.get(self.v_key.api_key), 403, self.v_key)

    def test_a_refused_key_reads_only_its_own_key_row(self):
        for key in (self.s_key, self.orgless_key):
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.get(key.api_key).status_code, 403)
            self.assertEqual(len(queries), 1, [q['sql'] for q in queries])
            sql = queries[0]['sql']
            self.assertTrue(sql.startswith('SELECT'), sql)
            self.assertIn('"maps_apitenant"', sql)
            self.assertNotIn('maps_lead', sql)
            self.assertNotIn('maps_repcount', sql)


class TeamSunshineUnchangedTests(AvailabilityBase):
    def snapshot(self):
        sun = self.sunshine.id
        return {
            'key': list(APITenant.objects.filter(organization_id=sun).values()),
            'leads': list(Lead.all_objects.filter(organization_id=sun).values()),
            'defaults': list(RepCountDefault.all_objects.filter(organization_id=sun).values()),
            'overrides': list(RepCountOverride.all_objects.filter(organization_id=sun).values()),
        }

    def team_sunshine_requests(self):
        leads = Client().get('/api/v1/leads/', HTTP_AUTHORIZATION=f'Bearer {self.s_key.api_key}')
        portal = self.portal(self.sunshine, WEEK)
        self.assertEqual((leads.status_code, portal.status_code), (200, 200))
        return leads.content, portal.content

    def test_team_sunshine_sees_no_difference(self):
        before = self.team_sunshine_requests()
        # Team Sunshine's own portal still counts only its own bookings and rep counts
        rows = {(r['date'], r['time_block']): r for r in self.portal(self.sunshine, WEEK).json()['availability']}
        self.assertEqual((rows[(WEEK, 'morning')]['rep_count'], rows[(WEEK, 'morning')]['booked']), (9, 2))
        self.assertEqual(rows[(WEEK, 'midday')]['rep_count'], 7)
        state = self.snapshot()

        self.assertEqual(self.get(self.s_key.api_key).status_code, 403)
        self.assertEqual(self.get(self.orgless_key.api_key).status_code, 403)
        ventana = self.get(self.v_key.api_key, days=14)
        self.assertEqual(ventana.status_code, 200)

        self.assertEqual(self.snapshot(), state)  # not one Team Sunshine row changed
        self.assertEqual(self.team_sunshine_requests(), before)  # and its requests answer byte for byte as before
        # and nothing of Team Sunshine's reached Ventana's numbers
        morning = ventana.json()['availability'][0]
        self.assertEqual((morning['rep_count'], morning['booked']), (3, 2))
