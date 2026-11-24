import django.db.models.deletion
import django.db.models.manager
import django.utils.timezone
import uuid
from django.db import migrations, models

# A blocked lock on a table both organizations use fails after 5 s instead of
# queueing every read and write of it behind the migration. SET LOCAL lasts
# until the migration's transaction ends; it is set first in both directions.
LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"


def set_lock_timeout(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute(LOCK_TIMEOUT)


class Migration(migrations.Migration):
    """Slot claims (maps/availability.py), part 1. Create and add only: five
    Organization columns every organization reads as off / 24 h / 30 min /
    refuse / 1 (with database defaults, so older code can still insert one)
    and two new tables. maps_lead is not touched (its column is 0047). No row
    is updated or deleted. lock_timeout is 5 s, applying and reversing."""

    dependencies = [
        ('maps', '0045_organization_reminders_by_wednesday'),
    ]

    operations = [
        migrations.RunPython(set_lock_timeout, migrations.RunPython.noop),
        migrations.AddField(
            model_name='organization',
            name='slot_claim_ttl_hours',
            field=models.PositiveSmallIntegerField(db_default=24, default=24),
        ),
        migrations.AddField(
            model_name='organization',
            name='slot_claims_enabled',
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name='organization',
            name='slot_start_step_minutes',
            field=models.PositiveSmallIntegerField(db_default=30, default=30),
        ),
        migrations.AddField(
            model_name='organization',
            name='slot_full_policy',
            field=models.CharField(choices=[('refuse', 'Refuse'), ('flag', 'Allow and flag')], db_default='refuse', default='refuse', max_length=10),
        ),
        migrations.AddField(
            model_name='organization',
            name='slot_overbook_cap',
            field=models.PositiveSmallIntegerField(db_default=1, default=1),
        ),
        migrations.CreateModel(
            name='SlotBlockLock',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('time_block', models.CharField(max_length=10)),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='maps.organization')),
            ],
            options={
                'abstract': False,
                'base_manager_name': 'all_objects',
                'constraints': [models.UniqueConstraint(fields=('organization', 'date', 'time_block'), name='slot_block_lock_unique')],
            },
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
        migrations.CreateModel(
            name='SlotClaim',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('claim_id', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('date', models.DateField()),
                ('time_block', models.CharField(max_length=10)),
                ('idempotency_key', models.CharField(max_length=100)),
                ('session_ref', models.CharField(db_index=True, max_length=100)),
                ('phone10', models.CharField(blank=True, max_length=10)),
                ('start_time', models.DateTimeField(blank=True, null=True)),
                ('over_capacity', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('expires_at', models.DateTimeField()),
                ('released_at', models.DateTimeField(blank=True, null=True)),
                ('consumed_at', models.DateTimeField(blank=True, null=True)),
                ('consumed_by_lead', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to='maps.lead')),
                ('organization', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='maps.organization')),
            ],
            options={
                'abstract': False,
                'base_manager_name': 'all_objects',
                'indexes': [models.Index(fields=['organization', 'date', 'time_block'], name='slot_claim_block_idx')],
                'constraints': [models.UniqueConstraint(fields=('organization', 'idempotency_key'), name='slot_claim_idempotency_unique')],
            },
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
        # Reversing runs the operations backwards, so this one runs first then.
        migrations.RunPython(migrations.RunPython.noop, set_lock_timeout),
    ]
