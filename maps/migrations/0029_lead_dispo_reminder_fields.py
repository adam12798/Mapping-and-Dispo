from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0028_add_appt_notes_field'),
    ]

    operations = [
        migrations.AddField(
            model_name='lead',
            name='dispo_reminder_sent_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='lead',
            name='dispo_call_made_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
