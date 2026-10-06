"""Geocoding says WHY it failed.

From about 2026-10-02 nearly every new lead in both orgs came back without
coordinates, and the log said only 'Geocode failed for "<address>"':
_nominatim_search swallowed every exception, so a refusal from Nominatim
(blocked, throttled) read exactly like an address nobody could find. These hold
the logging that tells them apart. No network: urlopen is replaced.
"""
import io
import json
import urllib.error
from unittest import mock

from django.test import SimpleTestCase

from maps.views import geocode


def _http_error(code, body):
    return urllib.error.HTTPError(
        'https://nominatim.openstreetmap.org/search', code, 'refused', hdrs=None, fp=io.BytesIO(body.encode()))


class _Resp:
    status = 200

    def __init__(self, payload=None, raw=None):
        self._raw = raw if raw is not None else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@mock.patch('time.sleep', lambda *_: None)
class GeocodeLogsWhyTest(SimpleTestCase):
    def test_a_refusal_is_logged_with_its_status_and_body(self):
        err = _http_error(403, '<html>Access blocked: usage policy</html>')
        with mock.patch('urllib.request.urlopen', side_effect=err), \
                self.assertLogs('geocode', level='WARNING') as logs:
            self.assertEqual(geocode('42 Thomas St, Northbridge, MA 01534'), (None, None))
        text = '\n'.join(logs.output)
        self.assertIn('Nominatim HTTP 403', text)
        self.assertIn('Access blocked: usage policy', text)
        # …and the final line says what every attempt got back.
        self.assertRegex(text, r'Geocode failed for "42 Thomas St, Northbridge, MA 01534" — .*HTTP 403')

    def test_a_throttle_says_429(self):
        with mock.patch('urllib.request.urlopen', side_effect=_http_error(429, 'Too Many Requests')), \
                self.assertLogs('geocode', level='WARNING') as logs:
            geocode('668 burncoat st worcester')
        self.assertTrue(any('Nominatim HTTP 429' in line and 'Too Many Requests' in line for line in logs.output), logs.output)

    def test_a_network_error_is_logged_with_its_type(self):
        with mock.patch('urllib.request.urlopen', side_effect=urllib.error.URLError('timed out')), \
                self.assertLogs('geocode', level='WARNING') as logs:
            geocode('20 N Munroe Terrace dorchester ma')
        self.assertTrue(any('Nominatim request failed' in line and 'URLError' in line for line in logs.output), logs.output)

    def test_no_results_is_not_a_refusal(self):
        # An empty answer is Nominatim not finding it — logged once, in the
        # final line, not as an HTTP problem.
        with mock.patch('urllib.request.urlopen', return_value=_Resp([])), \
                self.assertLogs('geocode', level='WARNING') as logs:
            self.assertEqual(geocode('1 Nowhere Lane, Atlantis'), (None, None))
        self.assertEqual(len(logs.output), 1, logs.output)
        self.assertIn('no results', logs.output[0])
        self.assertNotIn('HTTP', logs.output[0])

    def test_an_answer_outside_massachusetts_is_reported_as_such(self):
        # Coordinates came back — they were rejected, not missing. The final
        # line must say so, not "no results" or "no attempts".
        with mock.patch('urllib.request.urlopen', return_value=_Resp([{'lat': '40.71', 'lon': '-74.0'}])), \
                self.assertLogs('geocode', level='WARNING') as logs:
            self.assertEqual(geocode('12 Main St, Springfield'), (None, None))
        self.assertIn('outside MA (40.71, -74.0)', logs.output[-1])
        self.assertNotIn('no attempts', logs.output[-1])

    def test_a_non_json_200_is_logged_with_its_status_and_body(self):
        page = b'<html><body>Access denied. Captcha required.</body></html>'
        with mock.patch('urllib.request.urlopen', return_value=_Resp(raw=page)), \
                self.assertLogs('geocode', level='WARNING') as logs:
            self.assertEqual(geocode('42 Thomas St, Northbridge, MA 01534'), (None, None))
        self.assertTrue(any('Nominatim non-JSON reply (HTTP 200)' in line and 'Captcha required' in line for line in logs.output), logs.output)

    def test_a_found_address_logs_nothing(self):
        with mock.patch('urllib.request.urlopen', return_value=_Resp([{'lat': '42.1619849', 'lon': '-71.6640266'}])), \
                self.assertNoLogs('geocode', level='WARNING'):
            self.assertEqual(geocode('42 Thomas St, Northbridge, MA 01534'), (42.1619849, -71.6640266))


class NominatimPolicyTest(SimpleTestCase):
    """Nominatim's usage policy: an identifying User-Agent, at most one request
    a second, and backing off when told to (2026-10-06)."""

    def setUp(self):
        import maps.views as v
        self.v = v
        v._nominatim_last = 0.0
        self.sleeps = []
        self.clock = [1000.0]
        self.patches = [
            mock.patch.object(v._time, 'sleep', side_effect=lambda s: (self.sleeps.append(s), self.clock.__setitem__(0, self.clock[0] + s))),
            mock.patch.object(v._time, 'monotonic', side_effect=lambda: self.clock[0]),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _ok(self, lat='42.1', lon='-71.5'):
        return _Resp([{'lat': lat, 'lon': lon}])

    def test_the_user_agent_names_the_app_and_its_contact(self):
        seen = []
        def fake(req, timeout=None):
            seen.append(req.get_header('User-agent'))
            return self._ok()
        with mock.patch('urllib.request.urlopen', side_effect=fake), mock.patch.dict('os.environ', {'NOMINATIM_CONTACT_EMAIL': 'ops@example.com'}):
            geocode('42 Thomas St, Northbridge, MA')
        self.assertEqual(seen, ['Sutton/1.0 (+https://sutton-soda.com; ops@example.com)'])
        seen.clear()
        with mock.patch('urllib.request.urlopen', side_effect=fake), mock.patch.dict('os.environ', {}, clear=False):
            import os
            os.environ.pop('NOMINATIM_CONTACT_EMAIL', None)
            geocode('42 Thomas St, Northbridge, MA')
        self.assertEqual(seen, ['Sutton/1.0 (+https://sutton-soda.com)'])

    def test_requests_are_a_second_apart_across_strategies_and_calls(self):
        times = []
        def fake(req, timeout=None):
            times.append(self.clock[0])
            return _Resp([])                       # every strategy runs
        with mock.patch('urllib.request.urlopen', side_effect=fake), self.assertLogs('geocode', level='WARNING'):
            geocode('1 Nowhere Ln, Atlantis')
            geocode('2 Nowhere Ln, Atlantis')
        gaps = [b - a for a, b in zip(times, times[1:])]
        self.assertTrue(len(times) >= 4 and all(g >= 1.0 for g in gaps), (times, gaps))

    def test_a_429_is_retried_after_a_wait_and_can_then_succeed(self):
        calls = iter([_http_error(429, 'Too Many Requests'), self._ok('42.2', '-71.6')])
        def fake(req, timeout=None):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch('urllib.request.urlopen', side_effect=fake), self.assertLogs('geocode', level='WARNING') as logs:
            self.assertEqual(geocode('42 Thomas St, Northbridge, MA'), (42.2, -71.6))
        self.assertIn(2, self.sleeps)               # the backoff
        self.assertTrue(any('retrying in 2s' in line for line in logs.output), logs.output)

    def test_retry_after_is_honoured_and_capped(self):
        import email.message
        def err(after):
            h = email.message.Message()
            h['Retry-After'] = after
            return urllib.error.HTTPError('u', 503, 'busy', hdrs=h, fp=io.BytesIO(b''))
        calls = iter([err('7'), err('600'), self._ok()])
        def fake(req, timeout=None):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch('urllib.request.urlopen', side_effect=fake), self.assertLogs('geocode', level='WARNING'):
            geocode('42 Thomas St, Northbridge, MA')
        self.assertIn(7, self.sleeps)
        self.assertIn(10, self.sleeps)              # 600 asked for, 10 at most
        self.assertNotIn(600, self.sleeps)

    def test_a_403_is_not_retried(self):
        n = []
        def fake(req, timeout=None):
            n.append(1)
            raise _http_error(403, 'Access blocked')
        with mock.patch('urllib.request.urlopen', side_effect=fake), self.assertLogs('geocode', level='WARNING'):
            geocode('42 Thomas St, Northbridge, MA')
        # one request per strategy (city fallback included), never a retry of the same query
        self.assertLessEqual(len(n), 3)
        self.assertNotIn(2, self.sleeps)

    def test_a_timeout_is_retried(self):
        calls = iter([urllib.error.URLError('timed out'), self._ok()])
        def fake(req, timeout=None):
            r = next(calls)
            if isinstance(r, Exception):
                raise r
            return r
        with mock.patch('urllib.request.urlopen', side_effect=fake), self.assertLogs('geocode', level='WARNING'):
            self.assertEqual(geocode('42 Thomas St, Northbridge, MA'), (42.1, -71.5))


from django.test import Client  # noqa: E402

from maps.tests import TenancyBase  # noqa: E402
from maps.models import Lead  # noqa: E402


class NoMapPinTest(TenancyBase):
    """A lead with no coordinates says so in words on the CRM list."""

    def test_the_crm_marks_a_lead_with_no_coordinates_and_only_that_one(self):
        pinned = Lead.all_objects.create(address='5 Pinned Pl', homeowner_name='Pia Pinned',
                                         latitude=42.3, longitude=-71.1, organization=self.ventana)
        client = Client()
        client.force_login(self.v_manager)
        html = client.get('/crm/').content.decode()
        self.assertEqual(html.count('<span class="no-pin"'), 1, 'one lead without coordinates, one badge')
        row_unpinned = html[html.index('Vera Ventana'):]
        self.assertIn('No map pin', row_unpinned[:row_unpinned.index('</tr>')])
        row_pinned = html[html.index('Pia Pinned'):]
        self.assertNotIn('No map pin', row_pinned[:row_pinned.index('</tr>')])
        self.assertIsNotNone(pinned.pk)


from datetime import timedelta  # noqa: E402
from io import StringIO  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.utils import timezone  # noqa: E402


class RegeocodeMissingTest(TenancyBase):
    """regeocode_missing writes the two coordinates and nothing else."""

    def _lead(self, org, address, city='', days_ago=0, lat=None, **kw):
        lead = Lead.all_objects.create(address=address, city=city, homeowner_name=address,
                                       latitude=lat, longitude=None if lat is None else -71.0, organization=org, **kw)
        Lead.all_objects.filter(pk=lead.pk).update(created_at=timezone.now() - timedelta(days=days_ago))
        return Lead.all_objects.get(pk=lead.pk)

    def _run(self, *args):
        out = StringIO()
        call_command('regeocode_missing', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        lead = self._lead(self.ventana, '42 Thomas St', 'Northbridge')
        with mock.patch('maps.management.commands.regeocode_missing.geocode', return_value=(42.16, -71.66)):
            out = self._run('--since', (timezone.now() - timedelta(days=1)).date().isoformat())
        self.assertIn('DRY RUN', out)
        self.assertIn(f'would set lead {lead.id}', out)
        lead.refresh_from_db()
        self.assertIsNone(lead.latitude)

    def test_apply_restores_both_orgs_and_changes_nothing_else(self):
        since = (timezone.now() - timedelta(days=2)).date().isoformat()
        v = self._lead(self.ventana, '42 Thomas St', 'Northbridge', disposition='', appt_notes='keep me')
        s = self._lead(self.sunshine, '557 Ware St')
        bad = self._lead(self.sunshine, '1 Nowhere Ln')
        old = self._lead(self.ventana, '9 Old Rd', days_ago=30)          # before --since: untouched
        has = self._lead(self.ventana, '5 Pinned Pl', lat=42.3)          # already has coordinates: untouched
        before = {f.name: getattr(v, f.name) for f in Lead._meta.concrete_fields if f.name not in ('latitude', 'longitude', 'updated_at')}
        queries = []
        def fake(q):
            queries.append(q)
            return (None, None) if 'Nowhere' in q else (42.16, -71.66)
        with mock.patch('maps.management.commands.regeocode_missing.geocode', side_effect=fake):
            out = self._run('--since', since, '--apply')
        # the same query creation builds: ", city, MA" only when there is a city
        self.assertIn('42 Thomas St, Northbridge, MA', queries)
        self.assertIn('557 Ware St', queries)
        self.assertNotIn('9 Old Rd', ' '.join(queries))
        self.assertNotIn('5 Pinned Pl', ' '.join(queries))
        for lead in (v, s, bad, old, has):
            lead.refresh_from_db()
        self.assertEqual((v.latitude, v.longitude), (42.16, -71.66))
        self.assertEqual((s.latitude, s.longitude), (42.16, -71.66))
        self.assertIsNone(bad.latitude)
        self.assertIsNone(old.latitude)
        self.assertEqual(has.latitude, 42.3)
        after = {f.name: getattr(v, f.name) for f in Lead._meta.concrete_fields if f.name not in ('latitude', 'longitude', 'updated_at')}
        self.assertEqual(before, after, 'nothing but the coordinates changed')
        # (the fixtures' two leads have no coordinates either, so they are in the lists too)
        v_ids, s_ids = sorted([self.v_lead.id, v.id]), sorted([self.s_lead.id, s.id])
        self.assertIn(f'Ventana Heating and Cooling: re-geocoded 2 {v_ids}; still failing 0 []', out)
        self.assertIn(f'Team Sunshine Construction: re-geocoded 2 {s_ids}; still failing 1 [{bad.id}]', out)

    def test_a_lead_fixed_meanwhile_is_left_as_the_person_left_it(self):
        lead = self._lead(self.ventana, '42 Thomas St', 'Northbridge')
        def fake(q):
            # someone geocodes it through the CRM while the command is running
            Lead.all_objects.filter(pk=lead.pk).update(latitude=41.0, longitude=-70.0)
            return (42.16, -71.66)
        with mock.patch('maps.management.commands.regeocode_missing.geocode', side_effect=fake):
            self._run('--since', (timezone.now() - timedelta(days=1)).date().isoformat(), '--apply')
        lead.refresh_from_db()
        self.assertEqual(lead.latitude, 41.0)
