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

from django.conf import settings

from .assignment import auto_assign_leads
from .models import Lead, Rep, TimeOffRequest, Manager


def twilio_check(request):
    """Quick check if Twilio env vars are loaded (no secrets exposed)."""
    return JsonResponse({
        'sid_set': bool(settings.TWILIO_ACCOUNT_SID),
        'token_set': bool(settings.TWILIO_AUTH_TOKEN),
        'phone_set': bool(settings.TWILIO_PHONE_NUMBER),
    })


def index(request):
    return render(request, 'maps/index.html')


def leads_api(request):
    """Return all leads as JSON for the map to plot."""
    from django.utils import timezone as tz
    eastern = tz.get_fixed_timezone(-300)  # EST = UTC-5
    try:
        import zoneinfo
        eastern = zoneinfo.ZoneInfo('America/New_York')
    except ImportError:
        pass

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
            'appointment_datetime': lead.appointment_datetime.astimezone(eastern).strftime('%m/%d/%Y %I:%M %p') if lead.appointment_datetime else '',
            'created_at': lead.created_at.astimezone(eastern).strftime('%m/%d/%Y %I:%M %p'),
            'rep_id': lead.rep_id,
            'rep_name': lead.rep.name if lead.rep else '',
        }
        for lead in leads
    ]
    return JsonResponse(data, safe=False)


def send_sms(to, body):
    """Send an SMS via Twilio REST API."""
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return
    url = f'https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json'
    data = urllib.parse.urlencode({
        'To': to,
        'From': settings.TWILIO_PHONE_NUMBER,
        'Body': body,
    }).encode()
    req = urllib.request.Request(url, data=data)
    credentials = f'{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}'
    import base64
    auth = base64.b64encode(credentials.encode()).decode()
    req.add_header('Authorization', f'Basic {auth}')
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def notify_managers_time_off(time_off_request):
    """Text all managers about a new time off request."""
    tor = time_off_request
    time_str = 'All Day' if not tor.start_time else f'{tor.start_time:%I:%M %p} - {tor.end_time:%I:%M %p}'
    reason_str = f' — {tor.reason}' if tor.reason else ''
    body = (
        f'Time Off Request #{tor.id}\n'
        f'{tor.rep.name} requests {tor.date:%m/%d/%Y} {time_str}{reason_str}\n\n'
        f'Reply "APPROVE {tor.id}" or "DENY {tor.id}"'
    )
    for manager in Manager.objects.all():
        send_sms(manager.phone_number, body)


def is_in_massachusetts(lat, lng):
    """Check if coordinates fall within Massachusetts bounding box."""
    return 41.0 <= lat <= 43.0 and -73.6 <= lng <= -69.8


def geocode(address):
    """Geocode an address using Nominatim (free, no API key).

    Validates results are in Massachusetts. If not, retries with
    ', Massachusetts' appended. Returns (None, None) if still outside MA.
    """
    def _nominatim_search(query):
        try:
            params = urllib.parse.urlencode({
                'q': query,
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

    lat, lng = _nominatim_search(address)

    # If result is outside MA, retry with Massachusetts explicitly
    if lat is not None and not is_in_massachusetts(lat, lng):
        retry_address = f"{address}, Massachusetts"
        lat2, lng2 = _nominatim_search(retry_address)
        if lat2 is not None and is_in_massachusetts(lat2, lng2):
            return lat2, lng2

        # Last resort: try just the city/town from the address
        # e.g. "848 main st, beverly, MA" → try "beverly, Massachusetts"
        parts = address.split(',')
        if len(parts) >= 2:
            city_part = parts[-2].strip() if 'MA' in parts[-1].upper() else parts[-1].strip()
            city_lat, city_lng = _nominatim_search(f"{city_part}, Massachusetts")
            if city_lat is not None and is_in_massachusetts(city_lat, city_lng):
                return city_lat, city_lng

        return None, None

    # No result at all — try with Massachusetts appended
    if lat is None:
        retry_address = f"{address}, Massachusetts"
        lat2, lng2 = _nominatim_search(retry_address)
        if lat2 is not None and is_in_massachusetts(lat2, lng2):
            return lat2, lng2

    return lat, lng


def crm_view(request):
    leads = Lead.objects.select_related('rep').order_by('-created_at')
    reps = Rep.objects.order_by('name')
    return render(request, 'maps/crm.html', {
        'leads': leads, 'reps': reps,
        'dispo_colors_json': json.dumps(Lead.DISPO_COLORS),
    })


def daily_view(request):
    from datetime import date as dt_date
    selected_date = request.GET.get('date', dt_date.today().isoformat())
    leads = Lead.objects.select_related('rep').filter(
        appointment_datetime__date=selected_date
    ).order_by('appointment_datetime')
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/daily.html', {
        'leads': leads,
        'reps': reps,
        'selected_date': selected_date,
        'dispo_colors_json': json.dumps(Lead.DISPO_COLORS),
    })


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
        'homeowner_name', 'phone_number', 'address', 'city', 'state',
        'appointment_type', 'appointment_format', 'appointment_datetime',
        'disposition', 'sat', 'follow_up_date', 'call_notes', 'call_transcript',
    ]
    if 'rep_id' in data:
        rep_val = data['rep_id']
        lead.rep_id = int(rep_val) if rep_val else None
    for field in allowed_fields:
        if field in data:
            value = data[field]
            if field in ('appointment_datetime', 'follow_up_date') and value == '':
                value = None
            if field == 'sat':
                value = {'true': True, 'false': False, 'yes': True, 'no': False}.get(str(value).lower().strip()) if value != '' else None
            setattr(lead, field, value)
    # Re-geocode if address or city changed
    if 'address' in data or 'city' in data:
        geocode_address = lead.address
        if lead.city:
            geocode_address = f"{lead.address}, {lead.city}, MA"
        lead.latitude, lead.longitude = geocode(geocode_address) if lead.address else (None, None)
    lead.save()

    # Send webhook to Go High Level if disposition was updated
    if 'disposition' in data:
        import logging
        ghl_logger = logging.getLogger('ghl_webhook')
        try:
            ghl_payload = json.dumps({
                'phone': lead.phone_number,
                'name': lead.homeowner_name,
                'disposition': lead.disposition or '',
                'call_transcript': lead.call_transcript or '',
            }).encode()
            ghl_req = urllib.request.Request(
                settings.GHL_WEBHOOK_URL,
                data=ghl_payload,
                headers={'Content-Type': 'application/json'},
            )
            resp = urllib.request.urlopen(ghl_req, timeout=10)
            ghl_logger.info(f'GHL webhook sent for lead {pk}: status {resp.status}')
        except Exception as e:
            ghl_logger.error(f'GHL webhook failed for lead {pk}: {e}')

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


@csrf_exempt
def manager_api(request):
    """List, create, or delete managers."""
    if request.method == 'GET':
        managers = list(Manager.objects.all().values('id', 'name', 'phone_number'))
        return JsonResponse({'managers': managers})
    if request.method == 'POST':
        data = json.loads(request.body)
        Manager.objects.create(
            name=data.get('name', ''),
            phone_number=data.get('phone_number', ''),
        )
        return JsonResponse({'status': 'ok'})
    if request.method == 'DELETE':
        data = json.loads(request.body)
        Manager.objects.filter(pk=data.get('id')).delete()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


def time_off_view(request):
    requests = TimeOffRequest.objects.select_related('rep').order_by('-created_at')
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/time_off.html', {'requests': requests, 'reps': reps})


def time_off_by_date_api(request):
    """Return approved time off for a given date."""
    date_str = request.GET.get('date', '')
    if not date_str:
        return JsonResponse({'error': 'date required'}, status=400)
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date'}, status=400)

    reqs = TimeOffRequest.objects.filter(date=target_date, status='approved').select_related('rep')
    data = []
    for r in reqs:
        data.append({
            'rep_name': r.rep.name,
            'rep_color': r.rep.color,
            'all_day': r.start_time is None,
            'start_time': r.start_time.strftime('%I:%M %p') if r.start_time else None,
            'end_time': r.end_time.strftime('%I:%M %p') if r.end_time else None,
            'reason': r.reason,
        })
    return JsonResponse({'time_off': data})


@csrf_exempt
def time_off_api(request):
    """Create a time off request manually."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    rep = get_object_or_404(Rep, pk=data['rep_id'])
    start_time = data.get('start_time') or None
    end_time = data.get('end_time') or None
    TimeOffRequest.objects.create(
        rep=rep,
        date=data['date'],
        start_time=start_time,
        end_time=end_time,
        reason=data.get('reason', ''),
    )
    return JsonResponse({'status': 'ok'})


@csrf_exempt
def time_off_update(request, pk):
    """Update or delete a time off request."""
    tor = get_object_or_404(TimeOffRequest, pk=pk)
    if request.method == 'DELETE':
        tor.delete()
        return JsonResponse({'status': 'ok'})
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    if 'status' in data:
        tor.status = data['status']
    if 'reason' in data:
        tor.reason = data['reason']
    if 'date' in data:
        tor.date = data['date']
    if 'start_time' in data:
        tor.start_time = data['start_time'] or None
    if 'end_time' in data:
        tor.end_time = data['end_time'] or None
    tor.save()
    return JsonResponse({'status': 'ok'})


def reps_view(request):
    active_reps = Rep.objects.filter(is_active=True).order_by('-rating', 'name')
    inactive_reps = Rep.objects.filter(is_active=False).order_by('-rating', 'name')
    return render(request, 'maps/reps.html', {
        'active_reps': active_reps,
        'inactive_reps': inactive_reps,
    })


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
    allowed_fields = ['name', 'phone_number', 'home_address', 'city', 'specialty', 'rating', 'color', 'is_active']
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
    """Return all active reps as JSON."""
    reps = Rep.objects.filter(is_active=True).order_by('-rating', 'name')
    data = [
        {
            'id': rep.id,
            'name': rep.name,
            'lat': rep.latitude,
            'lng': rep.longitude,
            'home_address': rep.home_address,
            'city': rep.city,
            'color': rep.color,
            'phone': rep.phone_number,
        }
        for rep in reps
    ]
    return JsonResponse(data, safe=False)


def route_api(request):
    """Return ordered route stops for a given date.

    If leads have rep assignments, returns per-rep routes.
    Otherwise falls back to single-rep mode (highest rated).
    """
    from django.utils import timezone as tz
    try:
        import zoneinfo
        eastern = zoneinfo.ZoneInfo('America/New_York')
    except ImportError:
        eastern = tz.get_fixed_timezone(-300)

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
                    'time': lead.appointment_datetime.astimezone(eastern).strftime('%I:%M %p'),
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
                'time': lead.appointment_datetime.astimezone(eastern).strftime('%I:%M %p'),
                'type': lead.appointment_type,
                'lat': lead.latitude,
                'lng': lead.longitude,
            }
            for lead in leads
        ]
        rep_data = None
        rep = Rep.objects.filter(latitude__isnull=False, is_active=True).order_by('-rating').first()
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
    from django.utils import timezone as tz
    try:
        import zoneinfo
        eastern = zoneinfo.ZoneInfo('America/New_York')
    except ImportError:
        eastern = tz.get_fixed_timezone(-300)

    data = json.loads(request.body)
    date_str = data.get('date', '')
    if not date_str:
        return JsonResponse({'error': 'date parameter required'}, status=400)
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format'}, status=400)

    result = auto_assign_leads(target_date, save=False)

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
                'phone': lead.phone_number,
                'lat': lead.latitude,
                'lng': lead.longitude,
                'estimated_arrival': arrival_time.strftime('%I:%M %p'),
                'time': lead.appointment_datetime.astimezone(eastern).strftime('%I:%M %p') if lead.appointment_datetime else '',
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
            'city': l.city,
            'type': l.appointment_type,
            'phone': l.phone_number,
            'time': l.appointment_datetime.astimezone(eastern).strftime('%I:%M %p') if l.appointment_datetime else '',
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


@csrf_exempt
@require_POST
def confirm_assignments_api(request):
    """Confirm proposed assignments by saving lead-to-rep mappings."""
    data = json.loads(request.body)
    assignments = data.get('assignments', {})
    if not assignments:
        return JsonResponse({'error': 'No assignments provided'}, status=400)

    count = 0
    for lead_id_str, rep_id in assignments.items():
        Lead.objects.filter(id=int(lead_id_str)).update(rep_id=rep_id)
        count += 1

    return JsonResponse({'status': 'ok', 'confirmed': count})


def dashboard_view(request):
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/dashboard.html', {'reps': reps})


def dashboard_api(request):
    """Return aggregated appointment stats for the dashboard charts."""
    from django.db.models import Count, Q

    start = request.GET.get('start', '')
    end = request.GET.get('end', '')
    rep_ids_raw = request.GET.get('rep_ids', '')
    group_by = request.GET.get('group_by', '')

    qs = Lead.objects.select_related('rep')
    if start:
        qs = qs.filter(appointment_datetime__date__gte=start)
    if end:
        qs = qs.filter(appointment_datetime__date__lte=end)
    if rep_ids_raw:
        rep_ids = [int(x) for x in rep_ids_raw.split(',') if x.strip().isdigit()]
        qs = qs.filter(rep_id__in=rep_ids)

    DISPO_KEYS = [k for k, v in Lead.DISPOSITION_CHOICES]
    DISPO_LABELS = Lead.DISPO_LABELS
    DISPO_COLORS = Lead.DISPO_COLORS
    PRODUCT_COLORS = {'solar': '#f1c40f', 'hvac': '#e74c3c', 'both': '#27ae60'}

    # --- by_disposition ---
    if group_by == 'rep':
        grouped = qs.values('rep__name', 'rep__color', 'disposition').annotate(count=Count('id'))
        pivot = {}
        for row in grouped:
            rep_name = row['rep__name'] or 'Unassigned'
            rep_color = row['rep__color'] or '#98c1d9'
            if rep_name not in pivot:
                pivot[rep_name] = {'color': rep_color, 'counts': {d: 0 for d in DISPO_KEYS}}
            if row['disposition'] in pivot[rep_name]['counts']:
                pivot[rep_name]['counts'][row['disposition']] = row['count']
        dispo_datasets = [
            {'label': name, 'data': [v['counts'][d] for d in DISPO_KEYS], 'backgroundColor': v['color']}
            for name, v in pivot.items()
        ]
    elif group_by == 'product':
        grouped = qs.values('appointment_type', 'disposition').annotate(count=Count('id'))
        pivot = {pt: {d: 0 for d in DISPO_KEYS} for pt in ['solar', 'hvac', 'both']}
        for row in grouped:
            pt = row['appointment_type'] or ''
            if pt in pivot and row['disposition'] in pivot[pt]:
                pivot[pt][row['disposition']] = row['count']
        dispo_datasets = [
            {'label': pt.capitalize(), 'data': [pivot[pt][d] for d in DISPO_KEYS],
             'backgroundColor': PRODUCT_COLORS[pt]}
            for pt in ['solar', 'hvac', 'both']
        ]
    else:
        flat = qs.values('disposition').annotate(count=Count('id'))
        counts = {r['disposition']: r['count'] for r in flat}
        dispo_datasets = [{
            'label': 'All',
            'data': [counts.get(d, 0) for d in DISPO_KEYS],
            'backgroundColor': [DISPO_COLORS[d] for d in DISPO_KEYS],
        }]

    # --- by_rep ---
    rep_rows = list(qs.filter(rep__isnull=False).values('rep__name', 'rep__color').annotate(
        total=Count('id')).order_by('-total'))
    by_rep = {
        'labels': [r['rep__name'] for r in rep_rows],
        'datasets': [{
            'label': 'Appointments',
            'data': [r['total'] for r in rep_rows],
            'backgroundColor': [r['rep__color'] or '#98c1d9' for r in rep_rows],
        }],
    }

    # --- conversion_by_rep ---
    conv_rows = list(qs.filter(rep__isnull=False).values('rep__name', 'rep__color').annotate(
        total=Count('id'), sales=Count('id', filter=Q(disposition='sale'))
    ).order_by('rep__name'))
    by_conversion = {
        'labels': [r['rep__name'] for r in conv_rows],
        'datasets': [{
            'label': 'Sale %',
            'data': [round(r['sales'] / r['total'] * 100, 1) if r['total'] else 0 for r in conv_rows],
            'backgroundColor': [r['rep__color'] or '#98c1d9' for r in conv_rows],
        }],
    }

    # --- by_product ---
    PRODUCT_LABELS = {'solar': 'Solar', 'hvac': 'HVAC', 'both': 'Both'}
    PRODUCT_COLORS_FLAT = {'solar': '#f1c40f', 'hvac': '#e74c3c', 'both': '#27ae60'}
    prod_rows = list(qs.filter(appointment_type__in=['solar', 'hvac', 'both']).values(
        'appointment_type').annotate(count=Count('id')))
    by_product = {
        'labels': [PRODUCT_LABELS.get(r['appointment_type'], r['appointment_type']) for r in prod_rows],
        'datasets': [{
            'label': 'Appointments',
            'data': [r['count'] for r in prod_rows],
            'backgroundColor': [PRODUCT_COLORS_FLAT.get(r['appointment_type'], '#98c1d9') for r in prod_rows],
        }],
    }

    total = qs.count()
    sales = qs.filter(disposition='sale').count()

    return JsonResponse({
        'by_disposition': {'labels': [DISPO_LABELS[d] for d in DISPO_KEYS], 'datasets': dispo_datasets},
        'by_rep': by_rep,
        'conversion_by_rep': by_conversion,
        'by_product': by_product,
        'summary': {
            'total': total,
            'total_sales': sales,
            'conversion_rate': round(sales / total * 100, 1) if total else 0,
        },
    })


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
        'product type': 'type',
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


def parse_time_off_request(body, rep):
    """Parse a time off request from SMS body.

    Expected format:
      Rep Name
      Off Tuesday
      -- or --
      Rep Name
      Busy Wed 12pm-3pm tire appointment

    Returns list of (date, start_time, end_time, reason) tuples, or None if can't parse.
    """
    from datetime import date, time, timedelta
    import calendar

    lines = [l.strip() for l in body.strip().split('\n') if l.strip()]
    if not lines:
        return None

    # If single line, parse the whole message; if multi-line, skip first line (rep name)
    parse_lines = lines if len(lines) == 1 else lines[1:]

    requests = []
    today = date.today()

    for line in parse_lines:
        lower = line.lower()

        # Detect time off keywords
        off_keywords = ['off', 'busy', 'unavailable', 'out', 'vacation', 'pto', "can't work", 'cant work', 'not available', 'time off']
        if not any(kw in lower for kw in off_keywords):
            continue

        # Try to find a date reference
        # Check for day names (monday, tuesday, etc.)
        target_date = None
        day_names = list(calendar.day_name)
        day_abbrs = list(calendar.day_abbr)

        for i, (full, abbr) in enumerate(zip(day_names, day_abbrs)):
            if full.lower() in lower or abbr.lower() in lower:
                # Find next occurrence of this weekday
                days_ahead = i - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                target_date = today + timedelta(days=days_ahead)
                break

        # Check for "today" or "tomorrow"
        if 'today' in lower:
            target_date = today
        elif 'tomorrow' in lower:
            target_date = today + timedelta(days=1)

        # Try to parse a date from the line (e.g. "March 10" or "3/10")
        if target_date is None:
            try:
                parsed = dateparser.parse(line, fuzzy=True)
                if parsed:
                    target_date = parsed.date()
            except (ValueError, OverflowError):
                pass

        if target_date is None:
            continue

        # Try to extract time range (e.g. "12pm-3pm" or "12:00-3:00")
        time_pattern = r'(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*[-–to]+\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)'
        time_match = re.search(time_pattern, lower)

        start_t = None
        end_t = None
        if time_match:
            try:
                start_parsed = dateparser.parse(time_match.group(1), fuzzy=True)
                end_parsed = dateparser.parse(time_match.group(2), fuzzy=True)
                if start_parsed and end_parsed:
                    start_t = start_parsed.time()
                    end_t = end_parsed.time()
            except (ValueError, OverflowError):
                pass

        # Extract reason — everything after the date/time info
        reason = line
        for kw in off_keywords:
            reason = re.sub(rf'\b{kw}\b', '', reason, flags=re.IGNORECASE)
        for day in day_names + day_abbrs:
            reason = re.sub(rf'\b{day}\b', '', reason, flags=re.IGNORECASE)
        if time_match:
            reason = reason.replace(time_match.group(0), '')
        reason = re.sub(r'\s+', ' ', reason).strip(' -–,.')

        requests.append((target_date, start_t, end_t, reason))

    return requests if requests else None


@csrf_exempt
@require_POST
def sms_webhook(request):
    """Twilio webhook — receives incoming SMS, parses fields, geocodes address, saves as Lead.
    If sender is a rep, parse as time off request instead."""
    body = request.POST.get('Body', '').strip()
    from_number = request.POST.get('From', '')

    # Check if sender is a manager replying APPROVE/DENY
    if from_number and body:
        upper = body.strip().upper()
        approve_with_id = re.match(r'^APPROVE\s+(\d+)$', upper)
        deny_with_id = re.match(r'^DENY\s+(\d+)$', upper)
        approve_plain = upper == 'APPROVE'
        deny_plain = upper == 'DENY'

        if approve_with_id or deny_with_id or approve_plain or deny_plain:
            is_manager = Manager.objects.filter(phone_number__icontains=from_number[-10:]).exists()
            if is_manager:
                is_approve = approve_with_id or approve_plain
                tor = None

                if approve_with_id or deny_with_id:
                    tor_id = int((approve_with_id or deny_with_id).group(1))
                    tor = TimeOffRequest.objects.select_related('rep').filter(pk=tor_id).first()
                    if not tor:
                        send_sms(from_number, f'Time off request #{tor_id} not found.')
                else:
                    # Plain APPROVE/DENY — find pending requests
                    pending = list(TimeOffRequest.objects.filter(status='pending').select_related('rep').order_by('-created_at'))
                    if len(pending) == 1:
                        tor = pending[0]
                    elif len(pending) > 1:
                        lines = [f'Multiple pending requests. Reply with the ID:']
                        for p in pending:
                            time_str = 'All Day' if not p.start_time else f'{p.start_time:%I:%M %p}-{p.end_time:%I:%M %p}'
                            lines.append(f'  #{p.id} — {p.rep.name} {p.date:%m/%d/%Y} {time_str}')
                        send_sms(from_number, '\n'.join(lines))
                    else:
                        send_sms(from_number, 'No pending time off requests.')

                if tor:
                    new_status = 'approved' if is_approve else 'denied'
                    tor.status = new_status
                    tor.save(update_fields=['status'])
                    send_sms(from_number, f'{tor.rep.name} time off {tor.date:%m/%d/%Y} has been {new_status}.')
                    if tor.rep.phone_number:
                        send_sms(tor.rep.phone_number, f'Your time off request for {tor.date:%m/%d/%Y} has been {new_status}.')

                return HttpResponse(
                    '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type='text/xml',
                )

    # Check if sender is a rep — if so, treat as time off request
    rep = Rep.objects.filter(phone_number__icontains=from_number[-10:]).first() if from_number else None
    if rep and body:
        time_off = parse_time_off_request(body, rep)
        if time_off:
            for req_date, start_t, end_t, reason in time_off:
                tor = TimeOffRequest.objects.create(
                    rep=rep,
                    date=req_date,
                    start_time=start_t,
                    end_time=end_t,
                    reason=reason,
                    raw_message=body,
                )
                notify_managers_time_off(tor)
            return HttpResponse(
                '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                content_type='text/xml',
            )

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
                state=fields.get('state', ''),
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
