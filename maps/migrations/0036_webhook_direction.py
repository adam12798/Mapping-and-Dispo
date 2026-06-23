from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0035_lead_followup_details'),
    ]

    operations = [
        migrations.AddField(
            model_name='ghlwebhooklog',
            name='direction',
            field=models.CharField(choices=[('outbound', 'Outbound'), ('inbound', 'Inbound')], default='outbound', max_length=10),
        ),
        migrations.AlterField(
            model_name='ghlwebhooklog',
            name='webhook_type',
            field=models.CharField(choices=[('disposition', 'Disposition'), ('appointment', 'Appointment'), ('reschedule', 'Reschedule'), ('cancel', 'Cancel'), ('update', 'Update'), ('test', 'Test')], max_length=20),
        ),
        migrations.AlterField(
            model_name='ghlwebhooklog',
            name='url',
            field=models.URLField(blank=True, max_length=500),
        ),
    ]
