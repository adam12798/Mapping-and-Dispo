from django.db import migrations, models
from django.utils.text import slugify
import django.db.models.deletion


def populate_slugs(apps, schema_editor):
    APITenant = apps.get_model('maps', 'APITenant')
    for tenant in APITenant.objects.all():
        base = slugify(tenant.name) or f'tenant-{tenant.pk}'
        slug = base
        counter = 2
        while APITenant.objects.filter(slug=slug).exclude(pk=tenant.pk).exists():
            slug = f'{base}-{counter}'
            counter += 1
        tenant.slug = slug
        tenant.save(update_fields=['slug'])


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0032_ghl_webhook_log'),
    ]

    operations = [
        # Clean up any leftover indexes/columns from a previously failed run
        migrations.RunSQL(
            "DROP INDEX IF EXISTS maps_apitenant_slug_da3ef2f4_like;",
            migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            "ALTER TABLE maps_apitenant DROP COLUMN IF EXISTS slug;",
            migrations.RunSQL.noop,
        ),
        # Add slug as plain CharField first (no _like index)
        migrations.AddField(
            model_name='apitenant',
            name='slug',
            field=models.CharField(blank=True, default='', max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='apitenant',
            name='company_name',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='logo_url',
            field=models.URLField(blank=True, max_length=500),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='color_primary',
            field=models.CharField(default='#293241', max_length=7),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='color_secondary',
            field=models.CharField(default='#3d5a80', max_length=7),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='color_accent',
            field=models.CharField(default='#ee6c4d', max_length=7),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='color_bg',
            field=models.CharField(default='#293241', max_length=7),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='color_text',
            field=models.CharField(default='#e0fbfc', max_length=7),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='color_text_muted',
            field=models.CharField(default='#98c1d9', max_length=7),
        ),
        migrations.AddField(
            model_name='apitenant',
            name='font_family',
            field=models.CharField(default='Montserrat', max_length=200),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='tenant',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='users', to='maps.apitenant'),
        ),
        # Populate slugs for existing rows before adding unique constraint
        migrations.RunPython(populate_slugs, migrations.RunPython.noop),
        # Now convert to SlugField with unique (creates _like index only once)
        migrations.AlterField(
            model_name='apitenant',
            name='slug',
            field=models.SlugField(blank=True, max_length=100, unique=True),
        ),
    ]
