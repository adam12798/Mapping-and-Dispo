from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0032_ghl_webhook_log'),
    ]

    operations = [
        migrations.AddField(
            model_name='apitenant',
            name='slug',
            field=models.SlugField(blank=True, default='', max_length=100, unique=False),
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
        migrations.AlterField(
            model_name='apitenant',
            name='slug',
            field=models.SlugField(blank=True, max_length=100, unique=True),
        ),
    ]
