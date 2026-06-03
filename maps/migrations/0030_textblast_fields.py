from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0029_lead_dispo_reminder_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='rep',
            name='textblast_eligible',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='lead',
            name='textblast_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
