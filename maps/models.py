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
    ]
    DISPO_COLORS = {
        'sale': '#27ae60', 'no_sale': '#8e44ad', 'follow_up': '#e67e22',
        'credit_fail': '#ff69b4', 'cancel_door': '#95a5a6', 'cpfu': '#98c1d9',
        'rep_no_show': '#111111', 'no_coverage': '#c0392b', 'needs_reschedule': '#3498db',
        'incomplete_deal': '#d4a017', 'future_contact': '#1abc9c',
    }
    DISPO_LABELS = {k: v for k, v in DISPOSITION_CHOICES}

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
    appointment_type = models.CharField(max_length=10, choices=APPOINTMENT_TYPE_CHOICES, blank=True)
    appointment_format = models.CharField(max_length=10, choices=APPOINTMENT_FORMAT_CHOICES, blank=True)
    appointment_datetime = models.DateTimeField(null=True, blank=True)
    rep = models.ForeignKey('Rep', null=True, blank=True, on_delete=models.SET_NULL, related_name='leads')
    disposition = models.CharField(max_length=20, choices=DISPOSITION_CHOICES, blank=True)
    sat = models.BooleanField(null=True, blank=True)
    follow_up_date = models.DateField(null=True, blank=True)
    call_notes = models.CharField(max_length=200, blank=True)
    call_transcript = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['appointment_datetime']),
            models.Index(fields=['rep']),
            models.Index(fields=['disposition']),
        ]

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
    date = models.DateField()
    start_time = models.TimeField(null=True, blank=True)  # null = full day off
    end_time = models.TimeField(null=True, blank=True)
    reason = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)
    raw_message = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['rep', 'date', 'status']),
        ]

    def __str__(self):
        time_str = 'All Day' if not self.start_time else f'{self.start_time:%I:%M %p} - {self.end_time:%I:%M %p}'
        return f"{self.rep.name} — {self.date:%m/%d/%Y} {time_str} ({self.status})"


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
