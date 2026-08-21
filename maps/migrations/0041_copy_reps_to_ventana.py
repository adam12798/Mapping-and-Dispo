"""Copy every Team Sunshine rep into Ventana (requested by Adam 2026-08-21).

Team Sunshine's rows are only read, never modified. Copies are new Rep rows
owned by Ventana; linked records (time off, user accounts, rep-count rows)
are not copied. Idempotent: a rep whose name already exists in Ventana is
skipped, so re-running on deploy is safe.
"""
from django.db import migrations

COPIED_FIELDS = [
    'name', 'phone_number', 'home_address', 'city', 'latitude', 'longitude',
    'specialty', 'rating', 'color', 'is_active', 'textblast_eligible',
    'sms_consent', 'sms_consent_at',
]


def forwards(apps, schema_editor):
    Organization = apps.get_model('maps', 'Organization')
    Rep = apps.get_model('maps', 'Rep')

    sunshine = Organization.objects.filter(slug='team-sunshine').first()
    ventana = Organization.objects.filter(slug='ventana').first()
    if sunshine is None or ventana is None:
        return

    existing_names = set(
        Rep.objects.filter(organization=ventana).values_list('name', flat=True)
    )
    for rep in Rep.objects.filter(organization=sunshine):
        if rep.name in existing_names:
            continue
        Rep.objects.create(
            organization=ventana,
            **{field: getattr(rep, field) for field in COPIED_FIELDS},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0040_backfill_organizations'),
    ]

    operations = [
        # Copies are plain Ventana rows; reversing would risk deleting rows
        # Adam has since edited, so the reverse is a no-op.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
