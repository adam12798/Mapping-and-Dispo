from django.db import migrations, models


class Migration(migrations.Migration):
    """A column only: every organization reads False, so Sutton's reminder
    worker runs as before (L12). No row is updated or deleted."""

    dependencies = [
        ('maps', '0044_organization_alfred_declines'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='reminders_by_wednesday',
            field=models.BooleanField(default=False),
        ),
    ]
