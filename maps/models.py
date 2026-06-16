import uuid

from django.contrib.auth.models import User
from django.db import models


class Lead(models.Model):
    APPOINTMENT_TYPE_CHOICES = [
        ('solar', 'Solar'),
        ('hvac', 'HVAC'),
        ('both', 'Both'),
    ]
    APPOINTMENT_FORMAT_CHOICES = [
        ('in_person', 'In Person'),
        ('virtual', 'Virtual'),
    ]
    DISPOSITION_CHOICES = [
        ('sale', 'Sale'),
        ('no_sale', 'No Sale'),
        ('follow_up', 'Follow Up'),
        ('credit_fail', 'Credit Fail'),
        ('cancel_door', 'Cancel at Door'),
        ('cpfu', 'CPFU'),
        ('rep_no_show', 'Rep No Show'),
        ('no_coverage', 'No Coverage'),
        ('needs_reschedule', 'Needs Reschedule'),
        ('incomplete_deal', 'Incomplete Deal'),
        ('future_contact', 'Future Contact'),
        ('dq', 'DQ'),
        ('no_show', 'No Show'),
    ]

    address = models.CharField(max_length=500)
    city = models.CharField(max_length=200, blank=True)
    state = models.CharField(max_length=50, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    from_number = models.CharField(max_length=20, blank=True)
    raw_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # CRM fields
    homeowner_name = models.CharField(max_length=200, blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    source = models.CharField(max_length=200, blank=True)
    tags = models.CharField(max_length=200, blank=True)
    appointment_type = models.CharField(max_length=10, choices=APPOINTMENT_TYPE_CHOICES, blank=True)
    appointment_format = models.CharField(max_length=10, choices=APPOINTMENT_FORMAT_CHOICES, blank=True)
    appointment_datetime = models.DateTimeField(null=True, blank=True)
    rep = models.ForeignKey('Rep', null=True, blank=True, on_delete=models.SET_NULL, related_name='leads')
    disposition = models.CharField(max_length=20, choices=DISPOSITION_CHOICES, blank=True)
    sat = models.BooleanField(null=True, blank=True)
    follow_up_date = models.DateField(null=True, blank=True)
    call_notes = models.CharField(max_length=200, blank=True)
    appt_notes = models.TextField(blank=True)
    call_transcript = models.TextField(blank=True)
    cancelled = models.BooleanField(default=False)
    dispo_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    dispo_call_made_at = models.DateTimeField(null=True, blank=True)
    textblast_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.address} ({self.created_at:%m/%d/%Y})"


class Rep(models.Model):
    SPECIALTY_CHOICES = [
        ('solar', 'Solar'),
        ('hvac', 'HVAC'),
        ('both', 'Both'),
    ]

    name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=20, blank=True)
    home_address = models.CharField(max_length=500)
    city = models.CharField(max_length=200, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    specialty = models.CharField(max_length=10, choices=SPECIALTY_CHOICES, blank=True)
    rating = models.IntegerField(default=0)
    color = models.CharField(max_length=7, default='#2980b9')
    is_active = models.BooleanField(default=True)
    textblast_eligible = models.BooleanField(default=False)
    sms_consent = models.BooleanField(default=False)
    sms_consent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class Manager(models.Model):
    name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=20)

    def __str__(self):
        return f"{self.name} ({self.phone_number})"


class TimeOffRequest(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('denied', 'Denied'),
    ]

    rep = models.ForeignKey(Rep, on_delete=models.CASCADE, related_name='time_off_requests')
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField(null=True, blank=True)  # null = full day off
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    raw_message = models.TextField(blank=True)

    def __str__(self):
        time_str = 'All Day' if not self.start_time else f'{self.start_time:%I:%M %p} - {self.end_time:%I:%M %p}'
        if self.end_date and self.end_date != self.start_date:
            return f"{self.rep.name} — {self.start_date:%m/%d/%Y} to {self.end_date:%m/%d/%Y} {time_str} ({self.status})"
        elif not self.end_date:
            return f"{self.rep.name} — {self.start_date:%m/%d/%Y} onwards {time_str} ({self.status})"
        return f"{self.rep.name} — {self.start_date:%m/%d/%Y} {time_str} ({self.status})"


class VoiceCallLog(models.Model):
    rep = models.ForeignKey(Rep, null=True, blank=True, on_delete=models.SET_NULL, related_name='voice_calls')
    caller_number = models.CharField(max_length=20)
    twilio_call_sid = models.CharField(max_length=64, blank=True)
    transcript = models.TextField(blank=True)
    summary = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        name = self.rep.name if self.rep else self.caller_number
        return f"Voice call from {name} ({self.created_at:%m/%d/%Y %I:%M %p})"


class UserProfile(models.Model):
    ROLE_CHOICES = [
        ('manager', 'Manager'),
        ('rep', 'Rep'),
        ('provider', 'Provider'),
    ]
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='rep')
    rep = models.OneToOneField(Rep, null=True, blank=True, on_delete=models.SET_NULL, related_name='user_profile')
    tenant = models.ForeignKey('APITenant', null=True, blank=True, on_delete=models.SET_NULL, related_name='users')
    lead_sources = models.TextField(blank=True)
    hourly_availability = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.user.username} ({self.role})"

    @property
    def is_manager(self):
        return self.role == 'manager'

    @property
    def is_provider(self):
        return self.role == 'provider'

    def get_lead_sources_list(self):
        if not self.lead_sources:
            return []
        return [s.strip() for s in self.lead_sources.split(',') if s.strip()]


class LeadMessage(models.Model):
    DIRECTION_CHOICES = [
        ('inbound', 'Inbound'),
        ('outbound', 'Outbound'),
    ]
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='messages')
    phone_number = models.CharField(max_length=20)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.direction} {self.phone_number}: {self.body[:50]}"


class LeadUpdate(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='updates')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='lead_updates')
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user.username} on {self.lead} ({self.created_at:%m/%d %I:%M %p})"


class RepCountDefault(models.Model):
    time_block = models.CharField(max_length=10, blank=True, default='')
    count = models.IntegerField(default=3)

    class Meta:
        unique_together = [('time_block',)]

    def __str__(self):
        return f"Default rep count ({self.time_block or 'global'}): {self.count}"

    @classmethod
    def get_default(cls, block_key=''):
        obj, _ = cls.objects.get_or_create(time_block=block_key, defaults={'count': 3})
        return obj.count


class RepCountOverride(models.Model):
    TIME_BLOCK_CHOICES = [
        ('morning', '9-12 PM'),
        ('midday', '12-3 PM'),
        ('afternoon', '3-6 PM'),
        ('evening', '6-9 PM'),
    ]
    date = models.DateField()
    time_block = models.CharField(max_length=10, choices=TIME_BLOCK_CHOICES)
    count = models.IntegerField()

    class Meta:
        unique_together = [('date', 'time_block')]

    def __str__(self):
        return f"{self.date} {self.get_time_block_display()}: {self.count} reps"


class GHLWebhookLog(models.Model):
    WEBHOOK_TYPE_CHOICES = [
        ('disposition', 'Disposition'),
        ('appointment', 'Appointment'),
        ('test', 'Test'),
    ]
    webhook_type = models.CharField(max_length=20, choices=WEBHOOK_TYPE_CHOICES)
    lead = models.ForeignKey('Lead', null=True, blank=True, on_delete=models.SET_NULL)
    lead_name = models.CharField(max_length=200, blank=True)
    source = models.CharField(max_length=50, blank=True)
    url = models.URLField(max_length=500)
    payload = models.TextField(blank=True)
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    success = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        status = 'OK' if self.success else 'FAIL'
        return f"GHL {self.webhook_type} [{status}] {self.lead_name} ({self.created_at:%m/%d %I:%M %p})"


class APITenant(models.Model):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True, blank=True)
    api_key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    rate_limit = models.IntegerField(default=1000, help_text='Requests per hour')
    allowed_origins = models.TextField(blank=True, help_text='Comma-separated allowed CORS origins')
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)

    # Theming
    company_name = models.CharField(max_length=200, blank=True)
    logo_url = models.URLField(max_length=500, blank=True)
    color_primary = models.CharField(max_length=7, default='#293241')
    color_secondary = models.CharField(max_length=7, default='#3d5a80')
    color_accent = models.CharField(max_length=7, default='#ee6c4d')
    color_bg = models.CharField(max_length=7, default='#293241')
    color_text = models.CharField(max_length=7, default='#e0fbfc')
    color_text_muted = models.CharField(max_length=7, default='#98c1d9')
    font_family = models.CharField(max_length=200, default='Montserrat')

    def __str__(self):
        status = 'active' if self.is_active else 'inactive'
        return f"{self.name} ({status})"

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.name)
        if not self.company_name:
            self.company_name = self.name
        super().save(*args, **kwargs)

    def get_allowed_origins(self):
        if not self.allowed_origins:
            return []
        return [o.strip() for o in self.allowed_origins.split(',') if o.strip()]

    def get_theme(self):
        return {
            'company_name': self.company_name or self.name,
            'logo_url': self.logo_url,
            'color_primary': self.color_primary,
            'color_secondary': self.color_secondary,
            'color_accent': self.color_accent,
            'color_bg': self.color_bg,
            'color_text': self.color_text,
            'color_text_muted': self.color_text_muted,
            'font_family': self.font_family,
        }
