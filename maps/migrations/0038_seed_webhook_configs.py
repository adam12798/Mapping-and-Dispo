from django.db import migrations


def seed_configs(apps, schema_editor):
    WebhookConfig = apps.get_model('maps', 'WebhookConfig')

    # Only seed if no configs exist yet
    if WebhookConfig.objects.exists():
        return

    WebhookConfig.objects.create(
        name='GHL Dispo Update',
        trigger='disposition_changed',
        url='https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/92de7dff-cf7a-4727-92f7-b88e26c515cd',
        method='POST',
        fields=['source', 'phone_number', 'homeowner_name', 'disposition', 'call_transcript'],
        headers=[{'key': 'Content-Type', 'value': 'application/json'}],
        is_active=True,
    )

    WebhookConfig.objects.create(
        name='GHL Appt Update',
        trigger='appointment_changed',
        url='https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/bc69b54d-d701-432f-82be-80d8dcfa799b',
        method='POST',
        fields=['source', 'phone_number', 'appointment_type', 'appointment_datetime'],
        headers=[{'key': 'Content-Type', 'value': 'application/json'}],
        is_active=True,
    )


def reverse(apps, schema_editor):
    WebhookConfig = apps.get_model('maps', 'WebhookConfig')
    WebhookConfig.objects.filter(name__in=['GHL Dispo Update', 'GHL Appt Update']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0037_webhook_config'),
    ]

    operations = [
        migrations.RunPython(seed_configs, reverse),
    ]
