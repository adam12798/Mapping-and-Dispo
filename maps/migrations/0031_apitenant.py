import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('maps', '0030_textblast_fields'),
    ]

    operations = [
        migrations.CreateModel(
            name='APITenant',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('api_key', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ('is_active', models.BooleanField(default=True)),
                ('rate_limit', models.IntegerField(default=1000, help_text='Requests per hour')),
                ('allowed_origins', models.TextField(blank=True, help_text='Comma-separated allowed CORS origins')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('last_used_at', models.DateTimeField(blank=True, null=True)),
                ('notes', models.TextField(blank=True)),
            ],
        ),
    ]
