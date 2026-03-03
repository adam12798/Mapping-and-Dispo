import json
import urllib.parse
import urllib.request

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Lead


def index(request):
    return render(request, 'maps/index.html')


def leads_api(request):
    """Return all leads as JSON for the map to plot."""
    leads = Lead.objects.filter(latitude__isnull=False).order_by('-created_at')
    data = [
        {
            'id': lead.id,
            'address': lead.address,
            'lat': lead.latitude,
            'lng': lead.longitude,
            'from_number': lead.from_number,
            'created_at': lead.created_at.strftime('%m/%d/%Y %I:%M %p'),
        }
        for lead in leads
    ]
    return JsonResponse(data, safe=False)


def geocode(address):
    """Geocode an address using Nominatim (free, no API key)."""
    params = urllib.parse.urlencode({
        'q': address,
        'format': 'json',
        'limit': 1,
        'countrycodes': 'us',
    })
    url = f'https://nominatim.openstreetmap.org/search?{params}'
    req = urllib.request.Request(url, headers={'User-Agent': 'MappingDispo/1.0'})
    with urllib.request.urlopen(req, timeout=10) as resp:
        results = json.loads(resp.read())
    if results:
        return float(results[0]['lat']), float(results[0]['lon'])
    return None, None


def crm_view(request):
    leads = Lead.objects.order_by('-created_at')
    return render(request, 'maps/crm.html', {'leads': leads})


@csrf_exempt
def lead_update(request, pk):
    """Update a lead's CRM fields."""
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    lead = get_object_or_404(Lead, pk=pk)
    data = json.loads(request.body)
    allowed_fields = [
        'homeowner_name', 'phone_number', 'city',
        'appointment_type', 'appointment_format', 'appointment_datetime',
    ]
    for field in allowed_fields:
        if field in data:
            value = data[field]
            if field == 'appointment_datetime' and value == '':
                value = None
            setattr(lead, field, value)
    lead.save()
    return JsonResponse({'status': 'ok'})


@csrf_exempt
@require_POST
def sms_webhook(request):
    """Twilio webhook — receives incoming SMS, geocodes address, saves as Lead."""
    body = request.POST.get('Body', '').strip()
    from_number = request.POST.get('From', '')

    if body:
        lat, lng = geocode(body)
        Lead.objects.create(
            address=body,
            latitude=lat,
            longitude=lng,
            from_number=from_number,
            raw_message=body,
        )

    # Return empty TwiML response
    return HttpResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        content_type='text/xml',
    )
