"""Slot availability and claims (maps/availability.py, INTEGRATION.md §6.7).

The concurrency tests and the over_capacity trigger test need PostgreSQL
(production's engine): on SQLite, SELECT ... FOR UPDATE and advisory locks do
nothing and there is no trigger, so they skip. Run the suite against both:
    python3 manage.py test maps
    DATABASE_URL=postgres://… python3 manage.py test maps

The clock is fixed at NOW (Tuesday 2026-11-24, 8 AM New York) so the dates
below stay in the future.
"""
import contextlib
import json
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import availability
from .models import (
    APITenant, Lead, Organization, RepCountDefault, RepCountOverride, SlotBlockLock, SlotClaim,
    UserProfile,
)
from .views import TIME_BLOCKS

EASTERN = ZoneInfo('America/New_York')
WEEK = '/api/v1/availability/'
BOOK = '/api/v1/availability/book/'
RELEASE = '/api/v1/availability/release/'
CREATE = '/api/v1/leads/create/'
DAY = '2026-12-01'          # a Tuesday, a week after NOW

requires_postgres = unittest.skipUnless(
    connection.vendor == 'postgresql', 'row locks, advisory locks and the trigger exist only on PostgreSQL')


def et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=EASTERN)


NOW = et(2026, 11, 24, 8)


def sid(name):
    """A setter call's session_ref: a UUID, stable per name."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f'lurch-session:{name}'))


def call(method, path, tenant, body=None):
    headers = {'HTTP_AUTHORIZATION': f'Bearer {tenant.api_key}'}
    if method == 'get':
        return Client().get(path, **headers)
    if method == 'put':
        return Client().generic('PUT', path, json.dumps(body), content_type='application/json', **headers)
    return Client().post(path, data=json.dumps(body), content_type='application/json', **headers)


def counts():
    return {
        'claims': SlotClaim.all_objects.count(),
        'locks': SlotBlockLock.all_objects.count(),
        'leads': Lead.all_objects.count(),
        'rep_defaults': RepCountDefault.all_objects.count(),
        'rep_overrides': RepCountOverride.all_objects.count(),
    }


class NoSends:
    """Fails the test if anything is sent: SMS, webhook, any HTTP request.
    Also fixes the clock at NOW."""

    def setUp(self):
        super().setUp()
        clock = mock.patch('django.utils.timezone.now', return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        self.sends = {}
        for target in ('maps.views.send_sms', 'maps.views.send_sms_with_result', 'maps.views.send_textblast',
                       'maps.views._send_ghl_dispo_webhook', 'maps.views._send_ghl_appt_webhook',
                       'maps.views.fire_webhooks', 'urllib.request.urlopen', 'maps.views.geocode'):
            patcher = mock.patch(target)
            self.sends[target] = patcher.start()
            self.addCleanup(patcher.stop)
        self.sends['maps.views.geocode'].return_value = (42.64, -71.31)

    def assertNothingSent(self):
        for target, fake in self.sends.items():
            if target != 'maps.views.geocode':
                self.assertFalse(fake.called, f'{target} was called')


class Base(NoSends, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.sunshine = Organization.objects.get(slug='team-sunshine')
        Organization.objects.filter(pk=cls.ventana.pk).update(slot_claims_enabled=True)
        cls.lurch = APITenant.objects.create(name='Lurch', organization=cls.ventana)
        cls.wednesday = APITenant.objects.create(name='Wednesday', organization=cls.ventana)
        cls.s_tenant = APITenant.objects.create(name='Team Sunshine', organization=cls.sunshine)
        cls.no_org = APITenant.objects.create(name='Legacy', organization=None)
        for key, _label, _start, _end in TIME_BLOCKS:
            RepCountDefault.all_objects.create(organization=cls.ventana, time_block=key, count=2)
            RepCountDefault.all_objects.create(organization=cls.sunshine, time_block=key, count=5)

    def capacity(self, day, key, count):
        RepCountOverride.all_objects.update_or_create(
            organization=self.ventana, date=day, time_block=key, defaults={'count': count})

    def lead(self, when, org=None, cancelled=False, phone='', **extra):
        return Lead.all_objects.create(address='1 Test St', appointment_datetime=when, cancelled=cancelled,
                                       phone_number=phone, organization=org or self.ventana, **extra)

    def book(self, session='s1', block='morning', day=DAY, key=None, tenant=None, **extra):
        body = {'date': day, 'time_block': block, 'idempotency_key': key or f'{session}:{day}:{block}',
                'session_ref': sid(session), **extra}
        return call('post', BOOK, tenant or self.lurch, body)

    def week(self, start=DAY, days=None, tenant=None):
        path = f'{WEEK}?week_start={start}' + (f'&days={days}' if days is not None else '')
        return call('get', path, tenant or self.lurch)

    def block(self, day, key):
        rows = self.week(day, 1).json()['availability']
        return next(r for r in rows if r['date'] == day and r['time_block'] == key)

    def assertError(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.content)
        body = resp.json()
        self.assertEqual(body['error'], code)
        self.assertIsInstance(body['detail'], str)
        self.assertTrue(body['detail'])
        return body


# ── The gate ──────────────────────────────────────────────────────────────

class GateTests(Base):
    def requests(self, tenant):
        return [
            self.week(tenant=tenant),
            self.book(tenant=tenant),
            call('post', RELEASE, tenant, {'session_ref': sid('s1')}),
        ]

    def test_team_sunshine_key_gets_403_and_nothing_is_created(self):
        before = counts()
        for resp in self.requests(self.s_tenant):
            self.assertError(resp, 403, 'not_enabled')
        self.assertEqual(counts(), before)
        self.assertNothingSent()

    def test_team_sunshine_stays_out_even_with_its_own_flag_on(self):
        Organization.objects.filter(pk=self.sunshine.pk).update(slot_claims_enabled=True)
        before = counts()
        for resp in self.requests(self.s_tenant):
            self.assertEqual(resp.status_code, 403)
        self.assertEqual(counts(), before)

    def test_key_with_no_org_gets_403_not_the_default_inbound_org(self):
        Organization.objects.filter(pk=self.sunshine.pk).update(slot_claims_enabled=True)
        before = counts()
        for resp in self.requests(self.no_org):
            self.assertEqual(resp.status_code, 403)
        self.assertEqual(counts(), before)

    def test_flag_off_gets_403(self):
        Organization.objects.filter(pk=self.ventana.pk).update(slot_claims_enabled=False)
        before = counts()
        for resp in self.requests(self.lurch):
            self.assertEqual(resp.status_code, 403)
        self.assertEqual(counts(), before)

    def test_inactive_org_gets_403(self):
        Organization.objects.filter(pk=self.ventana.pk).update(is_active=False)
        self.assertEqual(self.book().status_code, 403)

    def test_no_key_or_unknown_key_is_401(self):
        self.assertEqual(Client().get(f'{WEEK}?week_start={DAY}').status_code, 401)
        resp = Client().get(f'{WEEK}?week_start={DAY}',
                            HTTP_AUTHORIZATION='Bearer 00000000-0000-0000-0000-000000000000')
        self.assertEqual(resp.status_code, 401)

    def test_wrong_methods_are_405(self):
        self.assertError(call('post', WEEK, self.lurch, {}), 405, 'method_not_allowed')
        self.assertError(call('get', BOOK, self.lurch), 405, 'method_not_allowed')
        self.assertError(call('get', RELEASE, self.lurch), 405, 'method_not_allowed')


# ── GET availability ──────────────────────────────────────────────────────

class WeekTests(Base):
    def test_shape_and_capacity(self):
        resp = self.week(DAY)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual((body['timezone'], body['start_step_minutes'], body['full_policy']),
                         ('America/New_York', 30, 'refuse'))
        rows = body['availability']
        self.assertEqual(len(rows), 7 * len(TIME_BLOCKS))
        self.assertEqual(rows[0], {
            'date': DAY, 'time_block': 'morning', 'label': '9-12 PM',
            'start': '2026-12-01T09:00:00-05:00', 'end': '2026-12-01T12:00:00-05:00',
            'start_times': ['2026-12-01T09:00:00-05:00', '2026-12-01T09:30:00-05:00',
                            '2026-12-01T10:00:00-05:00', '2026-12-01T10:30:00-05:00',
                            '2026-12-01T11:00:00-05:00', '2026-12-01T11:30:00-05:00'],
            'rep_count': 2, 'booked': 0, 'open': 2, 'bookable': True, 'leads': 0, 'claims': 0,
        })
        afternoon = rows[2]
        self.assertEqual((afternoon['start_times'][0], afternoon['start_times'][-1]),
                         ('2026-12-01T15:00:00-05:00', '2026-12-01T17:30:00-05:00'))
        self.assertNothingSent()

    def test_the_step_is_the_organizations_setting(self):
        Organization.objects.filter(pk=self.ventana.pk).update(slot_start_step_minutes=60)
        row = self.block(DAY, 'evening')
        self.assertEqual(row['start_times'], ['2026-12-01T18:00:00-05:00', '2026-12-01T19:00:00-05:00',
                                              '2026-12-01T20:00:00-05:00'])
        Organization.objects.filter(pk=self.ventana.pk).update(slot_start_step_minutes=45)
        self.assertEqual([t[11:16] for t in self.block(DAY, 'evening')['start_times']],
                         ['18:00', '18:45', '19:30', '20:15'])

    def test_only_future_start_times_and_bookable(self):
        with mock.patch('django.utils.timezone.now', return_value=et(2026, 12, 1, 16, 10)):
            rows = {r['time_block']: r for r in self.week(DAY, 1).json()['availability']}
        self.assertEqual(rows['morning']['start_times'], [])
        self.assertFalse(rows['morning']['bookable'])
        self.assertEqual([t[11:16] for t in rows['afternoon']['start_times']], ['16:30', '17:00', '17:30'])
        self.assertTrue(rows['afternoon']['bookable'])
        self.assertEqual(len(rows['evening']['start_times']), 6)
        self.capacity(DAY, 'evening', 0)
        self.assertFalse(self.block(DAY, 'evening')['bookable'])

    def test_booked_is_leads_plus_live_claims_only(self):
        self.capacity(DAY, 'afternoon', 3)
        self.lead(et(2026, 12, 1, 15, 30))
        self.lead(et(2026, 12, 1, 16), cancelled=True)                  # cancelled: frees its spot
        self.lead(et(2026, 12, 1, 15, 30), org=self.sunshine)           # another org: never seen
        self.assertEqual(self.book('a', 'afternoon').status_code, 201)  # live
        for state in ('released', 'consumed', 'expired'):
            SlotClaim.all_objects.create(
                organization=self.ventana, date=DAY, time_block='afternoon', idempotency_key=state,
                session_ref=sid(state), expires_at=NOW - timedelta(seconds=1) if state == 'expired'
                else NOW + timedelta(hours=1),
                released_at=NOW if state == 'released' else None,
                consumed_at=NOW if state == 'consumed' else None)
        row = self.block(DAY, 'afternoon')
        self.assertEqual((row['rep_count'], row['leads'], row['claims'], row['booked'], row['open']),
                         (3, 1, 1, 2, 1))

    def test_override_zero_blocks_the_block(self):
        self.capacity(DAY, 'evening', 0)
        row = self.block(DAY, 'evening')
        self.assertEqual((row['rep_count'], row['open'], row['bookable']), (0, 0, False))
        body = self.assertError(self.book('a', 'evening'), 409, 'slot_full')
        self.assertEqual((body['date'], body['time_block'], body['open']), (DAY, 'evening', 0))

    def test_open_never_negative(self):
        self.capacity(DAY, 'midday', 1)
        self.lead(et(2026, 12, 1, 12))
        self.lead(et(2026, 12, 1, 13))
        row = self.block(DAY, 'midday')
        self.assertEqual((row['booked'], row['open']), (2, 0))

    def test_range_limits(self):
        self.assertEqual(len(self.week(DAY, 14).json()['availability']), 14 * 4)
        self.assertEqual(len(self.week(DAY, 1).json()['availability']), 4)
        for bad in (0, 15, 'x'):
            self.assertError(self.week(DAY, bad), 400, 'invalid_days')
        self.assertError(call('get', WEEK, self.lurch), 400, 'invalid_week_start')
        self.assertError(call('get', f'{WEEK}?week_start=12/01/2026', self.lurch), 400, 'invalid_week_start')


class BlockEdgeTests(Base):
    """New York time, on ordinary days and on both DST days."""

    def leads_by_block(self, day):
        rows = self.week(day, 1).json()['availability']
        return {r['time_block']: r['leads'] for r in rows}

    def test_edges_of_the_day(self):
        for h, mi in ((8, 59), (9, 0), (11, 59), (12, 0), (20, 59), (21, 0)):
            self.lead(et(2026, 12, 2, h, mi))
        self.assertEqual(self.leads_by_block('2026-12-02'),
                         {'morning': 2, 'midday': 1, 'afternoon': 0, 'evening': 1})
        self.assertIsNone(availability.block_of(et(2026, 12, 2, 8, 59)))
        self.assertEqual(availability.block_of(et(2026, 12, 2, 9)), (datetime(2026, 12, 2).date(), 'morning'))
        self.assertEqual(availability.block_of(et(2026, 12, 2, 20, 59))[1], 'evening')
        self.assertIsNone(availability.block_of(et(2026, 12, 2, 21)))

    def test_fall_back_2026_11_01(self):
        self.lead(et(2026, 10, 31, 20, 30))     # 00:30 UTC on Nov 1: still Oct 31's evening
        self.lead(et(2026, 11, 1, 9))           # 14:00 UTC (EST)
        self.lead(et(2026, 11, 1, 20, 59))      # 01:59 UTC on Nov 2
        self.lead(et(2026, 11, 1, 21))          # no block
        self.assertEqual(self.leads_by_block('2026-10-31'),
                         {'morning': 0, 'midday': 0, 'afternoon': 0, 'evening': 1})
        self.assertEqual(self.leads_by_block('2026-11-01'),
                         {'morning': 1, 'midday': 0, 'afternoon': 0, 'evening': 1})
        with mock.patch('django.utils.timezone.now', return_value=et(2026, 10, 30, 12)):
            rows = self.week('2026-10-31', 2).json()['availability']
        self.assertEqual([r['start'] for r in rows if r['time_block'] == 'morning'],
                         ['2026-10-31T09:00:00-04:00', '2026-11-01T09:00:00-05:00'])
        self.assertEqual([r['start_times'][-1] for r in rows if r['time_block'] == 'evening'],
                         ['2026-10-31T20:30:00-04:00', '2026-11-01T20:30:00-05:00'])
        utc_lead = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo('UTC'))
        self.assertEqual(availability.block_of(utc_lead), (datetime(2026, 10, 31).date(), 'evening'))

    def test_spring_forward_2027_03_14(self):
        self.lead(et(2027, 3, 13, 20, 59))      # 01:59 UTC on Mar 14
        self.lead(et(2027, 3, 14, 8, 59))       # no block
        self.lead(et(2027, 3, 14, 9))           # 13:00 UTC (EDT)
        self.lead(et(2027, 3, 14, 20, 59))      # 00:59 UTC on Mar 15
        self.assertEqual(self.leads_by_block('2027-03-13'),
                         {'morning': 0, 'midday': 0, 'afternoon': 0, 'evening': 1})
        self.assertEqual(self.leads_by_block('2027-03-14'),
                         {'morning': 1, 'midday': 0, 'afternoon': 0, 'evening': 1})
        rows = self.week('2027-03-13', 2).json()['availability']
        self.assertEqual([r['end'] for r in rows if r['time_block'] == 'evening'],
                         ['2027-03-13T21:00:00-05:00', '2027-03-14T21:00:00-04:00'])

    def test_claims_on_dst_days(self):
        with mock.patch('django.utils.timezone.now', return_value=et(2026, 10, 30, 12)):
            self.capacity('2026-11-01', 'morning', 1)
            self.assertEqual(self.book('a', 'morning', '2026-11-01').status_code, 201)
            self.assertEqual(self.book('b', 'morning', '2026-11-01').status_code, 409)
        self.capacity('2027-03-14', 'evening', 1)
        ok = self.book('a', 'evening', '2027-03-14', start_time='2027-03-14T18:30:00-04:00')
        self.assertEqual(ok.status_code, 201)
        self.assertEqual(ok.json()['start_time'], '2027-03-14T18:30:00-04:00')
        self.assertEqual(self.book('b', 'evening', '2027-03-14').status_code, 409)


# ── POST book / release ───────────────────────────────────────────────────

class BookTests(Base):
    def test_claim_then_full(self):
        self.capacity(DAY, 'morning', 1)
        resp = self.book('a', phone='(978) 555-0101')
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        claim = SlotClaim.all_objects.get()
        self.assertEqual(body, {
            'claim_id': str(claim.claim_id), 'date': DAY, 'time_block': 'morning',
            'block_start': '2026-12-01T09:00:00-05:00', 'block_end': '2026-12-01T12:00:00-05:00',
            'start_time': None, 'over_capacity': False, 'expires_at': '2026-11-25T08:00:00-05:00',
            'state': 'live', 'open_after': 0, 'released': [],
        })
        self.assertEqual((claim.organization_id, claim.session_ref, claim.phone10),
                         (self.ventana.id, sid('a'), '9785550101'))
        self.assertError(self.book('b'), 409, 'slot_full')
        self.assertEqual(SlotClaim.all_objects.count(), 1)
        self.assertEqual(self.block(DAY, 'morning')['open'], 0)
        self.assertNothingSent()

    def test_an_exact_start_time(self):
        self.capacity(DAY, 'afternoon', 5)
        resp = self.book('a', 'afternoon', start_time='2026-12-01T15:30:00-05:00')
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual((body['block_start'], body['block_end'], body['start_time']),
                         ('2026-12-01T15:00:00-05:00', '2026-12-01T18:00:00-05:00', '2026-12-01T15:30:00-05:00'))
        self.assertEqual(SlotClaim.all_objects.get().start_time, et(2026, 12, 1, 15, 30))
        # No offset: New York time. Same instant written in UTC: accepted.
        self.assertEqual(self.book('b', 'afternoon', start_time='2026-12-01T16:00').json()['start_time'],
                         '2026-12-01T16:00:00-05:00')
        self.assertEqual(self.book('c', 'afternoon', start_time='2026-12-01T22:00:00+00:00').json()['start_time'],
                         '2026-12-01T17:00:00-05:00')
        # Any minute, not only the suggested ones; the replay returns it too.
        odd = self.book('d', 'afternoon', start_time='2026-12-01T17:59:00-05:00')
        self.assertEqual((odd.status_code, odd.json()['start_time']), (201, '2026-12-01T17:59:00-05:00'))
        replay = self.book('d', 'afternoon', start_time='2026-12-01T17:59:00-05:00')
        self.assertEqual((replay.status_code, replay.json()['start_time']), (200, '2026-12-01T17:59:00-05:00'))

    def test_a_start_time_alone_names_its_block(self):
        body = {'idempotency_key': 'k', 'session_ref': sid('a'), 'start_time': '2026-12-01T19:07:00-05:00'}
        resp = call('post', BOOK, self.lurch, body)
        self.assertEqual(resp.status_code, 201)
        self.assertEqual((resp.json()['date'], resp.json()['time_block'], resp.json()['block_start']),
                         (DAY, 'evening', '2026-12-01T18:00:00-05:00'))
        self.assertEqual(self.block(DAY, 'evening')['claims'], 1)

    def test_a_start_time_must_be_in_the_block(self):
        before = counts()
        # In another block or on another day than the body's date and time_block.
        for start in ('2026-12-01T14:30:00-05:00', '2026-12-01T18:00:00-05:00', '2026-12-02T15:30:00-05:00'):
            self.assertError(self.book('a', 'afternoon', start_time=start), 422, 'start_outside_block')
        # In no block at all.
        for start in ('2026-12-01T08:30:00-05:00', '2026-12-01T21:00:00-05:00', '2026-12-01T23:59:00-05:00'):
            body = {'idempotency_key': 'k', 'session_ref': sid('a'), 'start_time': start}
            self.assertError(call('post', BOOK, self.lurch, body), 422, 'start_outside_block')
        # Not a time, or not a whole minute.
        for start in ('3:30 PM', 1530, '2026-13-01T15:00', '2026-12-01T15:30:30-05:00',
                      '2026-12-01T15:30:00.500-05:00'):
            self.assertError(self.book('a', 'afternoon', start_time=start), 400, 'invalid_start_time')
        self.assertEqual(counts(), before)

    def test_a_full_block_is_refused_by_default(self):
        self.capacity(DAY, 'morning', 1)
        self.assertEqual(self.book('a').status_code, 201)
        self.assertError(self.book('b', start_time='2026-12-01T11:15:00-05:00'), 409, 'slot_full')
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_full_policy_flag_takes_the_booking_and_marks_it(self):
        Organization.objects.filter(pk=self.ventana.pk).update(slot_full_policy='flag')
        self.capacity(DAY, 'morning', 1)
        first = self.book('a', phone='5550000001')
        self.assertEqual((first.status_code, first.json()['over_capacity']), (201, False))
        second = self.book('b', start_time='2026-12-01T11:15:00-05:00', phone='5550000002')
        self.assertEqual(second.status_code, 201)
        self.assertEqual((second.json()['over_capacity'], second.json()['open_after']), (True, 0))
        self.assertIs(SlotClaim.all_objects.get(session_ref=sid('b')).over_capacity, True)
        week = self.week(DAY, 1).json()
        row = next(r for r in week['availability'] if r['time_block'] == 'morning')
        self.assertEqual((week['full_policy'], row['booked'], row['open'], row['bookable']), ('flag', 2, 0, True))
        # Its lead inherits the mark; the first claim's lead does not.
        for claim_id, hub, expected in ((second.json()['claim_id'], 'w:2', True),
                                        (first.json()['claim_id'], 'w:1', False)):
            resp = call('post', CREATE, self.wednesday, {
                'address': '1 Test St', 'phone_number': '9780000000', 'hub_customer_id': hub,
                'appointment_datetime': '2026-12-01T11:15:00-05:00', 'claim_id': claim_id})
            self.assertIs(Lead.all_objects.get(pk=resp.json()['id']).over_capacity, expected)

    def test_past_dates_and_ended_blocks_are_refused(self):
        self.assertError(self.book('a', day='2020-01-01'), 422, 'block_past')
        with mock.patch('django.utils.timezone.now', return_value=et(2026, 12, 1, 12, 30)):
            self.assertError(self.book('a', 'morning'), 422, 'block_past')                 # ended at 12:00
            self.assertError(self.book('a', 'midday', start_time='2026-12-01T12:00:00-05:00'), 422, 'block_past')
            self.assertError(self.book('a', 'midday', start_time='2026-12-01T12:30:00-05:00'), 422, 'block_past')
            self.assertEqual(self.book('a', 'midday', start_time='2026-12-01T13:00:00-05:00').status_code, 201)
            self.assertEqual(self.book('b', 'midday').status_code, 201)                    # under way, no start
        self.assertEqual(SlotClaim.all_objects.count(), 2)

    def test_lead_fills_the_block_too(self):
        self.capacity(DAY, 'morning', 1)
        self.lead(et(2026, 12, 1, 10))
        self.assertEqual(self.book('a').status_code, 409)

    def test_idempotent_replay(self):
        first = self.book('a', key='k-1')
        again = self.book('a', key='k-1')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['claim_id'], first.json()['claim_id'])
        self.assertTrue(again.json()['replayed'])
        self.assertEqual(SlotClaim.all_objects.count(), 1)
        # The same key for another block or start time is a client bug, not a second claim.
        self.assertError(self.book('a', 'midday', key='k-1'), 422, 'idempotency_key_reused')
        self.assertError(self.book('a', key='k-1', start_time='2026-12-01T10:00:00-05:00'),
                         422, 'idempotency_key_reused')
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_replay_still_answers_when_the_block_is_full(self):
        self.capacity(DAY, 'morning', 1)
        first = self.book('a', key='k-1')
        again = self.book('a', key='k-1')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['claim_id'], first.json()['claim_id'])

    def test_replay_after_release_or_expiry_is_409_with_the_claim(self):
        first = self.book('a', key='k-1').json()
        call('post', RELEASE, self.lurch, {'claim_id': first['claim_id']})
        body = self.assertError(self.book('a', key='k-1'), 409, 'claim_not_live')
        self.assertEqual((body['claim_id'], body['state']), (first['claim_id'], 'released'))
        SlotClaim.all_objects.update(expires_at=NOW - timedelta(seconds=1), released_at=None)
        body = self.assertError(self.book('a', key='k-1'), 409, 'claim_not_live')
        self.assertEqual(body['state'], 'expired')
        SlotClaim.all_objects.update(expires_at=NOW + timedelta(hours=1), consumed_at=NOW)
        consumed = self.book('a', key='k-1')
        self.assertEqual((consumed.status_code, consumed.json()['state']), (200, 'consumed'))
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_a_new_claim_releases_the_sessions_other_claims(self):
        self.capacity(DAY, 'morning', 1)
        first = self.book('a', 'morning').json()
        second = self.book('a', 'afternoon')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()['released'], [first['claim_id']])
        old = SlotClaim.all_objects.get(claim_id=first['claim_id'])
        self.assertIsNotNone(old.released_at)
        self.assertEqual(self.block(DAY, 'morning')['open'], 1)
        self.assertEqual(self.book('b', 'morning').status_code, 201)

    def test_same_session_new_key_in_the_same_block_replaces_its_claim(self):
        self.capacity(DAY, 'morning', 1)
        first = self.book('a', key='k-1').json()
        second = self.book('a', key='k-2', start_time='2026-12-01T10:30:00-05:00')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()['released'], [first['claim_id']])
        self.assertEqual(self.block(DAY, 'morning')['claims'], 1)

    def test_a_full_block_keeps_the_sessions_earlier_claim(self):
        self.capacity(DAY, 'afternoon', 0)
        first = self.book('a', 'morning').json()
        self.assertEqual(self.book('a', 'afternoon').status_code, 409)
        self.assertEqual(SlotClaim.all_objects.get(claim_id=first['claim_id']).state(NOW), 'live')

    def test_other_sessions_are_untouched(self):
        a = self.book('a', 'morning').json()
        self.book('b', 'afternoon')
        self.assertIsNone(SlotClaim.all_objects.get(claim_id=a['claim_id']).released_at)

    def test_a_session_ref_must_be_a_uuid(self):
        # A constant like "lurch" would let every setter release (and overbook
        # past) every other setter's claims.
        self.capacity(DAY, 'morning', 1)
        before = counts()
        for constant in ('lurch', 'setter-1', '', 12345, None):
            body = {'date': DAY, 'time_block': 'morning', 'idempotency_key': f'k-{constant}',
                    'session_ref': constant}
            self.assertError(call('post', BOOK, self.lurch, body), 400, 'invalid_session_ref')
        self.assertError(call('post', RELEASE, self.lurch, {'session_ref': 'lurch'}), 400, 'invalid_session_ref')
        self.assertEqual(counts(), before)
        upper = str(uuid.uuid4()).upper()
        self.assertEqual(call('post', BOOK, self.lurch, {'date': DAY, 'time_block': 'morning',
                                                         'idempotency_key': 'k', 'session_ref': upper}).status_code,
                         201)
        self.assertEqual(SlotClaim.all_objects.get().session_ref, upper.lower())

    def test_ttl_comes_from_the_organization_and_expiry_frees_the_spot(self):
        self.capacity(DAY, 'morning', 1)
        body = self.book('a').json()
        claim = SlotClaim.all_objects.get()
        self.assertEqual(claim.expires_at - claim.created_at, timedelta(hours=24))
        self.assertEqual(datetime.fromisoformat(body['expires_at']), NOW + timedelta(hours=24))
        just_before = NOW + timedelta(hours=24) - timedelta(seconds=1)
        with mock.patch('django.utils.timezone.now', return_value=just_before):
            self.assertEqual(self.block(DAY, 'morning')['open'], 0)
            self.assertEqual(self.book('b').status_code, 409)
        with mock.patch('django.utils.timezone.now', return_value=NOW + timedelta(hours=24)):
            self.assertEqual(self.block(DAY, 'morning')['open'], 1)
            self.assertEqual(self.book('b').status_code, 201)

        Organization.objects.filter(pk=self.ventana.pk).update(slot_claim_ttl_hours=3)
        self.book('c', 'midday')
        claim = SlotClaim.all_objects.get(session_ref=sid('c'))
        self.assertEqual(claim.expires_at - claim.created_at, timedelta(hours=3))

    def test_bad_bodies_are_400_and_create_nothing(self):
        before = counts()
        good = {'date': DAY, 'time_block': 'morning', 'idempotency_key': 'k', 'session_ref': sid('s')}
        cases = [
            ({'date': '12/01/2026'}, 'invalid_date'),
            ({'date': 20261201}, 'invalid_date'),
            ({'time_block': 'night'}, 'invalid_time_block'),
            ({'time_block': ['morning']}, 'invalid_time_block'),
            ({'time_block': {'a': 1}}, 'invalid_time_block'),
            ({'time_block': 5}, 'invalid_time_block'),
            ({'idempotency_key': None}, 'invalid_idempotency_key'),
            ({'idempotency_key': 5}, 'invalid_idempotency_key'),
            ({'idempotency_key': 'k' * 101}, 'invalid_idempotency_key'),
            ({'idempotency_key': '  '}, 'invalid_idempotency_key'),
        ]
        for change, code in cases:
            self.assertError(call('post', BOOK, self.lurch, {**good, **change}), 400, code)
        self.assertError(call('post', BOOK, self.lurch, ['not', 'an', 'object']), 400, 'invalid_body')
        resp = Client().post(BOOK, data='{nope', content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {self.lurch.api_key}')
        self.assertError(resp, 400, 'invalid_body')
        self.assertEqual(counts(), before)


class ReleaseTests(Base):
    def test_release_by_claim_id(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a').json()['claim_id']
        resp = call('post', RELEASE, self.lurch, {'claim_id': claim_id})
        self.assertEqual(resp.json(), {'released': [claim_id]})
        self.assertEqual(self.block(DAY, 'morning')['open'], 1)
        again = call('post', RELEASE, self.lurch, {'claim_id': claim_id})
        self.assertEqual(again.json(), {'released': []})
        self.assertNothingSent()

    def test_release_by_session(self):
        self.book('a', 'morning')
        self.book('b', 'midday')
        resp = call('post', RELEASE, self.lurch, {'session_ref': sid('a')})
        self.assertEqual(len(resp.json()['released']), 1)
        self.assertIsNone(SlotClaim.all_objects.get(session_ref=sid('b')).released_at)

    def test_a_consumed_claim_is_never_released(self):
        claim_id = self.book('a').json()['claim_id']
        SlotClaim.all_objects.update(consumed_at=NOW)
        self.assertEqual(call('post', RELEASE, self.lurch, {'claim_id': claim_id}).json(), {'released': []})
        self.assertIsNone(SlotClaim.all_objects.get().released_at)

    def test_bad_requests(self):
        self.assertError(call('post', RELEASE, self.lurch, {}), 400, 'invalid_body')
        self.assertError(call('post', RELEASE, self.lurch, {'claim_id': 'x', 'session_ref': sid('a')}),
                         400, 'invalid_body')
        self.assertError(call('post', RELEASE, self.lurch, {'claim_id': 'not-a-uuid'}), 400, 'invalid_claim_id')
        self.assertError(call('post', RELEASE, self.lurch, {'claim_id': {'a': 1}}), 400, 'invalid_claim_id')
        missing = call('post', RELEASE, self.lurch, {'claim_id': '00000000-0000-0000-0000-000000000000'})
        self.assertError(missing, 404, 'claim_not_found')

    def test_another_orgs_claim_is_not_found(self):
        other = SlotClaim.all_objects.create(organization=self.sunshine, date=DAY, time_block='morning',
                                             idempotency_key='k', session_ref=sid('a'),
                                             expires_at=NOW + timedelta(hours=1))
        self.assertEqual(call('post', RELEASE, self.lurch, {'claim_id': str(other.claim_id)}).status_code, 404)
        self.assertEqual(call('post', RELEASE, self.lurch, {'session_ref': sid('a')}).json(), {'released': []})
        self.assertIsNone(SlotClaim.all_objects.get(pk=other.pk).released_at)


# ── v1 lead create ────────────────────────────────────────────────────────

class LeadCreateTests(Base):
    def create(self, tenant=None, **fields):
        body = {'address': '9 Hub St', 'city': 'Lowell', 'homeowner_name': 'Nina New',
                'phone_number': '9785550101', 'appointment_datetime': '2026-12-01T10:00:00-05:00',
                'hub_customer_id': 'wednesday:abc', **fields}
        return call('post', CREATE, tenant or self.wednesday, body)

    def assertCreated(self, resp):
        self.assertEqual(resp.status_code, 201)
        lead = Lead.all_objects.get(pk=resp.json()['id'])
        self.assertEqual(resp.content, json.dumps({'status': 'ok', 'id': lead.id}).encode())
        fire = self.sends['maps.views.fire_webhooks']
        self.assertEqual(fire.call_count, 1)
        self.assertEqual(fire.call_args.args[0], 'lead_created')
        self.assertEqual(fire.call_args.args[1].pk, lead.pk)
        return lead

    def test_claim_id_is_consumed_and_not_double_counted(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a', phone='5550000000').json()['claim_id']   # another phone: no fallback
        self.assertEqual(self.block(DAY, 'morning')['booked'], 1)
        lead = self.assertCreated(self.create(claim_id=claim_id))
        claim = SlotClaim.all_objects.get()
        self.assertEqual(claim.consumed_by_lead_id, lead.id)
        self.assertIsNotNone(claim.consumed_at)
        self.assertIs(lead.over_capacity, False)
        row = self.block(DAY, 'morning')
        self.assertEqual((row['leads'], row['claims'], row['booked'], row['open']), (1, 0, 1, 0))

    def test_no_claim_and_a_full_block_still_creates_the_lead_marked_over_capacity(self):
        self.capacity(DAY, 'morning', 1)
        self.lead(et(2026, 12, 1, 9, 30))
        lead = self.assertCreated(self.create())
        self.assertIs(lead.over_capacity, True)
        self.assertEqual(self.block(DAY, 'morning')['booked'], 2)

    def test_without_claim_id_while_a_claim_is_live_warns_and_both_count(self):
        # Its own claim, booked under another phone: nothing links them, so
        # both count, the lead is over capacity, and the log says why.
        self.capacity(DAY, 'morning', 1)
        self.book('a', phone='9785550000')
        with self.assertLogs('maps.availability', 'WARNING') as logs:
            lead = self.assertCreated(self.create(phone_number='9785559999'))
        self.assertIn('without a usable claim_id while 1 live claim(s)', logs.output[0])
        self.assertIs(lead.over_capacity, True)
        self.assertIsNone(SlotClaim.all_objects.get().consumed_at)
        self.assertEqual(self.block(DAY, 'morning')['booked'], 2)

    def test_room_left_is_not_over_capacity(self):
        self.capacity(DAY, 'morning', 2)
        self.lead(et(2026, 12, 1, 9, 30))
        self.assertIs(self.assertCreated(self.create()).over_capacity, False)

    def test_phone_fallback_consumes_the_matching_claim(self):
        self.capacity(DAY, 'morning', 1)
        self.book('a', phone='+1 (978) 555-0101')
        with self.assertLogs('maps.availability', 'WARNING') as logs:
            lead = self.assertCreated(self.create(phone_number='978-555-0101'))
        self.assertIn('by phone', logs.output[0])
        self.assertIs(lead.over_capacity, False)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, lead.id)

    def test_unknown_or_malformed_claim_id_is_checked_like_no_claim(self):
        self.capacity(DAY, 'morning', 1)
        self.lead(et(2026, 12, 1, 9, 30))
        for claim_id in ('00000000-0000-0000-0000-000000000000', 'garbage', 12, ['x']):
            Lead.all_objects.filter(hub_customer_id='wednesday:abc').delete()
            self.sends['maps.views.fire_webhooks'].reset_mock()
            self.assertIs(self.assertCreated(self.create(claim_id=claim_id)).over_capacity, True)

    def test_claim_for_another_block_is_consumed_logged_and_this_block_is_checked(self):
        self.capacity(DAY, 'morning', 1)
        self.capacity(DAY, 'evening', 1)
        self.lead(et(2026, 12, 1, 9, 30))
        claim_id = self.book('a', 'evening', phone='5550000000').json()['claim_id']
        with self.assertLogs('maps.availability', 'WARNING') as logs:
            lead = self.assertCreated(self.create(claim_id=claim_id))   # the form says 10:00, not evening
        self.assertIn(f'consumes claim {claim_id} for 2026-12-01 evening', logs.output[0])
        self.assertIs(lead.over_capacity, True)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, lead.id)
        self.assertEqual(self.block(DAY, 'evening')['open'], 1)

    def test_claim_for_another_date_is_logged(self):
        claim_id = self.book('a', 'morning', '2026-12-02').json()['claim_id']
        with self.assertLogs('maps.availability', 'WARNING') as logs:
            self.assertCreated(self.create(claim_id=claim_id))
        self.assertIn('for 2026-12-02 morning, but its appointment is 2026-12-01T10:00:00-05:00', logs.output[0])

    def test_an_expired_claim_no_longer_holds_the_spot(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a').json()['claim_id']
        SlotClaim.all_objects.update(expires_at=NOW - timedelta(seconds=1))
        self.lead(et(2026, 12, 1, 9, 30))                     # took the freed spot
        lead = self.assertCreated(self.create(claim_id=claim_id))
        self.assertIs(lead.over_capacity, True)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, lead.id)

    def test_a_released_claim_is_linked_but_holds_nothing(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a', phone='5550000000').json()['claim_id']
        call('post', RELEASE, self.lurch, {'claim_id': claim_id})
        self.lead(et(2026, 12, 1, 9, 30))
        lead = self.assertCreated(self.create(claim_id=claim_id))
        self.assertIs(lead.over_capacity, True)
        claim = SlotClaim.all_objects.get()
        self.assertEqual((claim.consumed_by_lead_id, claim.released_at is not None), (lead.id, True))

    def test_a_consumed_claim_is_not_consumed_twice(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a', phone='5550000000').json()['claim_id']
        first = self.assertCreated(self.create(claim_id=claim_id))
        self.sends['maps.views.fire_webhooks'].reset_mock()
        second = self.assertCreated(self.create(claim_id=claim_id, hub_customer_id='wednesday:def'))
        self.assertIs(second.over_capacity, True)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, first.id)

    def test_appointment_outside_every_block_is_unchecked(self):
        self.capacity(DAY, 'morning', 0)
        for at in ('2026-12-01T08:30:00-05:00', '2026-12-01T21:00:00-05:00', None):
            Lead.all_objects.filter(hub_customer_id='wednesday:abc').delete()
            self.sends['maps.views.fire_webhooks'].reset_mock()
            self.assertIsNone(self.assertCreated(self.create(appointment_datetime=at)).over_capacity)
        self.assertEqual(SlotBlockLock.all_objects.count(), 0)

    def test_a_naive_time_is_new_york_time(self):
        self.capacity(DAY, 'evening', 0)
        lead = self.assertCreated(self.create(appointment_datetime='2026-12-01 20:30'))
        self.assertIs(lead.over_capacity, True)

    def test_a_failing_check_never_refuses_the_lead(self):
        with mock.patch('maps.availability._check', side_effect=RuntimeError('boom')), \
                self.assertLogs('maps.availability', 'ERROR'):
            lead = self.assertCreated(self.create())
        self.assertIsNone(lead.over_capacity)

    def test_nothing_else_is_sent(self):
        self.capacity(DAY, 'morning', 1)
        self.lead(et(2026, 12, 1, 9, 30))
        self.assertCreated(self.create())
        for target, fake in self.sends.items():
            if target not in ('maps.views.geocode', 'maps.views.fire_webhooks'):
                self.assertFalse(fake.called, target)


class LeadCreateUnchangedTests(Base):
    """With the flag off, or for any other organization, v1 lead create runs
    exactly as before: the same response bytes, the same row, the same writes,
    and the slot tables untouched."""

    BODY = {'address': '9 Hub St', 'phone_number': '9785550101', 'hub_customer_id': 'h-1',
            'appointment_datetime': '2026-12-01T10:00:00-05:00'}

    def run_create(self, tenant, body):
        with CaptureQueriesContext(connection) as queries:
            resp = call('post', CREATE, tenant, body)
        self.assertEqual(resp.status_code, 201)
        lead = Lead.all_objects.get(pk=resp.json()['id'])
        self.assertEqual(resp.content, json.dumps({'status': 'ok', 'id': lead.id}).encode())
        sql = [q['sql'] for q in queries.captured_queries]
        self.assertFalse([s for s in sql if 'maps_slot' in s or 'advisory' in s], 'touched a slot table or lock')
        writes = sorted(s.split()[0] + ' ' + s.split()[2 if s.startswith('INSERT') else 1].strip('"')
                        for s in sql if s.split()[0] in ('INSERT', 'UPDATE', 'DELETE'))
        self.assertEqual(writes, ['INSERT maps_lead', 'UPDATE maps_apitenant'])
        self.assertIsNone(lead.over_capacity)
        fire = self.sends['maps.views.fire_webhooks']
        self.assertEqual([c.args[0] for c in fire.call_args_list], ['lead_created'])
        return lead

    def test_flag_off(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a', phone='9785550101').json()['claim_id']
        self.lead(et(2026, 12, 1, 9, 30))
        Organization.objects.filter(pk=self.ventana.pk).update(slot_claims_enabled=False)
        self.run_create(self.wednesday, {**self.BODY, 'claim_id': claim_id})
        self.assertIsNone(SlotClaim.all_objects.get().consumed_at)

    def test_team_sunshine(self):
        Organization.objects.filter(pk=self.sunshine.pk).update(slot_claims_enabled=True)
        RepCountOverride.all_objects.create(organization=self.sunshine, date=DAY, time_block='morning', count=0)
        lead = self.run_create(self.s_tenant, {**self.BODY, 'claim_id': '00000000-0000-0000-0000-000000000000'})
        self.assertEqual(lead.organization_id, self.sunshine.id)

    def test_key_with_no_org(self):
        lead = self.run_create(self.no_org, self.BODY)
        self.assertEqual(lead.organization_id, self.sunshine.id)     # api_key_required's fallback, as before


@requires_postgres
class OverCapacityTriggerTests(Base):
    """Moving or cancelling a lead marked over_capacity clears the mark (the
    migration 0046 trigger, on every write path). A NULL is never touched."""

    def row(self, lead):
        return Lead.all_objects.filter(pk=lead.pk).values().get()

    def test_a_put_that_moves_or_cancels_clears_the_mark(self):
        moved = self.lead(et(2026, 12, 1, 10), over_capacity=True)
        notes = self.lead(et(2026, 12, 1, 10), over_capacity=True)
        cancelled = self.lead(et(2026, 12, 1, 10), over_capacity=True)
        kept = self.lead(et(2026, 12, 1, 10), over_capacity=False)
        put = lambda lead, body: call('put', f'/api/v1/leads/{lead.pk}/', self.wednesday, body)  # noqa: E731
        self.assertEqual(put(moved, {'appointment_datetime': '2026-12-02T13:00:00-05:00'}).status_code, 200)
        put(notes, {'appt_notes': 'gate code 1234'})
        put(cancelled, {'cancelled': True})
        put(kept, {'appointment_datetime': '2026-12-02T13:00:00-05:00'})
        self.assertEqual([self.row(x)['over_capacity'] for x in (moved, notes, cancelled, kept)],
                         [None, True, None, False])
        # Every other write path too: a queryset update.
        Lead.all_objects.filter(pk=notes.pk).update(appointment_datetime=et(2026, 12, 3, 9))
        self.assertIsNone(self.row(notes)['over_capacity'])

    def test_team_sunshine_rows_are_unchanged(self):
        lead = self.lead(et(2026, 12, 1, 10), org=self.sunshine, homeowner_name='Sam')
        before = self.row(lead)
        resp = call('put', f'/api/v1/leads/{lead.pk}/', self.s_tenant,
                    {'appointment_datetime': '2026-12-02T13:00:00-05:00', 'cancelled': True})
        self.assertEqual(resp.status_code, 200)
        after = self.row(lead)
        changed = {k for k in before if before[k] != after[k]}
        self.assertEqual(changed, {'appointment_datetime', 'cancelled', 'updated_at'})
        self.assertIsNone(after['over_capacity'])


# ── The switch, the columns and the CRM badge ─────────────────────────────

class CommandTests(TestCase):
    def test_on_off_ttl_and_step_for_ventana(self):
        out = StringIO()
        call_command('slot_claims', 'ventana', 'on', '--ttl-hours', '12', '--step-minutes', '15',
                     '--full-policy', 'flag', stdout=out)
        org = Organization.objects.get(slug='ventana')
        self.assertEqual((org.slot_claims_enabled, org.slot_claim_ttl_hours, org.slot_start_step_minutes,
                          org.slot_full_policy), (True, 12, 15, 'flag'))
        self.assertIn('were off, claims hold 24 h, starts every 30 min, a full block: refuse, '
                      'now on, claims hold 12 h, starts every 15 min, a full block: flag', out.getvalue())
        call_command('slot_claims', 'ventana', 'off', stdout=StringIO())
        self.assertFalse(Organization.objects.get(slug='ventana').slot_claims_enabled)

    def test_refuses_any_other_organization(self):
        before = list(Organization.objects.filter(slug='team-sunshine').values())
        with self.assertRaises(CommandError):
            call_command('slot_claims', 'team-sunshine', 'on', stdout=StringIO())
        self.assertEqual(list(Organization.objects.filter(slug='team-sunshine').values()), before)

    def test_refuses_silly_values(self):
        for args in (('--ttl-hours', '0'), ('--ttl-hours', '337'), ('--step-minutes', '4'),
                     ('--step-minutes', '181'), ('--full-policy', 'allow')):
            with self.assertRaises((CommandError, SystemExit)):
                call_command('slot_claims', 'ventana', *args, stdout=StringIO())

    def test_new_columns_default_off(self):
        for org in Organization.objects.all():
            self.assertEqual((org.slot_claims_enabled, org.slot_claim_ttl_hours, org.slot_start_step_minutes,
                              org.slot_full_policy), (False, 24, 30, 'refuse'))

    def test_older_code_can_still_insert_an_organization(self):
        # The database defaults: an INSERT that names none of the new columns.
        with connection.cursor() as cursor:
            cursor.execute(
                'INSERT INTO maps_organization (name, slug, is_active, is_default_inbound, alfred_declines, '
                'reminders_by_wednesday, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)',
                ['Old Code Co', 'old-code', True, False, False, False, timezone.now()])
        org = Organization.objects.get(slug='old-code')
        self.assertEqual((org.slot_claims_enabled, org.slot_claim_ttl_hours, org.slot_start_step_minutes,
                          org.slot_full_policy), (False, 24, 30, 'refuse'))


class BadgeTests(Base):
    def test_crm_and_daily_show_over_capacity_only_when_true(self):
        user = User.objects.create_user('v_manager', password='pw')
        UserProfile.all_objects.create(user=user, role='manager', organization=self.ventana)
        self.lead(et(2026, 12, 1, 10), homeowner_name='Over', over_capacity=True)
        self.lead(et(2026, 12, 1, 11), homeowner_name='Fine', over_capacity=False)
        client = Client()
        client.force_login(user)
        for path in ('/crm/', f'/daily/?date={DAY}'):
            html = client.get(path).content.decode()
            self.assertEqual(html.count('OVER CAPACITY'), 1, path)


# ── Concurrency (PostgreSQL only) ─────────────────────────────────────────

@requires_postgres
class ConcurrencyTests(NoSends, TransactionTestCase):
    """Real transactions in real threads. Counting a block's leads is slowed
    down, so without the locks both requests would count the block open."""
    serialized_rollback = True
    DAY = '2026-12-03'

    def setUp(self):
        super().setUp()
        self.ventana = Organization.objects.get(slug='ventana')
        Organization.objects.filter(pk=self.ventana.pk).update(slot_claims_enabled=True)
        self.lurch = APITenant.objects.create(name='Lurch', organization=self.ventana)
        self.wednesday = APITenant.objects.create(name='Wednesday', organization=self.ventana)
        for block in ('morning', 'afternoon'):
            RepCountOverride.all_objects.create(organization=self.ventana, date=self.DAY, time_block=block,
                                                count=1)
        slow = availability._leads_in

        def slow_leads_in(day, key):
            time.sleep(0.3)
            return slow(day, key)
        patcher = mock.patch('maps.availability._leads_in', side_effect=slow_leads_in)
        patcher.start()
        self.addCleanup(patcher.stop)

    def together(self, *requests):
        barrier = threading.Barrier(len(requests))
        results = [None] * len(requests)

        def run(i, fn):
            try:
                barrier.wait()
                results[i] = fn()
            finally:
                connection.close()
        threads = [threading.Thread(target=run, args=(i, fn)) for i, fn in enumerate(requests)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results

    def book(self, session, key=None, block='morning'):
        def go():
            return call('post', BOOK, self.lurch, {'date': self.DAY, 'time_block': block,
                                                   'idempotency_key': key or f'{session}:{block}',
                                                   'session_ref': sid(session)})
        return go

    def push(self, unlocked=None):
        def go():
            if unlocked is not None:
                unlocked.on = True
            return call('post', CREATE, self.wednesday, {
                'address': '9 Hub St', 'phone_number': '9785550101', 'hub_customer_id': 'wednesday:x',
                'appointment_datetime': f'{self.DAY}T10:00:00-05:00'})
        return go

    def live_for(self, session):
        return sum(1 for c in SlotClaim.all_objects.filter(session_ref=sid(session)) if c.state(NOW) == 'live')

    def held_at_write(self):
        """For the control runs: each side waits at its write (the claim's
        INSERT, the lead's save) until the other has counted too, so both
        decisions are made on the same, empty block."""
        at_write = threading.Barrier(2, timeout=10)
        real_save, real_create = Lead.save, SlotClaim.objects.create

        def save(lead, *args, **kwargs):
            at_write.wait()
            return real_save(lead, *args, **kwargs)

        def create(**kwargs):
            at_write.wait()
            return real_create(**kwargs)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(Lead, 'save', autospec=True, side_effect=save))
        stack.enter_context(mock.patch.object(SlotClaim.objects, 'create', side_effect=create))
        return stack

    def test_two_setters_race_for_the_last_spot(self):
        a, b = self.together(self.book('a'), self.book('b'))
        self.assertEqual(sorted([a.status_code, b.status_code]), [201, 409])
        self.assertEqual(SlotClaim.all_objects.count(), 1)
        self.assertNothingSent()

    def test_two_setters_race_for_a_block_locked_before(self):
        # The lock row already exists (the block was claimed before), so the
        # second request waits on SELECT ... FOR UPDATE, not on the INSERT.
        SlotBlockLock.all_objects.create(organization=self.ventana, date=self.DAY, time_block='morning')
        a, b = self.together(self.book('a'), self.book('b'))
        self.assertEqual(sorted([a.status_code, b.status_code]), [201, 409])
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_control_the_race_is_real_without_the_lock(self):
        # The harness itself: with the block lock taken out, both get through.
        with mock.patch('maps.availability._lock', side_effect=lambda day, key: None), self.held_at_write():
            a, b = self.together(self.book('a'), self.book('b'))
        self.assertEqual([a.status_code, b.status_code], [201, 201])
        self.assertEqual(SlotClaim.all_objects.count(), 2)

    def test_one_session_claiming_two_blocks_at_once_keeps_one(self):
        a, b = self.together(self.book('s', block='morning'), self.book('s', block='afternoon'))
        self.assertEqual([a.status_code, b.status_code], [201, 201])
        self.assertEqual(self.live_for('s'), 1)

    def test_same_key_twice_at_once_makes_one_claim(self):
        RepCountOverride.all_objects.filter(date=self.DAY).update(count=2)
        a, b = self.together(self.book('a', 'k'), self.book('a', 'k'))
        self.assertEqual(sorted([a.status_code, b.status_code]), [200, 201])
        self.assertEqual(a.json()['claim_id'], b.json()['claim_id'])
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_same_key_for_two_blocks_at_once(self):
        a, b = self.together(self.book('a', 'K', 'morning'), self.book('a', 'K', 'afternoon'))
        self.assertEqual(sorted([a.status_code, b.status_code]), [201, 422])
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_same_key_for_two_blocks_from_two_sessions_at_once(self):
        # No shared session lock: the unique key decides.
        a, b = self.together(self.book('c', 'K', 'morning'), self.book('d', 'K', 'afternoon'))
        self.assertEqual(sorted([a.status_code, b.status_code]), [201, 422])
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_a_claim_and_a_pushed_lead_race_for_the_last_spot(self):
        claim, created = self.together(self.book('a'), self.push())
        self.assertEqual(created.status_code, 201)
        lead = Lead.all_objects.get(pk=created.json()['id'])
        # Exactly one of them got the spot: the claim, or the lead.
        if claim.status_code == 201:
            self.assertIs(lead.over_capacity, True)
        else:
            self.assertEqual(claim.status_code, 409)
            self.assertIs(lead.over_capacity, False)

    def test_control_without_the_lead_paths_lock_both_get_the_spot(self):
        # The lead path's block lock removed (the claim keeps its own), and
        # both sides held at their write until the other has counted: both
        # see the last spot free. The test above would fail.
        unlocked = threading.local()
        real_lock = availability._lock

        def lock(day, key):
            if not getattr(unlocked, 'on', False):
                real_lock(day, key)
        with mock.patch('maps.availability._lock', side_effect=lock), self.held_at_write():
            claim, created = self.together(self.book('a'), self.push(unlocked))
        lead = Lead.all_objects.get(pk=created.json()['id'])
        self.assertEqual((claim.status_code, lead.over_capacity), (201, False))
