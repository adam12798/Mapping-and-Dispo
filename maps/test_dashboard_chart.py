"""Dashboard chart API (/api/dashboard/chart/): group_by and filter keys are
limited to the Lead columns the dashboard page offers.

Before this allowlist any manager login (either organization) could name any
ORM path, e.g. group_by=organization__api_tenants__api_key or
updates__user__password, and read API keys, password hashes and other
organizations' leads through the joins. Every key the page offers must keep
working exactly as before, for both organizations.
"""
import datetime as dt
import json
import logging
import re
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import FieldError, ValidationError
from django.test import Client, TestCase

from .models import APITenant, Lead, LeadUpdate, Organization, Rep, UserProfile
from .views import CHART_FILTER_FIELDS, CHART_GROUP_BY_FIELDS

EASTERN = ZoneInfo('America/New_York')
URL = '/api/dashboard/chart/'
TEMPLATE = settings.BASE_DIR / 'maps' / 'templates' / 'maps' / 'dashboard.html'

# The conditions the page offers per column type (FILTER_CONDITIONS).
CONDITIONS = {
    'text': ['contains', 'not_contains', 'is', 'is_not', 'is_empty', 'is_not_empty'],
    'select': ['is', 'is_not', 'is_empty', 'is_not_empty'],
    'date': ['is', 'between', 'before', 'after', 'is_empty', 'is_not_empty'],
    'datetime': ['is', 'between', 'before', 'after', 'is_empty', 'is_not_empty'],
}
# Pre-existing errors in the filter code, unchanged by the allowlist: a
# DateField has no __date lookup, and "is (date)" gets a datetime-local value.
KNOWN_OLD_ERRORS = {('follow_up_date', 'is'): FieldError, ('appointment_datetime', 'is'): ValidationError}

# Paths that used to leak through the joins, and lookups/columns the page never sends.
TRAVERSALS = [
    'organization__api_tenants__api_key',
    'organization__api_tenants__name',
    'updates__user__password',
    'updates__user__username',
    'rep__user_profile__user__password',
    'rep__user_profile__tenant__api_key',
    'updates__user__lead_updates__lead__phone_number',
    'messages__body',
    'rep__name',
    'rep__phone_number',
    'rep__organization_id',
    'user__password',
    'org__api_key',
]
LOOKUPS = [
    'phone_number__regex', 'homeowner_name__iregex', 'city__startswith',
    'appointment_datetime__year', 'sat__isnull', 'disposition__in', 'city__iexact',
    'rep_id__gt', 'pk', 'id', 'organization', 'organization_id', 'rep',
    'raw_message', 'hub_customer_id', 'from_number', 'total_cost',
]


def _template_keys(var, attr):
    src = TEMPLATE.read_text()
    block = re.search(var + r'\s*=\s*\[(.*?)\n\s*\];', src, re.S).group(1)
    if var == 'FILTER_COLUMNS':
        return {k: t for k, t in re.findall(r"\{key:\s*'(\w+)',\s*label:\s*'[^']*',\s*type:\s*'(\w+)'", block)}
    return set(re.findall(attr + r":\s*'(\w+)'", block))


class ChartBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.sunshine = Organization.objects.get(slug='team-sunshine')
        cls.v_manager = User.objects.create_user('v_mgr', password='pw')
        UserProfile.all_objects.create(user=cls.v_manager, role='manager', organization=cls.ventana)
        cls.s_manager = User.objects.create_user('s_mgr', password='pw')
        UserProfile.all_objects.create(user=cls.s_manager, role='manager', organization=cls.sunshine)
        # A superuser who wrote updates on both orgs' leads: the cross-org join.
        cls.owner = User.objects.create_user('owner', password='owner-secret-pw', is_superuser=True)
        UserProfile.all_objects.create(user=cls.owner, role='manager', organization=cls.ventana)

        cls.v_rep = Rep.all_objects.create(name='Vince', home_address='1 A St', organization=cls.ventana)
        cls.s_rep = Rep.all_objects.create(name='Sunny', home_address='2 B St', organization=cls.sunshine)
        cls.v_tenant = APITenant.objects.create(name='V key', organization=cls.ventana)
        cls.s_tenant = APITenant.objects.create(name='S key', organization=cls.sunshine)

        appt = dt.datetime(2026, 11, 5, 10, 0, tzinfo=EASTERN)
        cls.v_leads = [
            Lead.all_objects.create(
                address='10 Main St', city='Lowell', state='MA', homeowner_name='Vera', phone_number='9785550001',
                source='Meta', tags='hot', appointment_type='hvac', appointment_format='in_person',
                disposition='sale', sat=True, appointment_datetime=appt, follow_up_date=dt.date(2026, 11, 20),
                call_notes='went well', call_transcript='hello', rep=cls.v_rep, organization=cls.ventana),
            Lead.all_objects.create(
                address='20 Elm St', city='Dracut', state='MA', homeowner_name='Val', phone_number='9785550002',
                source='Google', appointment_type='solar', appointment_format='virtual', disposition='no_sale',
                sat=False, appointment_datetime=appt + dt.timedelta(days=1), organization=cls.ventana),
        ]
        cls.s_leads = [
            Lead.all_objects.create(
                address='30 Oak St', city='Andover', state='NH', homeowner_name='Sam', phone_number='6175559999',
                source='Referral', appointment_type='both', appointment_format='in_person',
                disposition='follow_up', appointment_datetime=appt, rep=cls.s_rep, organization=cls.sunshine),
        ]
        for lead in cls.v_leads + cls.s_leads:
            LeadUpdate.all_objects.create(lead=lead, user=cls.owner, text='note', organization=lead.organization)

    def setUp(self):
        # The refused and known-error requests would log a line each.
        logger = logging.getLogger('django.request')
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.CRITICAL)

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def get(self, user, **params):
        if 'filters' in params and not isinstance(params['filters'], str):
            params['filters'] = json.dumps(params['filters'])
        return self.client_for(user).get(URL, params)


class AllowlistMatchesThePageTests(TestCase):
    """The allowlists are exactly what the dashboard page offers."""

    def test_group_by_options(self):
        self.assertEqual(_template_keys('GROUP_BY_OPTIONS', 'value'), set(CHART_GROUP_BY_FIELDS))

    def test_filter_columns(self):
        self.assertEqual(set(_template_keys('FILTER_COLUMNS', 'key')), set(CHART_FILTER_FIELDS))

    def test_default_widgets(self):
        src = TEMPLATE.read_text()
        block = re.search(r'DEFAULT_WIDGETS\s*=\s*\[(.*?)\];', src, re.S).group(1)
        for g in re.findall(r"groupBy:\s*'(\w+)'", block):
            self.assertIn(g, CHART_GROUP_BY_FIELDS)

    def test_no_key_is_a_path(self):
        for key in CHART_GROUP_BY_FIELDS | CHART_FILTER_FIELDS:
            self.assertNotIn('__', key)
            Lead._meta.get_field(key.removesuffix('_id') if key == 'rep_id' else key)


class LegitimateChartsTests(ChartBase):
    """Every key and condition the page sends still works, for both orgs."""

    def test_every_group_by_metric_and_stack(self):
        for user in (self.v_manager, self.s_manager):
            for g in sorted(CHART_GROUP_BY_FIELDS):
                for metric in ('count', 'conversion_rate'):
                    for stack in ('', 'disposition'):
                        params = {'group_by': g, 'metric': metric, 'start': '2026-11-01', 'end': '2026-11-30'}
                        if stack:
                            params['stack_by'] = stack
                        resp = self.get(user, **params)
                        self.assertEqual(resp.status_code, 200, (user.username, params))

    def test_default_request(self):
        resp = self.get(self.v_manager)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['total'], 2)

    def test_every_filter_key_and_condition(self):
        columns = _template_keys('FILTER_COLUMNS', 'key')
        values = {
            'text': 'low', 'select': 'sale', 'date': '2026-11-20', 'datetime': '2026-11-05T10:00',
        }
        for user, rep in ((self.v_manager, self.v_rep), (self.s_manager, self.s_rep)):
            for key, typ in sorted(columns.items()):
                for cond in CONDITIONS[typ]:
                    val = values[typ]
                    if key == 'rep_id':
                        val = str(rep.id)
                    elif key == 'sat':
                        val = 'true'
                    elif key in ('appointment_type', 'appointment_format'):
                        val = 'hvac' if key == 'appointment_type' else 'virtual'
                    f = {'key': key, 'cond': cond, 'val': val,
                         'val2': '2026-11-30' if typ == 'date' else '2026-11-30T23:59' if cond == 'between' else ''}
                    for g in ('disposition', 'rep_id'):
                        known = KNOWN_OLD_ERRORS.get((key, cond))
                        if known:
                            with self.assertRaises(known, msg=(key, cond)):
                                self.get(user, group_by=g, filters=[f])
                            continue
                        resp = self.get(user, group_by=g, filters=[f])
                        self.assertEqual(resp.status_code, 200, (user.username, key, cond))

    def test_results_unchanged_and_scoped(self):
        data = self.get(self.v_manager, group_by='city').json()
        self.assertEqual(sorted(data['labels']), ['Dracut', 'Lowell'])
        self.assertEqual(data['total'], 2)
        data = self.get(self.s_manager, group_by='city').json()
        self.assertEqual(data['labels'], ['Andover'])

        data = self.get(self.v_manager, group_by='rep_id').json()
        self.assertEqual(data['labels'], ['Vince'])
        data = self.get(self.s_manager, group_by='rep_id', metric='conversion_rate').json()
        self.assertEqual(data['labels'], ['Sunny'])

        data = self.get(self.v_manager, group_by='disposition', stack_by='disposition').json()
        self.assertTrue(data['stacked'])
        self.assertEqual(data['total'], 2)

        f = [{'key': 'city', 'cond': 'contains', 'val': 'LOW', 'val2': ''},
             {'key': 'sat', 'cond': 'is', 'val': 'true', 'val2': ''},
             {'key': 'appointment_datetime', 'cond': 'between', 'val': '2026-11-01T00:00', 'val2': '2026-11-30T23:59'}]
        data = self.get(self.v_manager, group_by='source', filters=f).json()
        self.assertEqual((data['labels'], data['values']), (['Meta'], [1]))

        f = [{'key': 'rep_id', 'cond': 'is', 'val': str(self.v_rep.id), 'val2': ''}]
        self.assertEqual(self.get(self.v_manager, group_by='city', filters=f).json()['total'], 1)

    def test_unfinished_rows_and_bad_json_are_ignored_as_before(self):
        blank = [{'key': '', 'cond': '', 'val': '', 'val2': ''}, {'key': 'city', 'cond': '', 'val': 'x'}]
        self.assertEqual(self.get(self.v_manager, group_by='city', filters=blank).json()['total'], 2)
        self.assertEqual(self.get(self.v_manager, group_by='city', filters='[{bad').json()['total'], 2)

    def test_rep_login_still_forbidden(self):
        rep_user = User.objects.create_user('rep_user', password='pw')
        UserProfile.all_objects.create(user=rep_user, role='rep', organization=self.ventana)
        self.assertEqual(self.get(rep_user, group_by='city').status_code, 403)


class RefusedTests(ChartBase):
    """Anything outside the allowlist is a 400 and reads nothing."""

    def secrets(self):
        out = [str(self.v_tenant.api_key), str(self.s_tenant.api_key), self.owner.password,
               self.v_manager.password, 'Sam', '6175559999', 'Andover', 'Sunny']
        return [s for s in out if s]

    def assert_refused(self, resp, user):
        self.assertEqual(resp.status_code, 400)
        body = resp.content.decode()
        for s in self.secrets():
            if user == self.s_manager and s in ('Sam', '6175559999', 'Andover', 'Sunny'):
                continue  # Team Sunshine's own data; the body still must not carry it
            self.assertNotIn(s, body)

    def test_group_by_traversal_and_lookups(self):
        for user in (self.v_manager, self.s_manager):
            for g in TRAVERSALS + LOOKUPS + ['', 'rep', 'disposition ', 'Disposition']:
                for stack in ('', 'disposition'):
                    params = {'group_by': g}
                    if stack:
                        params['stack_by'] = stack
                    self.assert_refused(self.get(user, **params), user)

    def test_filter_traversal_and_lookups(self):
        conds = ['is', 'is_not', 'contains', 'not_contains', 'is_empty', 'is_not_empty',
                 'before', 'after', 'between']
        for user in (self.v_manager, self.s_manager):
            for key in TRAVERSALS + LOOKUPS:
                for cond in conds:
                    f = [{'key': key, 'cond': cond, 'val': 'a', 'val2': 'b'}]
                    self.assert_refused(self.get(user, group_by='city', filters=f), user)
                # with an empty cond too, and behind a valid filter
                self.assert_refused(self.get(user, group_by='city', filters=[{'key': key, 'cond': ''}]), user)
                f = [{'key': 'city', 'cond': 'contains', 'val': 'o'}, {'key': key, 'cond': 'contains', 'val': '$'}]
                self.assert_refused(self.get(user, group_by='city', filters=f), user)

    def test_cross_org_filters(self):
        # Naming the other org is refused outright.
        for key, val in (('organization_id', self.sunshine.id), ('organization__slug', 'team-sunshine'),
                         ('updates__user__lead_updates__lead__organization_id', self.sunshine.id)):
            f = [{'key': key, 'cond': 'is', 'val': str(val)}]
            self.assert_refused(self.get(self.v_manager, group_by='city', filters=f), self.v_manager)
        # The other org's rep id is a plain column filter: it matches nothing.
        f = [{'key': 'rep_id', 'cond': 'is', 'val': str(self.s_rep.id)}]
        data = self.get(self.v_manager, group_by='city', filters=f).json()
        self.assertEqual((data['labels'], data['total']), ([], 0))

    def test_malformed_filter_shapes(self):
        for filters in ('{"key": "city"}', '"city"', '5', '[["city", "is", "x"]]', '["city"]',
                        '[{"key": ["city"], "cond": "is", "val": "x"}]',
                        '[{"key": {"a": 1}, "cond": "is", "val": "x"}]', '[{"key": 7, "cond": "is"}]'):
            resp = self.get(self.v_manager, group_by='city', filters=filters)
            self.assertEqual(resp.status_code, 400, filters)
