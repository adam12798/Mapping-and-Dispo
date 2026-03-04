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
        ('credit_fail', 'Credit Fail'),
        ('cancel_door', 'Cancel at Door'),
        ('cpfu', 'CPFU'),
        ('rep_no_show', 'Rep No Show'),
        ('no_coverage', 'No Coverage'),
    ]

    address = models.CharField(max_length=500)
    city = models.CharField(max_length=200, blank=True)
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
