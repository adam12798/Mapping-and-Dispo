"""Create the two organizations and assign every existing row.

Decided with the owner (Adam) on 2026-08-03:
- ALL existing operational data (every lead, rep, manager row, time-off
  request, voice log, message, update, rep-count row, both API tenants and
  the webhook configs) belongs to the friend's business, Team Sunshine
  Construction. Team Sunshine is also the default-inbound org because all
  current automation traffic (Twilio SMS, GHL webhooks, Alfred) is theirs.
- The owner's business, Ventana Heating and Cooling, starts empty. Only
  the user account 'Abahou' belongs to it. Abahou becomes a superuser so
  he can deliberately switch into Team Sunshine for support (each switch
  is written to OrgSwitchAudit).

Setting `organization` here is the one authorized write to Team Sunshine's
rows — nothing else about them is touched.
"""
from django.db import migrations

VENTANA = {'name': 'Ventana Heating and Cooling', 'slug': 'ventana'}
TEAM_SUNSHINE = {'name': 'Team Sunshine Construction', 'slug': 'team-sunshine'}

OWNED_MODELS = [
    'Lead', 'Rep', 'Manager', 'TimeOffRequest', 'VoiceCallLog',
    'LeadMessage', 'LeadUpdate', 'RepCountDefault', 'RepCountOverride',
    'UserProfile', 'APITenant', 'WebhookConfig', 'GHLWebhookLog',
]

OWNER_USERNAME = 'Abahou'


def forwards(apps, schema_editor):
    Organization = apps.get_model('maps', 'Organization')
    User = apps.get_model('auth', 'User')

    ventana, _ = Organization.objects.get_or_create(
        slug=VENTANA['slug'], defaults={'name': VENTANA['name']})
    sunshine, _ = Organization.objects.get_or_create(
        slug=TEAM_SUNSHINE['slug'],
        defaults={'name': TEAM_SUNSHINE['name'], 'is_default_inbound': True})

    for model_name in OWNED_MODELS:
        model = apps.get_model('maps', model_name)
        model.objects.filter(organization__isnull=True).update(organization=sunshine)

    UserProfile = apps.get_model('maps', 'UserProfile')
    UserProfile.objects.filter(user__username=OWNER_USERNAME).update(organization=ventana)
    User.objects.filter(username=OWNER_USERNAME).update(is_superuser=True)


def backwards(apps, schema_editor):
    Organization = apps.get_model('maps', 'Organization')
    User = apps.get_model('auth', 'User')
    for model_name in OWNED_MODELS:
        model = apps.get_model('maps', model_name)
        model.objects.update(organization=None)
    User.objects.filter(username=OWNER_USERNAME).update(is_superuser=False)
    Organization.objects.filter(
        slug__in=[VENTANA['slug'], TEAM_SUNSHINE['slug']]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0039_organization_tenancy'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
