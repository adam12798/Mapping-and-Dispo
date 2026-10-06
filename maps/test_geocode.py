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
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

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

    def test_a_found_address_logs_nothing(self):
        with mock.patch('urllib.request.urlopen', return_value=_Resp([{'lat': '42.1619849', 'lon': '-71.6640266'}])), \
                self.assertNoLogs('geocode', level='WARNING'):
            self.assertEqual(geocode('42 Thomas St, Northbridge, MA 01534'), (42.1619849, -71.6640266))
