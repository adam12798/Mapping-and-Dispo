"""Rep Count: turn a whole day off / back on (POST /api/rep-count/day/).

Ventana only. Team Sunshine's /rep-count/ page must render byte-for-byte as
it did before this feature, and the endpoint refuses its managers."""
import hashlib
import re
import uuid

from django.contrib.auth.models import User
from django.template import engines
from django.test import Client

from .models import APITenant, RepCountDefault, RepCountOverride, UserProfile
from .tenancy import org_context
from .tests import TenancyBase

URL = '/api/rep-count/day/'
PAGE = '/rep-count/'
DAY = '2026-12-09'
BLOCKS = ['morning', 'midday', 'afternoon', 'evening']
TEMPLATE = 'maps/rep_count.html'
# sha256 of maps/templates/maps/rep_count.html before the day-off feature
# (origin/main 8fb2308). Stripping the `{% if rep_count_day_off %}` blocks
# from today's template must give back exactly that file, so Team Sunshine
# (rep_count_day_off False) is served the same page. If you change the page
# for everyone on purpose, update this hash in the same commit.
TEMPLATE_BEFORE_SHA256 = 'a9a49390c5361458950dddec23d3f6813c58e69cc5b90f91419cec8e120aba2f'
DAY_OFF_BLOCK = re.compile(r'\{% if rep_count_day_off %\}.*?\{% endif %\}', re.S)


def rows(org, date=DAY):
    return {o.time_block: o.count for o in RepCountOverride.all_objects.filter(organization=org, date=date)}


class DayOffBase(TenancyBase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        v, s = cls.ventana, cls.sunshine
        RepCountDefault.all_objects.create(time_block='midday', count=5, organization=v)
        # Ventana: a custom count on the day, and one on the next day
        RepCountOverride.all_objects.create(date=DAY, time_block='morning', count=6, organization=v)
        RepCountOverride.all_objects.create(date='2026-12-10', time_block='evening', count=4, organization=v)
        # Team Sunshine's own rows on the same day
        RepCountOverride.all_objects.create(date=DAY, time_block='morning', count=9, organization=s)
        RepCountOverride.all_objects.create(date=DAY, time_block='evening', count=2, organization=s)

    def client_for(self, user, **kwargs):
        client = Client(**kwargs)
        client.force_login(user)
        return client

    def post(self, user, body, **kwargs):
        return self.client_for(user).post(URL, body, content_type='application/json', **kwargs)

    def user(self, role, org):
        user = User.objects.create_user(f'{role}-{uuid.uuid4().hex[:12]}', password='pw')
        UserProfile.all_objects.create(user=user, role=role, organization=org)
        return user

    def assert_sunshine_untouched(self):
        self.assertEqual(rows(self.sunshine), {'morning': 9, 'evening': 2})


class DayOffTests(DayOffBase):
    def test_turn_off_sets_all_four_blocks_to_zero(self):
        resp = self.post(self.v_manager, {'date': DAY, 'off': True})
        self.assertEqual(resp.status_code, 200)
        zeros = {b: 0 for b in BLOCKS}
        self.assertEqual(resp.json(), {'status': 'ok', 'date': DAY, 'off': True, 'overrides': zeros})
        self.assertEqual(rows(self.ventana), zeros)  # the custom 6 is now 0 too
        self.assertEqual(rows(self.ventana, '2026-12-10'), {'evening': 4})
        self.assert_sunshine_untouched()
        # again: still one row per block
        self.assertEqual(self.post(self.v_manager, {'date': DAY, 'off': True}).status_code, 200)
        self.assertEqual(RepCountOverride.all_objects.filter(organization=self.ventana, date=DAY).count(), 4)
        # the page's own overrides feed shows the four zeros
        feed = self.client_for(self.v_manager).get('/api/rep-count/overrides/', {'week_start': '2026-12-07'})
        day = {o['time_block']: o['count'] for o in feed.json()['overrides'] if o['date'] == DAY}
        self.assertEqual(day, zeros)

    def test_turn_back_on_removes_the_days_overrides(self):
        self.post(self.v_manager, {'date': DAY, 'off': True})
        resp = self.post(self.v_manager, {'date': DAY, 'off': False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'ok', 'date': DAY, 'off': False, 'overrides': {}})
        self.assertEqual(rows(self.ventana), {})
        self.assertEqual(rows(self.ventana, '2026-12-10'), {'evening': 4})
        self.assert_sunshine_untouched()
        # back on for a day that was never off: nothing to remove, still fine
        self.assertEqual(self.post(self.v_manager, {'date': '2026-12-11', 'off': False}).status_code, 200)

    def test_lurch_availability_follows_the_day(self):
        key = APITenant.objects.create(name='V Lurch', organization=self.ventana).api_key

        def day_rows():
            resp = Client().get('/api/v1/availability/', {'week_start': DAY, 'days': 1},
                                HTTP_AUTHORIZATION=f'Bearer {key}')
            self.assertEqual(resp.status_code, 200)
            return {r['time_block']: (r['rep_count'], r['open']) for r in resp.json()['availability']}

        self.assertEqual(day_rows(), {'morning': (6, 6), 'midday': (5, 5), 'afternoon': (3, 3), 'evening': (3, 3)})
        self.post(self.v_manager, {'date': DAY, 'off': True})
        self.assertEqual(day_rows(), {b: (0, 0) for b in BLOCKS})
        self.post(self.v_manager, {'date': DAY, 'off': False})
        self.assertEqual(day_rows(), {'morning': (3, 3), 'midday': (5, 5), 'afternoon': (3, 3), 'evening': (3, 3)})

    def test_bad_requests(self):
        client = self.client_for(self.v_manager)
        self.assertEqual(client.get(URL).status_code, 405)
        for body in ({'off': True}, {'date': DAY}, {'date': '12/09/2026', 'off': True},
                     {'date': '2026-02-30', 'off': True}, {'date': '1999-12-31', 'off': True},
                     {'date': 20261209, 'off': True}, {'date': DAY, 'off': 'true'},
                     {'date': DAY, 'off': 1}, {'date': DAY, 'off': None}, [DAY, True]):
            resp = client.post(URL, body, content_type='application/json')
            self.assertEqual(resp.status_code, 400, body)
        for raw in ('not json', '', 'null'):
            self.assertEqual(client.post(URL, raw, content_type='application/json').status_code, 400, raw)
        self.assertEqual(rows(self.ventana), {'morning': 6})
        self.assert_sunshine_untouched()

    def test_csrf_token_required(self):
        client = self.client_for(self.v_manager, enforce_csrf_checks=True)
        resp = client.post(URL, {'date': DAY, 'off': True}, content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(rows(self.ventana), {'morning': 6})
        page = client.get(PAGE).content.decode()
        token = re.search(r"var DAY_OFF_CSRF = '([^']+)';", page).group(1)
        resp = client.post(URL, {'date': DAY, 'off': True}, content_type='application/json',
                           HTTP_X_CSRFTOKEN=token)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(rows(self.ventana), {b: 0 for b in BLOCKS})


class RefusedTests(DayOffBase):
    def test_non_managers_are_refused(self):
        for role in ('rep', 'provider'):
            resp = self.post(self.user(role, self.ventana), {'date': DAY, 'off': True})
            self.assertEqual(resp.status_code, 403, role)
        resp = Client().post(URL, {'date': DAY, 'off': True}, content_type='application/json')
        self.assertEqual(resp.status_code, 302)  # to the login page
        self.assertEqual(rows(self.ventana), {'morning': 6})
        self.assert_sunshine_untouched()

    def test_team_sunshine_manager_gets_403(self):
        for off in (True, False):
            resp = self.post(self.s_manager, {'date': DAY, 'off': off})
            self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.client_for(self.s_manager).get(URL).status_code, 403)
        self.assert_sunshine_untouched()
        self.assertEqual(rows(self.ventana), {'morning': 6})

    def test_superuser_switched_into_team_sunshine_is_refused(self):
        client = self.client_for(self.owner)
        session = client.session
        session['active_org_id'] = self.sunshine.id
        session.save()
        resp = client.post(URL, {'date': DAY, 'off': False}, content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assert_sunshine_untouched()
        page = client.get(PAGE)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('rc-day-btn', page.content.decode())


class PageTests(DayOffBase):
    def test_ventana_page_has_the_controls(self):
        resp = self.client_for(self.v_manager).get(PAGE)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        for marker in ('Turn off day', 'Turn back on', "fetch('/api/rep-count/day/'", '.rc-day-btn {',
                       'var DAY_OFF_CSRF = '):
            self.assertIn(marker, html)

    def test_team_sunshine_page_is_byte_identical_to_before(self):
        loader = engines['django'].engine.find_template(TEMPLATE)[0]
        source = loader.source
        self.assertEqual(len(DAY_OFF_BLOCK.findall(source)), 2)
        before = DAY_OFF_BLOCK.sub('', source)
        self.assertEqual(hashlib.sha256(before.encode('utf-8')).hexdigest(), TEMPLATE_BEFORE_SHA256)

        resp = self.client_for(self.s_manager).get(PAGE)
        self.assertEqual(resp.status_code, 200)
        with org_context(self.sunshine):
            expected = engines['django'].from_string(before).render(
                {'active_tab': 'rep_count'}, request=resp.wsgi_request)
        self.assertEqual(resp.content, expected.encode('utf-8'))
        html = resp.content.decode()
        for marker in ('rc-day', 'day-off', 'DAY_OFF', '/api/rep-count/day/', 'Turn off day'):
            self.assertNotIn(marker, html)
        self.assertNotIn('csrftoken', resp.cookies)
