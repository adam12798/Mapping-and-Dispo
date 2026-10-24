from django.db import migrations, models


class Migration(migrations.Migration):
    """A column only: every organization reads False, which changes nothing
    (maps/alfred_org.py). No row is updated or deleted."""

    dependencies = [
        ('maps', '0043_alfred_text'),
    ]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='alfred_declines',
            field=models.BooleanField(default=False),
        ),
    ]
