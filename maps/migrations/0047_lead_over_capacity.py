from django.db import migrations, models

# A blocked ALTER on maps_lead (both organizations' table) fails after 5 s
# instead of queueing every lead read and write behind it. SET LOCAL lasts
# until this migration's transaction ends; it is set first in both directions.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"

# Lead.over_capacity is only ever True for a lead a slot-claims check found in
# a full block. When that lead's appointment time or cancelled flag changes,
# the mark no longer describes it, so it goes back to NULL. The WHEN clause
# keeps the function from even running for a NULL or False row, which is
# every Team Sunshine lead: their writes are untouched.
CREATE_FUNCTION = """
CREATE OR REPLACE FUNCTION maps_lead_clear_over_capacity() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.over_capacity := NULL;
    RETURN NEW;
END;
$fn$;
"""

CREATE_TRIGGER = """
CREATE TRIGGER maps_lead_clear_over_capacity
    BEFORE UPDATE OF appointment_datetime, cancelled ON maps_lead
    FOR EACH ROW
    WHEN (OLD.over_capacity IS TRUE AND NEW.over_capacity IS TRUE AND (
          NEW.appointment_datetime IS DISTINCT FROM OLD.appointment_datetime
          OR NEW.cancelled IS DISTINCT FROM OLD.cancelled))
    EXECUTE FUNCTION maps_lead_clear_over_capacity();
"""


def set_lock_timeout(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(LOCK_TIMEOUT)


def install_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute(CREATE_FUNCTION)
    schema_editor.execute(CREATE_TRIGGER)


def remove_trigger(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return
    schema_editor.execute('DROP TRIGGER IF EXISTS maps_lead_clear_over_capacity ON maps_lead;')
    schema_editor.execute('DROP FUNCTION IF EXISTS maps_lead_clear_over_capacity();')


class Migration(migrations.Migration):
    """Slot claims, part 2: maps_lead alone. One nullable column every
    existing lead reads as NULL (metadata only on PostgreSQL; no row is
    rewritten or updated) and its trigger. lock_timeout is 5 s, applying and
    reversing."""

    dependencies = [
        ('maps', '0046_slot_claims'),
    ]

    operations = [
        migrations.RunPython(set_lock_timeout, migrations.RunPython.noop),
        migrations.AddField(
            model_name='lead',
            name='over_capacity',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.RunPython(install_trigger, remove_trigger),
        # Reversing runs the operations backwards, so this one runs first then.
        migrations.RunPython(migrations.RunPython.noop, set_lock_timeout),
    ]
