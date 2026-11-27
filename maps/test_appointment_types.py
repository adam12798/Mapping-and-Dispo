"""Appointment types per organization (Organization.appointment_types).

An organization with the default list (every one unless configured; Team
Sunshine) must see, report and assign exactly as before. An organization
that adds Windows (any rep) gets it in its dropdowns and reports, and any of
its active reps can be sent on it; no other organization sees it.
"""
import datetime as dt
import json
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase

from . import appointment_types
from .assignment import auto_assign_leads, is_compatible
from .models import APITenant, Lead, LeadUpdate, Organization, Rep, UserProfile
from .tenancy import org_context

E = ZoneInfo('America/New_York')
DAY = dt.date(2026, 12, 1)
VENTANA_TYPES = [
    {'value': 'solar', 'label': 'Solar', 'any_rep': False},
    {'value': 'hvac', 'label': 'HVAC', 'any_rep': False},
    {'value': 'both', 'label': 'Both', 'any_rep': False},
    {'value': 'windows', 'label': 'Windows', 'any_rep': True},
    {'value': 'insulation', 'label': 'Insulation', 'any_rep': True},
    {'value': 'other', 'label': 'Other', 'any_rep': True},
]
DEFAULT_FILTER_JS = ("{value: 'both', label: 'Both'}, {value: 'hvac', label: 'HVAC'}, "
                     "{value: 'solar', label: 'Solar'}\n")
DEFAULT_SELECT = (
    '<option value="">—</option>\n'
    '                            <option value="solar" >Solar</option>\n'
    '                            <option value="hvac" >HVAC</option>\n'
    '                            <option value="both" >Both</option>\n'
    '                        </select>'
)


def old_is_compatible(rep, lead):
    """is_compatible as it was before per-organization types (origin/main)."""
    if not lead.appointment_type:
        return False
    if not rep.specialty or rep.specialty == 'both':
        return True
    return rep.specialty == lead.appointment_type


class DefaultListTests(TestCase):
    def test_new_org_gets_exactly_the_three(self):
        org = Organization.objects.create(name='New', slug='new-org')
        org.refresh_from_db()
        self.assertEqual(org.appointment_types, appointment_types.default_appointment_types())
        self.assertEqual([(t['value'], t['label']) for t in org.appointment_types],
                         Lead.APPOINTMENT_TYPE_CHOICES)
        self.assertFalse(any(t['any_rep'] for t in org.appointment_types))

    def test_existing_orgs_read_the_default(self):
        # The migration adds the column with the default; nothing else.
        for slug in ('ventana', 'team-sunshine'):
            org = Organization.objects.get(slug=slug)
            self.assertEqual(org.appointment_types, appointment_types.default_appointment_types())

    def test_default_is_a_fresh_copy(self):
        a = appointment_types.default_appointment_types()
        a[0]['label'] = 'changed'
        self.assertEqual(appointment_types.default_appointment_types()[0]['label'], 'Solar')

    def test_no_org_reads_the_default(self):
        self.assertEqual(appointment_types.for_org(None), appointment_types.default_appointment_types())
        self.assertEqual(appointment_types.for_org(987654), appointment_types.default_appointment_types())


class ValidationTests(TestCase):
    def test_valid_list(self):
        appointment_types.validate_appointment_types(VENTANA_TYPES)
        appointment_types.validate_appointment_types([{'value': 'x', 'label': 'X'}])

    def test_invalid_lists(self):
        bad = [
            [], {}, 'solar', [None], ['solar'],
            [{'value': 'Windows', 'label': 'Windows'}],          # uppercase value
            [{'value': 'insulations', 'label': 'Too long'}],     # 11 characters
            [{'value': '', 'label': 'Empty'}],
            [{'value': 'a', 'label': ''}],
            [{'value': 'a', 'label': 'A', 'any_rep': 'yes'}],
            [{'value': 'a', 'label': 'A'}, {'value': 'a', 'label': 'B'}],
            [{'value': 'a', 'label': 'A', 'colour': 'red'}],
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                appointment_types.validate_appointment_types(value)

    def test_full_clean_runs_the_validator(self):
        org = Organization.objects.get(slug='ventana')
        org.appointment_types = [{'value': 'Bad Value', 'label': 'x'}]
        with self.assertRaises(ValidationError):
            org.full_clean()

    def test_malformed_saved_value_reads_safely(self):
        self.assertEqual(appointment_types.normalize('junk'), appointment_types.default_appointment_types())
        self.assertEqual(appointment_types.normalize([{'label': 'no value'}]),
                         appointment_types.default_appointment_types())
        self.assertEqual(appointment_types.normalize([{'value': 'w', 'any_rep': 'true'}]),
                         [{'value': 'w', 'label': 'w', 'any_rep': False}])


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ventana = Organization.objects.get(slug='ventana')
        cls.ventana.appointment_types = VENTANA_TYPES
        cls.ventana.save(update_fields=['appointment_types'])
        cls.sunshine = Organization.objects.get(slug='team-sunshine')
        cls.v_mgr = User.objects.create_user('v_mgr_at', password='pw')
        UserProfile.all_objects.create(user=cls.v_mgr, role='manager', organization=cls.ventana)
        cls.s_mgr = User.objects.create_user('s_mgr_at', password='pw')
        UserProfile.all_objects.create(user=cls.s_mgr, role='manager', organization=cls.sunshine)
        cls.v_tenant = APITenant.objects.create(name='V', slug='v-at', organization=cls.ventana)
        cls.s_tenant = APITenant.objects.create(name='S', slug='s-at', organization=cls.sunshine)

    def client_for(self, user):
        c = Client()
        c.force_login(user)
        return c

    def lead(self, org, appointment_type, hour=10, **kw):
        kw.setdefault('disposition', '')
        return Lead.all_objects.create(
            organization=org, address=f'{hour} Main St', city='Lowell', appointment_type=appointment_type,
            appointment_format='in_person', appointment_datetime=dt.datetime(2026, 12, 1, hour, 0, tzinfo=E),
            latitude=42.6, longitude=-71.3, **kw)

    def rep(self, org, name, specialty, **kw):
        kw.setdefault('is_active', True)
        return Rep.all_objects.create(organization=org, name=name, specialty=specialty, home_address='x',
                                      latitude=42.6, longitude=-71.3, **kw)


class DefaultOrgUnchangedTests(Base):
    """Team Sunshine keeps the default list: identical pages and assignment."""

    def test_pages_render_the_three_as_before(self):
        self.lead(self.sunshine, '')
        c = self.client_for(self.s_mgr)
        for url in ('/crm/', '/daily/?date=2026-12-01'):
            html = c.get(url).content.decode()
            self.assertIn(DEFAULT_SELECT, html)
            self.assertIn(DEFAULT_FILTER_JS, html)
            self.assertNotIn('Windows', html)
        self.assertIn(DEFAULT_FILTER_JS, c.get('/dashboard/').content.decode())

    def test_reports_as_before(self):
        for t, d in [('solar', 'sale'), ('hvac', ''), ('both', 'no_sale'), ('windows', 'sale')]:
            self.lead(self.sunshine, t, disposition=d)
        c = self.client_for(self.s_mgr)
        data = c.get('/api/dashboard/?group_by=product').json()
        self.assertEqual([d['label'] for d in data['by_disposition']['datasets']], ['Solar', 'Hvac', 'Both'])
        self.assertEqual(sorted(data['by_product']['labels']), ['Both', 'HVAC', 'Solar'])
        chart = c.get('/api/dashboard/chart/?group_by=appointment_type').json()
        # A value outside the list shows raw, as it always has.
        self.assertEqual(sorted(chart['labels']), ['Both', 'HVAC', 'Solar', 'windows'])

    def test_is_compatible_matrix_unchanged(self):
        types = ['', 'solar', 'hvac', 'both', 'windows', 'other', 'Solar']
        with org_context(self.sunshine):
            for spec in ['', 'solar', 'hvac', 'both']:
                for active in (True, False):
                    rep = self.rep(self.sunshine, f'R {spec} {active}', spec, is_active=active)
                    for t in types:
                        lead = self.lead(self.sunshine, t)
                        with self.subTest(spec=spec, active=active, type=t):
                            self.assertEqual(is_compatible(rep, lead), old_is_compatible(rep, lead))

    def test_auto_assign_unchanged(self):
        hvac_rep = self.rep(self.sunshine, 'Hvac Rep', 'hvac')
        windows = self.lead(self.sunshine, 'windows', hour=9)
        hvac = self.lead(self.sunshine, 'hvac', hour=12)
        with org_context(self.sunshine):
            result = auto_assign_leads(DAY, save=False)
        assigned = {l.id for a in result['assignments'] for l, _ in a['stops']}
        self.assertEqual(assigned, {hvac.id})
        self.assertEqual([l.id for l in result['unassigned']], [windows.id])
        self.assertEqual(result['assignments'][0]['rep'].id, hvac_rep.id)


class WindowsAnyRepTests(Base):
    """Ventana with Windows/Insulation/Other open to any rep."""

    @mock.patch('maps.views.geocode', return_value=(42.6, -71.3))
    def test_v1_create_and_update_accept_new_types(self, _geo):
        auth = {'HTTP_AUTHORIZATION': f'Bearer {self.v_tenant.api_key}'}
        r = Client().post('/api/v1/leads/create/', json.dumps({'address': '1 Elm', 'appointment_type': 'windows'}),
                          content_type='application/json', **auth)
        self.assertEqual(r.status_code, 201)
        lead = Lead.all_objects.get(id=r.json()['id'])
        self.assertEqual((lead.appointment_type, lead.organization_id), ('windows', self.ventana.id))
        r = Client().put(f'/api/v1/leads/{lead.id}/', json.dumps({'appointment_type': 'insulation'}),
                         content_type='application/json', **auth)
        self.assertEqual(r.status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.appointment_type, 'insulation')

    @mock.patch('maps.views.geocode', return_value=(42.6, -71.3))
    def test_v1_still_stores_any_value_for_default_org(self, _geo):
        # No new rejections: Team Sunshine's integrations keep working as now.
        auth = {'HTTP_AUTHORIZATION': f'Bearer {self.s_tenant.api_key}'}
        for value in ('windows', 'Solar', ''):
            r = Client().post('/api/v1/leads/create/', json.dumps({'address': '2 Elm', 'appointment_type': value}),
                              content_type='application/json', **auth)
            self.assertEqual(r.status_code, 201)
            self.assertEqual(Lead.all_objects.get(id=r.json()['id']).appointment_type, value)

    def test_is_compatible_any_rep(self):
        lead = self.lead(self.ventana, 'windows')
        hvac_rep = self.rep(self.ventana, 'V Hvac', 'hvac')
        solar_rep = self.rep(self.ventana, 'V Solar', 'solar')
        inactive = self.rep(self.ventana, 'V Off', 'hvac', is_active=False)
        other_org = self.rep(self.sunshine, 'S Both', 'both')
        self.assertTrue(is_compatible(hvac_rep, lead))
        self.assertTrue(is_compatible(solar_rep, lead))
        self.assertFalse(is_compatible(inactive, lead))
        self.assertFalse(is_compatible(other_org, lead))
        # Types without the flag keep the specialty rule.
        self.assertFalse(is_compatible(hvac_rep, self.lead(self.ventana, 'solar')))
        self.assertFalse(is_compatible(hvac_rep, self.lead(self.ventana, '')))

    def test_auto_assign_sends_an_hvac_rep_on_windows(self):
        hvac_rep = self.rep(self.ventana, 'V Hvac', 'hvac')
        windows = self.lead(self.ventana, 'windows', hour=9)
        with org_context(self.ventana):
            result = auto_assign_leads(DAY, save=True)
        self.assertEqual(result['unassigned'], [])
        windows.refresh_from_db()
        self.assertEqual(windows.rep_id, hvac_rep.id)

    def test_dropdowns_show_windows_for_ventana_only(self):
        self.lead(self.ventana, 'windows')
        self.lead(self.sunshine, 'hvac')
        v_html = self.client_for(self.v_mgr).get('/crm/').content.decode()
        self.assertIn('<option value="windows" selected>Windows</option>', v_html)
        self.assertIn("{value: 'windows', label: 'Windows'}", v_html)
        self.assertIn("{value: 'both', label: 'Both'}, {value: 'hvac', label: 'HVAC'}, "
                      "{value: 'insulation', label: 'Insulation'}, {value: 'other', label: 'Other'}, "
                      "{value: 'solar', label: 'Solar'}, {value: 'windows', label: 'Windows'}", v_html)
        s_html = self.client_for(self.s_mgr).get('/crm/').content.decode()
        self.assertNotIn('windows', s_html.lower())

    def test_reports_show_windows_for_ventana_only(self):
        self.lead(self.ventana, 'windows', disposition='sale')
        self.lead(self.ventana, 'hvac')
        self.lead(self.sunshine, 'windows')
        v = self.client_for(self.v_mgr)
        self.assertIn('Windows', v.get('/api/dashboard/chart/?group_by=appointment_type').json()['labels'])
        prod = v.get('/api/dashboard/?group_by=product').json()
        self.assertIn('Windows', prod['by_product']['labels'])
        self.assertIn('Windows', [d['label'] for d in prod['by_disposition']['datasets']])
        s = self.client_for(self.s_mgr)
        self.assertEqual(s.get('/api/dashboard/chart/?group_by=appointment_type').json()['labels'], ['windows'])
        self.assertEqual(s.get('/api/dashboard/?group_by=product').json()['by_product']['labels'], [])

    def test_chatter_uses_the_label(self):
        lead = self.lead(self.ventana, 'hvac')
        c = self.client_for(self.v_mgr)
        c.put(f'/api/leads/{lead.id}/', json.dumps({'appointment_type': 'windows'}), content_type='application/json')
        c.post('/api/leads/bulk-update/', json.dumps({'ids': [lead.id], 'fields': {'appointment_type': 'other'}}),
               content_type='application/json')
        with org_context(self.ventana):
            texts = list(LeadUpdate.objects.filter(lead=lead).order_by('id').values_list('text', flat=True))
        self.assertEqual(texts, ['Appt Type: HVAC → Windows', 'Bulk update: Appt Type → Other'])


# The Appt Type <select> exactly as origin/main rendered it before
# per-organization types (crm.html and daily.html had the same lines).
OLD_SELECT = (
    '                            <option value="">—</option>\n'
    '                            <option value="solar" {% if lead.appointment_type == "solar" %}selected{% endif %}>Solar</option>\n'
    '                            <option value="hvac" {% if lead.appointment_type == "hvac" %}selected{% endif %}>HVAC</option>\n'
    '                            <option value="both" {% if lead.appointment_type == "both" %}selected{% endif %}>Both</option>\n'
    '                        </select>'
)
OLD_PINS = ("var leadTypeColors = { solar: '#f1c40f', hvac: '#e74c3c', both: '#27ae60' };\n"
            "        var leadsLayer")
OLD_BADGES = ("var typeColorMap = { solar: '#f1c40f', hvac: '#e74c3c', both: '#27ae60' };\n\n"
              "        function updateSidebar")


class DefaultOrgParityTests(Base):
    """A default-list organization renders what origin/main rendered, for
    every kind of stored value and every way into the pages."""
    STORED = ['solar', 'hvac', 'both', 'Solar', 'HVAC', 'roofing', ' solar', '', 'windows']

    def assert_rows_match_old(self, html, leads):
        from django.template import Context, Template
        old = Template(OLD_SELECT)
        for lead in leads:
            row = html.split(f'<tr data-id="{lead.id}">', 1)[1].split('</tr>', 1)[0]
            with self.subTest(value=lead.appointment_type):
                self.assertIn(old.render(Context({'lead': lead})), row)

    def assert_default_page(self, client, prefix=''):
        leads = [self.lead(self.sunshine, v, hour=8 + i) for i, v in enumerate(self.STORED)]
        crm = client.get(f'{prefix}/crm/').content.decode()
        daily = client.get(f'{prefix}/daily/?date=2026-12-01').content.decode()
        for html in (crm, daily):
            self.assertIn(DEFAULT_FILTER_JS, html)
            self.assertNotIn('Windows', html)
        if not prefix:
            self.assert_rows_match_old(crm, leads)
            self.assert_rows_match_old(daily, leads)
        index = client.get(f'{prefix}/').content.decode()
        self.assertIn(OLD_PINS, index)
        self.assertIn(OLD_BADGES, index)
        self.assertIn(DEFAULT_FILTER_JS, client.get(f'{prefix}/dashboard/').content.decode())

    def test_manager(self):
        self.assert_default_page(self.client_for(self.s_mgr))

    def test_superuser_switched_into_the_default_org(self):
        boss = User.objects.create_user('boss_at', password='pw', is_superuser=True)
        UserProfile.all_objects.create(user=boss, role='manager', organization=self.ventana)
        c = self.client_for(boss)
        session = c.session
        session['active_org_id'] = self.sunshine.id
        session.save()
        self.assert_default_page(c)
        # And switched back into Ventana, Ventana's list shows.
        session['active_org_id'] = self.ventana.id
        session.save()
        self.assertIn("{value: 'windows', label: 'Windows'}", c.get('/crm/').content.decode())

    def test_tenant_pages(self):
        tenant = APITenant.objects.create(name='ST', slug='st-at', organization=self.sunshine)
        user = User.objects.create_user('s_ten_at', password='pw')
        UserProfile.all_objects.create(user=user, role='manager', organization=self.sunshine, tenant=tenant)
        self.assert_default_page(self.client_for(user), prefix='/t/st-at')

    def test_provider_page(self):
        for org, name in ((self.sunshine, 's_prov_at'), (self.ventana, 'v_prov_at')):
            user = User.objects.create_user(name, password='pw')
            UserProfile.all_objects.create(user=user, role='provider', organization=org)
            html = self.client_for(user).get('/provider/crm/').content.decode()
            if org == self.sunshine:
                self.assertIn(DEFAULT_FILTER_JS, html)
                self.assertNotIn('Windows', html)
            else:
                self.assertIn("{value: 'windows', label: 'Windows'}", html)


class TagsAndColoursTests(Base):
    def put_tags(self, user, lead, tags):
        r = self.client_for(user).put(f'/api/leads/{lead.id}/', json.dumps({'tags': tags}),
                                      content_type='application/json')
        lead.refresh_from_db()
        return r.json()

    def test_tag_pills_keep_an_org_type_beyond_the_base_three(self):
        lead = self.lead(self.ventana, 'insulation')
        self.assertEqual(self.put_tags(self.v_mgr, lead, 'MassSave')['appointment_type'], 'insulation')
        self.assertEqual(lead.appointment_type, 'insulation')
        self.put_tags(self.v_mgr, lead, '')
        self.assertEqual(lead.appointment_type, 'insulation')

    def test_tag_pills_still_derive_the_base_three(self):
        v_lead = self.lead(self.ventana, 'hvac')
        self.put_tags(self.v_mgr, v_lead, 'MassSave')
        self.assertEqual(v_lead.appointment_type, 'both')
        # A value outside the org's list is re-derived, as before (Team Sunshine).
        s_lead = self.lead(self.sunshine, 'windows')
        self.put_tags(self.s_mgr, s_lead, 'MassSave')
        self.assertEqual(s_lead.appointment_type, 'both')
        self.put_tags(self.s_mgr, s_lead, '')
        self.assertEqual(s_lead.appointment_type, '')

    def test_dashboard_colours(self):
        for t in ('solar', 'hvac', 'both', 'windows', 'insulation', 'other'):
            self.lead(self.ventana, t, disposition='sale')
        data = self.client_for(self.v_mgr).get('/api/dashboard/?group_by=product').json()
        pivot = {d['label']: d['backgroundColor'] for d in data['by_disposition']['datasets']}
        pie = dict(zip(data['by_product']['labels'], data['by_product']['datasets'][0]['backgroundColor']))
        for colours in (pivot, pie):
            base = {k: v for k, v in colours.items() if k.lower() in ('solar', 'hvac', 'both')}
            self.assertEqual(sorted(base.values()), ['#27ae60', '#e74c3c', '#f1c40f'])
            extra = [v for k, v in colours.items() if k.lower() not in ('solar', 'hvac', 'both')]
            self.assertEqual(len(extra), 3)
            self.assertEqual(len(set(extra)), 3)
            self.assertFalse(set(extra) & {'#27ae60', '#e74c3c', '#f1c40f', '#ff69b4', '#98c1d9'})

    def test_map_pins(self):
        html = self.client_for(self.v_mgr).get('/').content.decode()
        for var in ('leadTypeColors', 'typeColorMap'):
            for v in ('windows', 'insulation', 'other'):
                self.assertIn(f"{var}['{v}'] = '{appointment_types.EXTRA_PIN_COLOR}';", html)
        self.assertNotIn("leadTypeColors['solar']", html)
        self.assertNotEqual(appointment_types.EXTRA_PIN_COLOR, '#ff69b4')
