from django.db import models


class Lead(models.Model):
    address = models.CharField(max_length=500)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    from_number = models.CharField(max_length=20, blank=True)
    raw_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.address} ({self.created_at:%m/%d/%Y})"
