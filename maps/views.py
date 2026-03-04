import json
import re
import urllib.parse
import urllib.request

from datetime import datetime

from dateutil import parser as dateparser

from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .assignment import auto_assign_leads
from .models import Lead, Rep


def index(request):
    return render(request, 'maps/index.html')


def leads_api(request):
    """Return all leads as JSON for the map to plot."""
    leads = Lead.objects.filter(latitude__isnull=False).select_related('rep').order_by('-created_at')
    data = [
        {
            'id': lead.id,
            'address': lead.address,
            'city': lead.city,
            'lat': lead.latitude,
            'lng': lead.longitude,
            'from_number': lead.from_number,
            'homeowner_name': lead.homeowner_name,
            'phone_number': lead.phone_number,
            'appointment_type': lead.appointment_type,
            'appointment_format': lead.appointment_format,
            'appointment_datetime': lead.appointment_datetime.strftime('%m/%d/%Y %I:%M %p') if lead.appointment_datetime else '',
            'created_at': lead.created_at.strftime('%m/%d/%Y %I:%M %p'),
            'rep_id': lead.rep_id,
            'rep_name': lead.rep.name if lead.rep else '',
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
    reps = Rep.objects.order_by('-rating', 'name')
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
        color=data.get('color', '#2980b9'),
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
    allowed_fields = ['name', 'phone_number', 'home_address', 'city', 'specialty', 'rating', 'color']
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


def reps_api(request):
    """Return all reps as JSON."""
    reps = Rep.objects.order_by('-rating', 'name')
    data = [
        {
            'id': rep.id,
            'name': rep.name,
            'lat': rep.latitude,
            'lng': rep.longitude,
            'home_address': rep.home_address,
            'city': rep.city,
            'color': rep.color,
        }
        for rep in reps
    ]
    return JsonResponse(data, safe=False)


def route_api(request):
    """Return ordered route stops for a given date.

    If leads have rep assignments, returns per-rep routes.
    Otherwise falls back to single-rep mode (highest rated).
    """
    date_str = request.GET.get('date', '')
    if not date_str:
        return JsonResponse({'error': 'date parameter required'}, status=400)
    try:
        date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format, use YYYY-MM-DD'}, status=400)

    leads = Lead.objects.filter(
        appointment_datetime__date=date,
        latitude__isnull=False,
    ).select_related('rep').order_by('appointment_datetime')

    assigned_leads = [l for l in leads if l.rep is not None]

    if assigned_leads:
        from collections import defaultdict
        rep_groups = defaultdict(list)
        for lead in assigned_leads:
            rep_groups[lead.rep_id].append(lead)

        routes = []
        for rep_id, rep_leads in rep_groups.items():
            rep = rep_leads[0].rep
            stops = [
                {
                    'name': lead.homeowner_name or lead.address,
                    'address': lead.address,
                    'city': lead.city,
                    'time': lead.appointment_datetime.strftime('%I:%M %p'),
                    'type': lead.appointment_type,
                    'lat': lead.latitude,
                    'lng': lead.longitude,
                }
                for lead in rep_leads
            ]
            routes.append({
                'rep': {
                    'name': rep.name,
                    'lat': rep.latitude,
                    'lng': rep.longitude,
                    'home_address': rep.home_address,
                    'color': rep.color,
                },
                'stops': stops,
            })

        return JsonResponse({'routes': routes})
    else:
        stops = [
            {
                'name': lead.homeowner_name or lead.address,
                'address': lead.address,
                'city': lead.city,
                'time': lead.appointment_datetime.strftime('%I:%M %p'),
                'type': lead.appointment_type,
                'lat': lead.latitude,
                'lng': lead.longitude,
            }
            for lead in leads
        ]
        rep_data = None
        rep = Rep.objects.filter(latitude__isnull=False).order_by('-rating').first()
        if rep:
            rep_data = {
                'name': rep.name,
                'lat': rep.latitude,
                'lng': rep.longitude,
                'home_address': rep.home_address,
                'color': rep.color,
            }
        return JsonResponse({'rep': rep_data, 'stops': stops})


@csrf_exempt
@require_POST
def auto_assign_api(request):
    """Trigger auto-assignment for a target date."""
    data = json.loads(request.body)
    date_str = data.get('date', '')
    if not date_str:
        return JsonResponse({'error': 'date parameter required'}, status=400)
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format'}, status=400)

    result = auto_assign_leads(target_date)

    assignments_data = []
    for assignment in result['assignments']:
        rep = assignment['rep']
        stops = []
        for lead, arrival_time in assignment['stops']:
            stops.append({
                'lead_id': lead.id,
                'name': lead.homeowner_name or lead.address,
                'address': lead.address,
                'city': lead.city,
                'type': lead.appointment_type,
                'lat': lead.latitude,
                'lng': lead.longitude,
                'estimated_arrival': arrival_time.strftime('%I:%M %p'),
            })
        assignments_data.append({
            'rep': {
                'id': rep.id,
                'name': rep.name,
                'color': rep.color,
                'lat': rep.latitude,
                'lng': rep.longitude,
                'home_address': rep.home_address,
            },
            'stops': stops,
        })

    unassigned_data = [
        {
            'lead_id': l.id,
            'name': l.homeowner_name or l.address,
            'address': l.address,
            'type': l.appointment_type,
        }
        for l in result['unassigned']
    ]

    return JsonResponse({
        'assignments': assignments_data,
        'unassigned': unassigned_data,
        'summary': {
            'total_leads': len(result['unassigned']) + sum(
                len(a['stops']) for a in result['assignments']
            ),
            'assigned': sum(len(a['stops']) for a in result['assignments']),
            'unassigned': len(result['unassigned']),
            'reps_used': len(assignments_data),
        },
    })


@csrf_exempt
@require_POST
def clear_assignments_api(request):
    """Clear all rep assignments for a given date."""
    data = json.loads(request.body)
    date_str = data.get('date', '')
    if not date_str:
        return JsonResponse({'error': 'date parameter required'}, status=400)
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format'}, status=400)

    count = Lead.objects.filter(
        appointment_datetime__date=target_date,
        rep__isnull=False,
    ).update(rep=None)

    return JsonResponse({'status': 'ok', 'cleared': count})


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
