"""Slot availability and claims (maps/availability.py, INTEGRATION.md §6.7).

The concurrency tests need PostgreSQL (production's engine): on SQLite,
SELECT ... FOR UPDATE does nothing, so they skip. Run the suite against both:
    python3 manage.py test maps
    DATABASE_URL=postgres://… python3 manage.py test maps
"""
import json
import threading
import time
import unittest
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
DAY = '2026-12-01'          # a Tuesday; claims have no date window


def et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=EASTERN)


def call(method, path, tenant, body=None):
    headers = {'HTTP_AUTHORIZATION': f'Bearer {tenant.api_key}'}
    if method == 'get':
        return Client().get(path, **headers)
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
    """Fails the test if anything is sent: SMS, webhook, any HTTP request."""

    def setUp(self):
        super().setUp()
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
                'session_ref': session, **extra}
        return call('post', BOOK, tenant or self.lurch, body)

    def week(self, start=DAY, days=None, tenant=None):
        path = f'{WEEK}?week_start={start}' + (f'&days={days}' if days is not None else '')
        return call('get', path, tenant or self.lurch)

    def block(self, day, key):
        days = 1
        rows = self.week(day, days).json()['availability']
        return next(r for r in rows if r['date'] == day and r['time_block'] == key)


# ── The gate ──────────────────────────────────────────────────────────────

class GateTests(Base):
    def requests(self, tenant):
        return [
            self.week(tenant=tenant),
            self.book(tenant=tenant),
            call('post', RELEASE, tenant, {'session_ref': 's1'}),
        ]

    def test_team_sunshine_key_gets_403_and_nothing_is_created(self):
        before = counts()
        for resp in self.requests(self.s_tenant):
            self.assertEqual(resp.status_code, 403)
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

    def test_no_key_or_bad_key_is_401(self):
        self.assertEqual(Client().get(f'{WEEK}?week_start={DAY}').status_code, 401)
        resp = Client().get(f'{WEEK}?week_start={DAY}',
                            HTTP_AUTHORIZATION='Bearer 00000000-0000-0000-0000-000000000000')
        self.assertEqual(resp.status_code, 401)

    def test_wrong_methods_are_405(self):
        self.assertEqual(call('post', WEEK, self.lurch, {}).status_code, 405)
        self.assertEqual(call('get', BOOK, self.lurch).status_code, 405)
        self.assertEqual(call('get', RELEASE, self.lurch).status_code, 405)


# ── GET availability ──────────────────────────────────────────────────────

class WeekTests(Base):
    def test_shape_and_capacity(self):
        resp = self.week(DAY)
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()['availability']
        self.assertEqual(len(rows), 7 * len(TIME_BLOCKS))
        first = rows[0]
        self.assertEqual(first, {
            'date': DAY, 'time_block': 'morning', 'label': '9-12 PM',
            'start': '2026-12-01T09:00:00-05:00', 'end': '2026-12-01T12:00:00-05:00',
            'rep_count': 2, 'booked': 0, 'open': 2, 'leads': 0, 'claims': 0,
        })
        self.assertNothingSent()

    def test_booked_is_leads_plus_live_claims_only(self):
        self.capacity(DAY, 'afternoon', 3)
        self.lead(et(2026, 12, 1, 15, 30))
        self.lead(et(2026, 12, 1, 16), cancelled=True)                  # cancelled: frees its spot
        self.lead(et(2026, 12, 1, 15, 30), org=self.sunshine)           # another org: never seen
        self.assertEqual(self.book('a', 'afternoon').status_code, 201)  # live
        now = timezone.now()
        with mock.patch('maps.availability.timezone.now', return_value=now):
            for state in ('released', 'consumed', 'expired'):
                SlotClaim.all_objects.create(
                    organization=self.ventana, date=DAY, time_block='afternoon', idempotency_key=state,
                    session_ref=state, expires_at=now - timedelta(seconds=1) if state == 'expired'
                    else now + timedelta(hours=1),
                    released_at=now if state == 'released' else None,
                    consumed_at=now if state == 'consumed' else None)
            row = self.block(DAY, 'afternoon')
        self.assertEqual((row['rep_count'], row['leads'], row['claims'], row['booked'], row['open']),
                         (3, 1, 1, 2, 1))

    def test_override_zero_blocks_the_block(self):
        self.capacity(DAY, 'evening', 0)
        row = self.block(DAY, 'evening')
        self.assertEqual((row['rep_count'], row['open']), (0, 0))
        self.assertEqual(self.book('a', 'evening').json(), {'error': 'slot_full', 'date': DAY,
                                                            'time_block': 'evening', 'open': 0})

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
            self.assertEqual(self.week(DAY, bad).status_code, 400)
        self.assertEqual(call('get', WEEK, self.lurch).status_code, 400)
        self.assertEqual(call('get', f'{WEEK}?week_start=12/01/2026', self.lurch).status_code, 400)


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
        rows = self.week('2026-10-31', 2).json()['availability']
        self.assertEqual([r['start'] for r in rows if r['time_block'] == 'morning'],
                         ['2026-10-31T09:00:00-04:00', '2026-11-01T09:00:00-05:00'])
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
        self.capacity('2026-11-01', 'morning', 1)
        self.assertEqual(self.book('a', 'morning', '2026-11-01').status_code, 201)
        self.assertEqual(self.book('b', 'morning', '2026-11-01').status_code, 409)
        self.capacity('2027-03-14', 'evening', 1)
        self.assertEqual(self.book('a', 'evening', '2027-03-14').status_code, 201)
        self.assertEqual(self.book('b', 'evening', '2027-03-14').status_code, 409)


# ── POST book / release ───────────────────────────────────────────────────

class BookTests(Base):
    def test_claim_then_full(self):
        self.capacity(DAY, 'morning', 1)
        resp = self.book('a', phone='(978) 555-0101')
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        claim = SlotClaim.all_objects.get()
        self.assertEqual(body['claim_id'], str(claim.claim_id))
        self.assertEqual((body['date'], body['time_block'], body['open_after'], body['state'], body['released']),
                         (DAY, 'morning', 0, 'live', []))
        self.assertEqual((claim.organization_id, claim.session_ref, claim.phone10),
                         (self.ventana.id, 'a', '9785550101'))
        full = self.book('b')
        self.assertEqual(full.status_code, 409)
        self.assertEqual(full.json()['error'], 'slot_full')
        self.assertEqual(SlotClaim.all_objects.count(), 1)
        self.assertEqual(self.block(DAY, 'morning')['open'], 0)
        self.assertNothingSent()

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
        # The same key for another block is a client bug, not a second claim.
        other = self.book('a', 'midday', key='k-1')
        self.assertEqual(other.status_code, 422)
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_replay_still_answers_when_the_block_is_full(self):
        self.capacity(DAY, 'morning', 1)
        first = self.book('a', key='k-1')
        again = self.book('a', key='k-1')
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()['claim_id'], first.json()['claim_id'])

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
        second = self.book('a', key='k-2')
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()['released'], [first['claim_id']])
        self.assertEqual(self.block(DAY, 'morning')['claims'], 1)

    def test_a_full_block_keeps_the_sessions_earlier_claim(self):
        self.capacity(DAY, 'afternoon', 0)
        first = self.book('a', 'morning').json()
        self.assertEqual(self.book('a', 'afternoon').status_code, 409)
        self.assertEqual(SlotClaim.all_objects.get(claim_id=first['claim_id']).state(timezone.now()), 'live')

    def test_other_sessions_are_untouched(self):
        a = self.book('a', 'morning').json()
        self.book('b', 'afternoon')
        self.assertIsNone(SlotClaim.all_objects.get(claim_id=a['claim_id']).released_at)

    def test_ttl_comes_from_the_organization_and_expiry_frees_the_spot(self):
        self.capacity(DAY, 'morning', 1)
        now = timezone.now()
        with mock.patch('maps.availability.timezone.now', return_value=now):
            body = self.book('a').json()
        claim = SlotClaim.all_objects.get()
        self.assertEqual(claim.expires_at - claim.created_at, timedelta(hours=24))
        self.assertEqual(datetime.fromisoformat(body['expires_at']), now + timedelta(hours=24))
        just_before = now + timedelta(hours=24) - timedelta(seconds=1)
        with mock.patch('maps.availability.timezone.now', return_value=just_before):
            self.assertEqual(self.block(DAY, 'morning')['open'], 0)
            self.assertEqual(self.book('b').status_code, 409)
        at_expiry = now + timedelta(hours=24)
        with mock.patch('maps.availability.timezone.now', return_value=at_expiry):
            self.assertEqual(self.block(DAY, 'morning')['open'], 1)
            self.assertEqual(self.book('b').status_code, 201)

        Organization.objects.filter(pk=self.ventana.pk).update(slot_claim_ttl_hours=3)
        self.book('c', 'midday')
        claim = SlotClaim.all_objects.get(session_ref='c')
        self.assertEqual(claim.expires_at - claim.created_at, timedelta(hours=3))

    def test_bad_bodies_are_400_and_create_nothing(self):
        before = counts()
        for body in ({'date': '12/01/2026', 'time_block': 'morning', 'idempotency_key': 'k', 'session_ref': 's'},
                     {'date': DAY, 'time_block': 'night', 'idempotency_key': 'k', 'session_ref': 's'},
                     {'date': DAY, 'time_block': 'morning', 'session_ref': 's'},
                     {'date': DAY, 'time_block': 'morning', 'idempotency_key': 'k'},
                     {'date': DAY, 'time_block': 'morning', 'idempotency_key': 'k' * 101, 'session_ref': 's'},
                     {'date': DAY, 'time_block': 'morning', 'idempotency_key': '  ', 'session_ref': 's'},
                     ['not', 'an', 'object']):
            self.assertEqual(call('post', BOOK, self.lurch, body).status_code, 400, body)
        resp = Client().post(BOOK, data='{nope', content_type='application/json',
                             HTTP_AUTHORIZATION=f'Bearer {self.lurch.api_key}')
        self.assertEqual(resp.status_code, 400)
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
        resp = call('post', RELEASE, self.lurch, {'session_ref': 'a'})
        self.assertEqual(len(resp.json()['released']), 1)
        self.assertIsNone(SlotClaim.all_objects.get(session_ref='b').released_at)

    def test_a_consumed_claim_is_never_released(self):
        claim_id = self.book('a').json()['claim_id']
        SlotClaim.all_objects.update(consumed_at=timezone.now())
        self.assertEqual(call('post', RELEASE, self.lurch, {'claim_id': claim_id}).json(), {'released': []})
        self.assertIsNone(SlotClaim.all_objects.get().released_at)

    def test_bad_requests(self):
        self.assertEqual(call('post', RELEASE, self.lurch, {}).status_code, 400)
        self.assertEqual(call('post', RELEASE, self.lurch, {'claim_id': 'x', 'session_ref': 'a'}).status_code, 400)
        self.assertEqual(call('post', RELEASE, self.lurch, {'claim_id': 'not-a-uuid'}).status_code, 400)
        missing = call('post', RELEASE, self.lurch, {'claim_id': '00000000-0000-0000-0000-000000000000'})
        self.assertEqual(missing.status_code, 404)

    def test_another_orgs_claim_is_not_found(self):
        other = SlotClaim.all_objects.create(organization=self.sunshine, date=DAY, time_block='morning',
                                             idempotency_key='k', session_ref='a',
                                             expires_at=timezone.now() + timedelta(hours=1))
        self.assertEqual(call('post', RELEASE, self.lurch, {'claim_id': str(other.claim_id)}).status_code, 404)
        self.assertEqual(call('post', RELEASE, self.lurch, {'session_ref': 'a'}).json(), {'released': []})
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

    def test_a_live_claim_of_someone_else_fills_the_block(self):
        self.capacity(DAY, 'morning', 1)
        self.book('a', phone='5550000000')
        lead = self.assertCreated(self.create())
        self.assertIs(lead.over_capacity, True)
        self.assertIsNone(SlotClaim.all_objects.get().consumed_at)

    def test_room_left_is_not_over_capacity(self):
        self.capacity(DAY, 'morning', 2)
        self.lead(et(2026, 12, 1, 9, 30))
        self.assertIs(self.assertCreated(self.create()).over_capacity, False)

    def test_phone_fallback_consumes_the_matching_claim(self):
        self.capacity(DAY, 'morning', 1)
        self.book('a', phone='+1 (978) 555-0101')
        lead = self.assertCreated(self.create(phone_number='978-555-0101'))
        self.assertIs(lead.over_capacity, False)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, lead.id)

    def test_unknown_or_malformed_claim_id_is_checked_like_no_claim(self):
        self.capacity(DAY, 'morning', 1)
        self.lead(et(2026, 12, 1, 9, 30))
        for claim_id in ('00000000-0000-0000-0000-000000000000', 'garbage', 12, ['x']):
            Lead.all_objects.filter(hub_customer_id='wednesday:abc').delete()
            self.sends['maps.views.fire_webhooks'].reset_mock()
            self.assertIs(self.assertCreated(self.create(claim_id=claim_id)).over_capacity, True)

    def test_claim_for_another_block_is_consumed_and_this_block_is_checked(self):
        self.capacity(DAY, 'morning', 1)
        self.capacity(DAY, 'evening', 1)
        self.lead(et(2026, 12, 1, 9, 30))
        claim_id = self.book('a', 'evening', phone='5550000000').json()['claim_id']
        lead = self.assertCreated(self.create(claim_id=claim_id))     # the form says 10:00, not evening
        self.assertIs(lead.over_capacity, True)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, lead.id)
        self.assertEqual(self.block(DAY, 'evening')['open'], 1)

    def test_an_expired_claim_no_longer_holds_the_spot(self):
        self.capacity(DAY, 'morning', 1)
        claim_id = self.book('a').json()['claim_id']
        SlotClaim.all_objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.lead(et(2026, 12, 1, 9, 30))                     # took the freed spot
        lead = self.assertCreated(self.create(claim_id=claim_id))
        self.assertIs(lead.over_capacity, True)
        self.assertEqual(SlotClaim.all_objects.get().consumed_by_lead_id, lead.id)

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
        self.assertFalse([s for s in sql if 'maps_slot' in s], 'touched a slot table')
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


# ── The switch and the CRM badge ──────────────────────────────────────────

class CommandTests(TestCase):
    def test_on_off_and_ttl_for_ventana(self):
        out = StringIO()
        call_command('slot_claims', 'ventana', 'on', '--ttl-hours', '12', stdout=out)
        org = Organization.objects.get(slug='ventana')
        self.assertEqual((org.slot_claims_enabled, org.slot_claim_ttl_hours), (True, 12))
        self.assertIn('were off, claims hold 24 h, now on, claims hold 12 h', out.getvalue())
        call_command('slot_claims', 'ventana', 'off', stdout=StringIO())
        self.assertFalse(Organization.objects.get(slug='ventana').slot_claims_enabled)

    def test_refuses_any_other_organization(self):
        before = list(Organization.objects.filter(slug='team-sunshine').values())
        with self.assertRaises(CommandError):
            call_command('slot_claims', 'team-sunshine', 'on', stdout=StringIO())
        self.assertEqual(list(Organization.objects.filter(slug='team-sunshine').values()), before)

    def test_refuses_a_silly_ttl(self):
        for ttl in ('0', '337'):
            with self.assertRaises(CommandError):
                call_command('slot_claims', 'ventana', '--ttl-hours', ttl, stdout=StringIO())

    def test_new_columns_default_off(self):
        for org in Organization.objects.all():
            self.assertEqual((org.slot_claims_enabled, org.slot_claim_ttl_hours), (False, 24))


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

@unittest.skipUnless(connection.vendor == 'postgresql', 'SELECT ... FOR UPDATE is a no-op on SQLite')
class ConcurrencyTests(NoSends, TransactionTestCase):
    """Real transactions in real threads. Counting a block's leads is slowed
    down, so without the lock both requests would count the block open."""
    serialized_rollback = True
    DAY = '2026-12-03'

    def setUp(self):
        super().setUp()
        self.ventana = Organization.objects.get(slug='ventana')
        Organization.objects.filter(pk=self.ventana.pk).update(slot_claims_enabled=True)
        self.lurch = APITenant.objects.create(name='Lurch', organization=self.ventana)
        self.wednesday = APITenant.objects.create(name='Wednesday', organization=self.ventana)
        RepCountOverride.all_objects.create(organization=self.ventana, date=self.DAY, time_block='morning',
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

    def book(self, session, key=None):
        def go():
            return call('post', BOOK, self.lurch, {'date': self.DAY, 'time_block': 'morning',
                                                   'idempotency_key': key or session, 'session_ref': session})
        return go

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

    def test_the_race_is_real_without_the_lock(self):
        # The harness itself: with the lock taken out, both get through.
        with mock.patch('maps.availability._lock', side_effect=lambda day, key: None):
            a, b = self.together(self.book('a'), self.book('b'))
        self.assertEqual([a.status_code, b.status_code], [201, 201])
        self.assertEqual(SlotClaim.all_objects.count(), 2)

    def test_same_key_twice_at_once_makes_one_claim(self):
        RepCountOverride.all_objects.filter(date=self.DAY).update(count=2)
        a, b = self.together(self.book('a', 'k'), self.book('a', 'k'))
        self.assertEqual(sorted([a.status_code, b.status_code]), [200, 201])
        self.assertEqual(a.json()['claim_id'], b.json()['claim_id'])
        self.assertEqual(SlotClaim.all_objects.count(), 1)

    def test_a_claim_and_a_pushed_lead_race_for_the_last_spot(self):
        def push():
            return call('post', CREATE, self.wednesday, {
                'address': '9 Hub St', 'phone_number': '9785550101', 'hub_customer_id': 'wednesday:x',
                'appointment_datetime': f'{self.DAY}T10:00:00-05:00'})
        claim, created = self.together(self.book('a'), push)
        self.assertEqual(created.status_code, 201)
        lead = Lead.all_objects.get(pk=created.json()['id'])
        # Exactly one of them got the spot: the claim, or the lead.
        if claim.status_code == 201:
            self.assertIs(lead.over_capacity, True)
        else:
            self.assertEqual(claim.status_code, 409)
            self.assertIs(lead.over_capacity, False)
