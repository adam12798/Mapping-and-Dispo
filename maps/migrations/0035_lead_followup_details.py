from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0034_sms_consent'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='follow_up_time',
            field=models.TimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='follow_up_reminder_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='monthly_cost',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='lead',
            name='total_cost',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='lead',
            name='adders',
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='post_appt_notes',
            field=models.TextField(blank=True),
        ),
    ]
