import json
import re
import urllib.parse
import urllib.request

from dateutil import parser as dateparser

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Lead, Rep


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
    try:
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
    except Exception:
        pass
    return None, None


def crm_view(request):
    leads = Lead.objects.order_by('-created_at')
    return render(request, 'maps/crm.html', {'leads': leads})


@csrf_exempt
def lead_update(request, pk):
    """Update or delete a lead's CRM fields."""
    if request.method == 'DELETE':
        lead = get_object_or_404(Lead, pk=pk)
        lead.delete()
        return JsonResponse({'status': 'ok'})
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
def leads_bulk_delete(request):
    """Delete multiple leads by ID."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    ids = data.get('ids', [])
    Lead.objects.filter(id__in=ids).delete()
    return JsonResponse({'status': 'ok'})


def reps_view(request):
    reps = Rep.objects.order_by('name')
    return render(request, 'maps/reps.html', {'reps': reps})


@csrf_exempt
@require_POST
def rep_create(request):
    """Create a new rep, geocoding their home address."""
    data = json.loads(request.body)
    home_address = data.get('home_address', '')
    city = data.get('city', '')
    geocode_address = home_address
    if city:
        geocode_address = f"{home_address}, {city}, MA"
    lat, lng = geocode(geocode_address) if home_address else (None, None)
    rep = Rep.objects.create(
        name=data.get('name', ''),
        phone_number=data.get('phone_number', ''),
        home_address=home_address,
        city=city,
        latitude=lat,
        longitude=lng,
        specialty=data.get('specialty', ''),
    )
    return JsonResponse({'status': 'ok', 'id': rep.id})


@csrf_exempt
def rep_update(request, pk):
    """Update or delete a rep."""
    if request.method == 'DELETE':
        rep = get_object_or_404(Rep, pk=pk)
        rep.delete()
        return JsonResponse({'status': 'ok'})
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    rep = get_object_or_404(Rep, pk=pk)
    data = json.loads(request.body)
    allowed_fields = ['name', 'phone_number', 'home_address', 'city', 'specialty']
    for field in allowed_fields:
        if field in data:
            setattr(rep, field, data[field])
    # Re-geocode if address or city changed
    if 'home_address' in data or 'city' in data:
        geocode_address = rep.home_address
        if rep.city:
            geocode_address = f"{rep.home_address}, {rep.city}, MA"
        rep.latitude, rep.longitude = geocode(geocode_address) if rep.home_address else (None, None)
    rep.save()
    return JsonResponse({'status': 'ok'})


@csrf_exempt
def reps_bulk_delete(request):
    """Delete multiple reps by ID."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    ids = data.get('ids', [])
    Rep.objects.filter(id__in=ids).delete()
    return JsonResponse({'status': 'ok'})


def parse_sms_fields(body):
    """Parse structured SMS line by line, matching 'label: value' patterns."""
    # Strip unsubscribe footers
    body = re.sub(r'(?i)reply\s*"?\d+"?\s*to\s+unsubscribe.*$', '', body).strip()

    # Map various label names to our field names
    label_map = {
        'name': 'name',
        'phone': 'phone',
        'phone number': 'phone',
        'address': 'address',
        'street address': 'address',
        'city': 'city',
        'state': 'state',
        'type': 'type',
        'appointment type': 'type',
        'appt type': 'type',
        'format': 'format',
        'meeting type': 'format',
        'appointment format': 'format',
        'day and time': 'appointment_datetime',
        'date and time': 'appointment_datetime',
        'date': 'appointment_datetime',
        'time': 'appointment_datetime',
    }

    fields = {}
    for line in body.split('\n'):
        line = line.strip()
        if ':' not in line:
            continue
        # Split on first colon only
        label_part, _, value_part = line.partition(':')
        label = label_part.strip().lower()
        value = value_part.strip()
        if label in label_map and value:
            fields[label_map[label]] = value
    return fields


def normalize_type(value):
    v = value.lower().strip()
    if 'both' in v or ('solar' in v and 'hvac' in v):
        return 'both'
    if 'solar' in v:
        return 'solar'
    if 'hvac' in v:
        return 'hvac'
    return ''


def normalize_format(value):
    v = value.lower().strip()
    if 'in' in v and 'person' in v:
        return 'in_person'
    if 'virtual' in v:
        return 'virtual'
    return ''


@csrf_exempt
@require_POST
def sms_webhook(request):
    """Twilio webhook — receives incoming SMS, parses fields, geocodes address, saves as Lead."""
    body = request.POST.get('Body', '').strip()
    from_number = request.POST.get('From', '')

    try:
        if body:
            fields = parse_sms_fields(body)

            # If structured fields found, use them; otherwise treat whole body as address
            address = fields.get('address', body if not fields else '')
            city = fields.get('city', '')

            # Build full address for geocoding
            geocode_address = address
            if city:
                geocode_address = f"{address}, {city}, MA"

            lat, lng = geocode(geocode_address) if address else (None, None)

            # Parse appointment datetime
            appt_datetime = None
            raw_datetime = fields.get('appointment_datetime', '')
            if raw_datetime:
                try:
                    appt_datetime = dateparser.parse(raw_datetime, fuzzy=True)
                except (ValueError, OverflowError):
                    pass

            Lead.objects.create(
                address=address,
                city=city,
                latitude=lat,
                longitude=lng,
                from_number=from_number,
                homeowner_name=fields.get('name', ''),
                phone_number=fields.get('phone', ''),
                appointment_type=normalize_type(fields.get('type', '')),
                appointment_format=normalize_format(fields.get('format', '')),
                appointment_datetime=appt_datetime,
                raw_message=body,
            )
    except Exception:
        # Always save the lead even if parsing fails
        Lead.objects.create(
            address=body,
            from_number=from_number,
            raw_message=body,
        )

    # Return empty TwiML response
    return HttpResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        content_type='text/xml',
    )
