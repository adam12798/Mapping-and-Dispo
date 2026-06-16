import json
import re
import urllib.parse
import urllib.request

from datetime import datetime

from dateutil import parser as dateparser

from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from django.conf import settings
from django.db.models import Q

from .assignment import auto_assign_leads
from .models import Lead, Rep, TimeOffRequest, Manager, UserProfile, LeadUpdate, LeadMessage, VoiceCallLog, RepCountDefault, RepCountOverride, GHLWebhookLog, APITenant


GHL_WEBHOOK_URL = 'https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/92de7dff-cf7a-4727-92f7-b88e26c515cd'
GHL_APPT_WEBHOOK_URL = 'https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/bc69b54d-d701-432f-82be-80d8dcfa799b'


def _format_dispo_for_ghl(dispo):
    """Format disposition for GHL: no_coverage -> No_Coverage, sale -> Sale, etc."""
    if not dispo:
        return ''
    return '_'.join(word.capitalize() for word in dispo.split('_'))


def _format_appt_dt_for_ghl(dt):
    if not dt:
        return ''
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as dt_cls
        eastern = ZoneInfo('America/New_York')
        if isinstance(dt, str):
            dt = dt_cls.fromisoformat(dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=eastern)
        local_dt = dt.astimezone(eastern)
        return local_dt.strftime('%m-%d-%Y %I:%M %p')
    except Exception:
        return str(dt)


import logging as _logging
import time as _time

_ghl_logger = _logging.getLogger('ghl_webhook')
_ghl_appt_sent = {}


def _send_ghl_dispo_webhook(lead, source=''):
    payload = {
        'phone': lead.phone_number,
        'name': lead.homeowner_name,
        'disposition': _format_dispo_for_ghl(lead.disposition),
        'call_transcript': lead.call_transcript or '',
    }
    log_entry = GHLWebhookLog(
        webhook_type='disposition',
        lead=lead,
        lead_name=lead.homeowner_name,
        source=source,
        url=GHL_WEBHOOK_URL,
        payload=json.dumps(payload),
    )
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            GHL_WEBHOOK_URL,
            data=data,
            headers={'Content-Type': 'application/json'},
        )
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode('utf-8', errors='replace')
        log_entry.response_status = resp.status
        log_entry.response_body = body[:2000]
        log_entry.success = 200 <= resp.status < 300
        _ghl_logger.info(f'GHL dispo webhook sent for lead {lead.id}: status {resp.status}')
    except Exception as e:
        log_entry.error_message = str(e)
        _ghl_logger.error(f'GHL dispo webhook failed for lead {lead.id}: {e}')
    log_entry.save()
    return log_entry


def _send_ghl_appt_webhook(lead, lead_id=None):
    lid = lead_id or lead.id
    now = _time.time()
    if _ghl_appt_sent.get(lid, 0) > now - 30:
        _ghl_logger.info(f'GHL appt webhook skipped for lead {lid}: sent within last 30s')
        return
    _ghl_appt_sent[lid] = now
    params = urllib.parse.urlencode({
        'phone': lead.phone_number,
        'appointment_type': 'Tommy' if lead.source and lead.source.strip().lower() == "tommy's team" else (lead.appointment_type or ''),
        'appointment_datetime': _format_appt_dt_for_ghl(lead.appointment_datetime),
    })
    url = GHL_APPT_WEBHOOK_URL + '?' + params
    payload_str = json.dumps({'phone': lead.phone_number, 'appointment_type': lead.appointment_type, 'appointment_datetime': str(lead.appointment_datetime)})
    log_entry = GHLWebhookLog(
        webhook_type='appointment',
        lead=lead,
        lead_name=lead.homeowner_name,
        source='crm',
        url=url,
        payload=payload_str,
    )
    try:
        ghl_req = urllib.request.Request(url, method='GET')
        resp = urllib.request.urlopen(ghl_req, timeout=10)
        body = resp.read().decode('utf-8', errors='replace')
        log_entry.response_status = resp.status
        log_entry.response_body = body[:2000]
        log_entry.success = 200 <= resp.status < 300
        _ghl_logger.info(f'GHL appt webhook sent for lead {lid}: status {resp.status}')
    except Exception as e:
        log_entry.error_message = str(e)
        _ghl_logger.error(f'GHL appt webhook failed for lead {lid}: {e}')
    log_entry.save()
    return log_entry


def manager_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        profile = getattr(request.user, 'profile', None)
        if not profile or not profile.is_manager:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


def provider_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f'/login/?next={request.path}')
        profile = getattr(request.user, 'profile', None)
        if not profile or not profile.is_provider:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        return view_func(request, *args, **kwargs)
    return wrapper


def get_user_rep(user):
    profile = getattr(user, 'profile', None)
    if profile and profile.role == 'rep' and profile.rep:
        return profile.rep
    return None


@manager_required
def twilio_check(request):
    """Quick check if Twilio env vars are loaded (no secrets exposed)."""
    return JsonResponse({
        'sid_set': bool(settings.TWILIO_ACCOUNT_SID),
        'token_set': bool(settings.TWILIO_AUTH_TOKEN),
        'phone_set': bool(settings.TWILIO_PHONE_NUMBER),
    })


def login_view(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, 'profile', None)
        if profile and profile.is_provider:
            return redirect('/provider/')
        return redirect('/')
    error = ''
    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user is not None and user.is_active:
            login(request, user)
            next_url = request.GET.get('next')
            if not next_url:
                profile = getattr(user, 'profile', None)
                next_url = '/provider/' if profile and profile.is_provider else '/'
            return redirect(next_url)
        else:
            error = 'Invalid username or password'
    return render(request, 'maps/login.html', {'error': error})


def logout_view(request):
    logout(request)
    return redirect('/login/')


@login_required
def index(request):
    return render(request, 'maps/index.html', {'active_tab': 'map'})


def privacy_view(request):
    return render(request, 'maps/privacy.html')


def terms_view(request):
    return render(request, 'maps/terms.html')


def sms_consent_view(request):
    return render(request, 'maps/sms_consent.html')


@login_required
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
    user_rep = get_user_rep(request.user)
    if user_rep:
        leads = leads.filter(rep=user_rep)
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
            'cancelled': lead.cancelled,
        }
        for lead in leads
    ]
    return JsonResponse(data, safe=False)


def _normalize_phone(number):
    """Ensure phone number is in E.164 format (+1XXXXXXXXXX)."""
    digits = re.sub(r'\D', '', number)
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    if number.startswith('+'):
        return number
    return f'+{digits}'


def send_sms(to, body):
    """Send an SMS via Twilio REST API."""
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return
    to = _normalize_phone(to)
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
    if tor.end_date and tor.end_date != tor.start_date:
        date_str = f'{tor.start_date:%m/%d/%Y} to {tor.end_date:%m/%d/%Y}'
    elif not tor.end_date:
        date_str = f'{tor.start_date:%m/%d/%Y} onwards (indefinite)'
    else:
        date_str = f'{tor.start_date:%m/%d/%Y}'
    body = (
        f'Time Off Request #{tor.id}\n'
        f'{tor.rep.name} requests {date_str} {time_str}{reason_str}\n\n'
        f'Reply "APPROVE {tor.id}" or "DENY {tor.id}"'
    )
    for manager in Manager.objects.all():
        send_sms(manager.phone_number, body)


def is_in_massachusetts(lat, lng):
    """Check if coordinates fall within Massachusetts bounding box."""
    return 41.0 <= lat <= 43.0 and -73.6 <= lng <= -69.8


def geocode(address):
    """Geocode an address using Nominatim (free, no API key).

    Validates results are in Massachusetts. Uses multiple strategies:
    1. Clean query with Massachusetts (strip MA abbreviation first)
    2. Free-text with original address
    3. City-only fallback (returns city center — better than nothing)
    """
    import time as _time
    import logging
    geo_logger = logging.getLogger('geocode')

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

    def _extract_city(addr):
        """Pull city name from an address string."""
        parts = [p.strip() for p in addr.split(',')]
        # Strip state/zip parts from the end
        clean = []
        for p in parts:
            if re.match(r'^(MA|Massachusetts|US|USA|\d{5})$', p, re.IGNORECASE):
                continue
            clean.append(p)
        # City is typically the second-to-last meaningful part, or last if only 2 parts
        if len(clean) >= 2:
            return clean[-1]
        return None

    # Strip "MA" / "Massachusetts" from the address so we can append cleanly
    clean_addr = re.sub(r',?\s*(MA|Massachusetts|Mass)\b\.?\s*$', '', address, flags=re.IGNORECASE).strip()
    clean_addr = clean_addr.rstrip(',').strip()

    # Strategy 1: clean address + Massachusetts
    ma_query = f'{clean_addr}, Massachusetts'
    lat, lng = _nominatim_search(ma_query)
    if lat is not None and is_in_massachusetts(lat, lng):
        return lat, lng

    _time.sleep(0.3)

    # Strategy 2: original address as-is
    lat, lng = _nominatim_search(address)
    if lat is not None and is_in_massachusetts(lat, lng):
        return lat, lng

    _time.sleep(0.3)

    # Strategy 3: city-only fallback (city center coordinates)
    city = _extract_city(address) or _extract_city(clean_addr)
    if city:
        city_lat, city_lng = _nominatim_search(f'{city}, Massachusetts')
        if city_lat is not None and is_in_massachusetts(city_lat, city_lng):
            geo_logger.info(f'Geocode city fallback for "{address}" -> {city}, MA ({city_lat}, {city_lng})')
            return city_lat, city_lng

    geo_logger.warning(f'Geocode failed for "{address}"')
    return None, None


@login_required
def crm_view(request):
    leads = Lead.objects.select_related('rep').order_by('-created_at')
    user_rep = get_user_rep(request.user)
    if user_rep:
        leads = leads.filter(rep=user_rep)
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/crm.html', {'leads': leads, 'reps': reps, 'active_tab': 'crm'})


@login_required
def daily_view(request):
    from datetime import date as dt_date
    selected_date = request.GET.get('date', dt_date.today().isoformat())
    leads = Lead.objects.select_related('rep').filter(
        appointment_datetime__date=selected_date
    ).order_by('appointment_datetime')
    user_rep = get_user_rep(request.user)
    if user_rep:
        leads = leads.filter(rep=user_rep)
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/daily.html', {
        'leads': leads,
        'reps': reps,
        'selected_date': selected_date,
        'active_tab': 'daily',
    })


@csrf_exempt
@login_required
def lead_update(request, pk):
    """Update or delete a lead's CRM fields."""
    is_mgr = getattr(request.user, 'profile', None) and request.user.profile.is_manager
    REP_EDITABLE_FIELDS = {'disposition', 'sat', 'call_notes', 'appt_notes', 'follow_up_date'}
    if not is_mgr:
        user_rep = get_user_rep(request.user)
        if not user_rep:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        lead = get_object_or_404(Lead, pk=pk)
        if lead.rep != user_rep:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        if request.method == 'DELETE':
            return JsonResponse({'error': 'Forbidden'}, status=403)
        data = json.loads(request.body)
        non_allowed = set(data.keys()) - REP_EDITABLE_FIELDS
        if non_allowed:
            return JsonResponse({'error': 'Forbidden'}, status=403)
    if request.method == 'DELETE':
        lead = get_object_or_404(Lead, pk=pk)
        lead.delete()
        return JsonResponse({'status': 'ok'})
    if request.method != 'PUT':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    lead = get_object_or_404(Lead, pk=pk)
    old_appt_dt = str(lead.appointment_datetime) if lead.appointment_datetime else ''
    data = json.loads(request.body)
    allowed_fields = [
        'homeowner_name', 'phone_number', 'address', 'city', 'state',
        'source', 'tags', 'appointment_type', 'appointment_format', 'appointment_datetime',
        'disposition', 'sat', 'follow_up_date', 'call_notes', 'appt_notes', 'call_transcript',
    ]
    FIELD_LABELS = {
        'homeowner_name': 'Name', 'phone_number': 'Phone', 'address': 'Address',
        'city': 'City', 'state': 'State', 'source': 'Source', 'tags': 'Tags',
        'appointment_type': 'Appt Type',
        'appointment_format': 'Appt Format', 'appointment_datetime': 'Appt Time',
        'disposition': 'Disposition', 'sat': 'Sit', 'follow_up_date': 'Follow Up Date',
        'call_notes': 'Call Notes', 'appt_notes': 'Appt Notes', 'call_transcript': 'Transcript', 'rep_id': 'Rep',
    }
    VALUE_LABELS = {
        'appointment_type': {'solar': 'Solar', 'hvac': 'HVAC', 'both': 'Both'},
        'appointment_format': {'in_person': 'In Person', 'virtual': 'Virtual'},
        'sat': {'true': 'Sit', 'false': 'No Sit'},
    }
    VALUE_LABELS['disposition'] = dict(Lead.DISPOSITION_CHOICES)
    changes = []
    if 'rep_id' in data:
        rep_val = data['rep_id']
        old_rep = lead.rep
        lead.rep_id = int(rep_val) if rep_val else None
        new_rep_name = Rep.objects.filter(id=lead.rep_id).values_list('name', flat=True).first() if lead.rep_id else None
        old_name = old_rep.name if old_rep else 'Unassigned'
        new_name = new_rep_name or 'Unassigned'
        if old_name != new_name:
            changes.append(f"Rep: {old_name} → {new_name}")
    for field in allowed_fields:
        if field in data:
            old_value = getattr(lead, field)
            value = data[field]
            if field in ('appointment_datetime', 'follow_up_date') and value == '':
                value = None
            if field == 'sat':
                value = {'true': True, 'false': False, 'yes': True, 'no': False}.get(str(value).lower().strip()) if value != '' else None
            setattr(lead, field, value)
            old_display = str(old_value) if old_value not in (None, '') else '—'
            new_display = str(value) if value not in (None, '') else '—'
            if field in VALUE_LABELS:
                old_display = VALUE_LABELS[field].get(str(old_value).lower() if old_value is not None else '', old_display)
                new_display = VALUE_LABELS[field].get(str(value).lower() if value is not None else '', new_display)
            if old_display != new_display:
                label = FIELD_LABELS.get(field, field)
                changes.append(f"{label}: {old_display} → {new_display}")
    # Auto-compute appointment_type from tags when tags change
    if 'tags' in data and 'appointment_type' not in data:
        new_type = compute_appointment_type(lead.tags)
        old_type = lead.appointment_type or ''
        if new_type != old_type:
            old_display = VALUE_LABELS['appointment_type'].get(old_type, old_type or '—')
            new_display = VALUE_LABELS['appointment_type'].get(new_type, new_type or '—')
            lead.appointment_type = new_type
            changes.append(f"Appt Type: {old_display} → {new_display} (auto)")

    # Re-geocode if address or city changed
    geocode_failed = False
    if 'address' in data or 'city' in data:
        geocode_address = lead.address
        if lead.city:
            geocode_address = f"{lead.address}, {lead.city}, MA"
        lead.latitude, lead.longitude = geocode(geocode_address) if lead.address else (None, None)
        if lead.address and lead.latitude is None:
            geocode_failed = True
    lead.save()
    if changes:
        LeadUpdate.objects.create(lead=lead, user=request.user, text='\n'.join(changes))

    if 'disposition' in data:
        _send_ghl_dispo_webhook(lead, source='crm')

    # Send webhook to Go High Level only if appointment datetime actually changed
    new_appt_dt = str(lead.appointment_datetime) if lead.appointment_datetime else ''
    if 'appointment_datetime' in data and new_appt_dt != old_appt_dt:
        _send_ghl_appt_webhook(lead, pk)

    response = {'status': 'ok'}
    if geocode_failed:
        response['geocode_failed'] = True
    if 'tags' in data and 'appointment_type' not in data:
        response['appointment_type'] = lead.appointment_type or ''
    return JsonResponse(response)


@csrf_exempt
@manager_required
def leads_bulk_delete(request):
    """Delete multiple leads by ID."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    ids = data.get('ids', [])
    Lead.objects.filter(id__in=ids).delete()
    return JsonResponse({'status': 'ok'})


@csrf_exempt
@manager_required
def leads_bulk_update(request):
    """Update multiple leads' fields at once."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    ids = data.get('ids', [])
    fields = data.get('fields', {})
    if not ids or not fields:
        return JsonResponse({'error': 'ids and fields required'}, status=400)

    BULK_ALLOWED = {'rep_id', 'disposition', 'sat', 'appointment_type', 'appointment_format', 'follow_up_date'}
    update_kwargs = {}
    for key, value in fields.items():
        if key not in BULK_ALLOWED:
            continue
        if key == 'rep_id':
            update_kwargs['rep_id'] = int(value) if value else None
        elif key == 'sat':
            update_kwargs['sat'] = {'true': True, 'false': False, 'yes': True, 'no': False}.get(str(value).lower().strip()) if value != '' else None
        elif key == 'follow_up_date':
            update_kwargs['follow_up_date'] = value if value else None
        else:
            update_kwargs[key] = value

    if not update_kwargs:
        return JsonResponse({'error': 'No valid fields to update'}, status=400)

    BULK_LABELS = {
        'rep_id': 'Rep', 'disposition': 'Disposition', 'sat': 'Sit',
        'appointment_type': 'Appt Type', 'appointment_format': 'Appt Format',
        'follow_up_date': 'Follow Up Date',
    }
    BULK_VALUE_LABELS = {
        'appointment_type': {'solar': 'Solar', 'hvac': 'HVAC', 'both': 'Both'},
        'appointment_format': {'in_person': 'In Person', 'virtual': 'Virtual'},
        'sat': {True: 'Sit', False: 'No Sit'},
        'disposition': dict(Lead.DISPOSITION_CHOICES),
    }
    change_parts = []
    for key, val in update_kwargs.items():
        label = BULK_LABELS.get(key, key)
        if key == 'rep_id':
            display = Rep.objects.filter(id=val).values_list('name', flat=True).first() if val else 'Unassigned'
        elif key in BULK_VALUE_LABELS:
            display = BULK_VALUE_LABELS[key].get(val, str(val) if val not in (None, '') else '—')
        else:
            display = str(val) if val not in (None, '') else '—'
        change_parts.append(f"{label} → {display}")
    change_text = 'Bulk update: ' + ', '.join(change_parts)
    bulk_updates = [LeadUpdate(lead_id=lid, user=request.user, text=change_text) for lid in ids]
    LeadUpdate.objects.bulk_create(bulk_updates)

    Lead.objects.filter(id__in=ids).update(**update_kwargs)

    if 'disposition' in update_kwargs:
        for lead in Lead.objects.filter(id__in=ids):
            _send_ghl_dispo_webhook(lead, source='bulk')

    return JsonResponse({'status': 'ok', 'updated': len(ids)})


@csrf_exempt
@manager_required
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


@manager_required
def time_off_view(request):
    requests = TimeOffRequest.objects.select_related('rep').order_by('-created_at')
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/time_off.html', {'requests': requests, 'reps': reps, 'active_tab': 'time_off'})


@login_required
def time_off_by_date_api(request):
    """Return approved time off for a given date."""
    date_str = request.GET.get('date', '')
    if not date_str:
        return JsonResponse({'error': 'date required'}, status=400)
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date'}, status=400)

    reqs = TimeOffRequest.objects.filter(
        start_date__lte=target_date,
        status='approved',
    ).filter(
        Q(end_date__gte=target_date) | Q(end_date__isnull=True)
    ).select_related('rep')
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
@manager_required
def time_off_api(request):
    """Create a time off request manually."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    rep = get_object_or_404(Rep, pk=data['rep_id'])
    start_time = data.get('start_time') or None
    end_time = data.get('end_time') or None
    end_date = data.get('end_date') or data['date']
    TimeOffRequest.objects.create(
        rep=rep,
        start_date=data['date'],
        end_date=end_date if end_date != 'indefinite' else None,
        start_time=start_time,
        end_time=end_time,
        reason=data.get('reason', ''),
    )
    return JsonResponse({'status': 'ok'})


@csrf_exempt
@manager_required
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
        tor.start_date = data['date']
    if 'start_date' in data:
        tor.start_date = data['start_date']
    if 'end_date' in data:
        v = data['end_date']
        tor.end_date = None if v == 'indefinite' or v == '' else v
    if 'start_time' in data:
        tor.start_time = data['start_time'] or None
    if 'end_time' in data:
        tor.end_time = data['end_time'] or None
    tor.save()
    return JsonResponse({'status': 'ok'})


@manager_required
def reps_view(request):
    active_reps = Rep.objects.filter(is_active=True).order_by('-rating', 'name')
    inactive_reps = Rep.objects.filter(is_active=False).order_by('-rating', 'name')
    return render(request, 'maps/reps.html', {
        'active_reps': active_reps,
        'inactive_reps': inactive_reps,
        'active_tab': 'reps',
    })


@csrf_exempt
@require_POST
@manager_required
def rep_create(request):
    """Create a new rep, geocoding their home address."""
    data = json.loads(request.body)
    home_address = data.get('home_address', '')
    city = data.get('city', '')
    geocode_address = home_address
    if city:
        geocode_address = f"{home_address}, {city}, MA"
    lat, lng = geocode(geocode_address) if home_address else (None, None)
    geocode_failed = bool(home_address and lat is None)
    REP_PALETTE = [
        '#2980b9', '#e74c3c', '#27ae60', '#8e44ad', '#f39c12',
        '#1abc9c', '#e67e22', '#3498db', '#9b59b6', '#d35400',
        '#16a085', '#c0392b', '#2ecc71', '#2c3e50', '#f1c40f',
        '#7f8c8d', '#00bcd4', '#e91e63', '#4caf50', '#ff5722',
    ]
    if not data.get('color'):
        used = set(Rep.objects.values_list('color', flat=True))
        color = next((c for c in REP_PALETTE if c not in used), '#%06x' % (hash(data.get('name', '')) % 0xFFFFFF))
    else:
        color = data['color']

    sms_consent = data.get('sms_consent', False)
    rep = Rep.objects.create(
        name=data.get('name', ''),
        phone_number=data.get('phone_number', ''),
        home_address=home_address,
        city=city,
        latitude=lat,
        longitude=lng,
        specialty=data.get('specialty', ''),
        color=color,
        textblast_eligible=data.get('textblast_eligible', False),
        sms_consent=sms_consent,
        sms_consent_at=datetime.now() if sms_consent else None,
    )
    if sms_consent and rep.phone_number:
        send_sms(rep.phone_number,
            'Sutton by Iceberg Home Solutions: You\'re now subscribed to appointment updates. '
            'Msg frequency varies. Msg&Data rates may apply. '
            'Reply HELP for help, STOP to cancel.')
    response = {'status': 'ok', 'id': rep.id}
    if geocode_failed:
        response['geocode_failed'] = True
    return JsonResponse(response)


@csrf_exempt
@manager_required
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
    had_consent = rep.sms_consent
    allowed_fields = ['name', 'phone_number', 'home_address', 'city', 'specialty', 'rating', 'color', 'is_active', 'textblast_eligible', 'sms_consent']
    for field in allowed_fields:
        if field in data:
            setattr(rep, field, data[field])
    if 'sms_consent' in data and data['sms_consent'] and not had_consent:
        rep.sms_consent_at = datetime.now()
        if rep.phone_number:
            send_sms(rep.phone_number,
                'Sutton by Iceberg Home Solutions: You\'re now subscribed to appointment updates. '
                'Msg frequency varies. Msg&Data rates may apply. '
                'Reply HELP for help, STOP to cancel.')
    # Re-geocode if address or city changed
    geocode_failed = False
    if 'home_address' in data or 'city' in data:
        geocode_address = rep.home_address
        if rep.city:
            geocode_address = f"{rep.home_address}, {rep.city}, MA"
        rep.latitude, rep.longitude = geocode(geocode_address) if rep.home_address else (None, None)
        if rep.home_address and rep.latitude is None:
            geocode_failed = True
    rep.save()
    response = {'status': 'ok'}
    if geocode_failed:
        response['geocode_failed'] = True
    return JsonResponse(response)


@csrf_exempt
@manager_required
def reps_bulk_delete(request):
    """Delete multiple reps by ID."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    ids = data.get('ids', [])
    Rep.objects.filter(id__in=ids).delete()
    return JsonResponse({'status': 'ok'})


@login_required
def reps_api(request):
    """Return all active reps as JSON. Ensures TextBlast rep exists for managers."""
    if hasattr(request.user, 'profile') and request.user.profile.is_manager:
        get_textblast_rep()
    reps = Rep.objects.filter(is_active=True).order_by('name')
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


@login_required
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
@manager_required
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
@manager_required
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


def get_textblast_rep():
    """Get or create the special TextBlast rep."""
    rep, _ = Rep.objects.get_or_create(
        name='TextBlast',
        defaults={'is_active': True, 'color': '#ff6b6b'},
    )
    return rep


def send_sms_with_result(to, body, from_number=None):
    """Send SMS via Twilio and return (success, error_detail)."""
    import base64 as b64
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return False, 'Twilio credentials not configured'
    if not to:
        return False, 'No phone number'
    to = _normalize_phone(to)
    url = f'https://api.twilio.com/2010-04-01/Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json'
    data = urllib.parse.urlencode({
        'To': to,
        'From': from_number or settings.TWILIO_PHONE_NUMBER,
        'Body': body,
    }).encode()
    req = urllib.request.Request(url, data=data)
    credentials = f'{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}'
    auth = b64.b64encode(credentials.encode()).decode()
    req.add_header('Authorization', f'Basic {auth}')
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return True, None
    except urllib.error.HTTPError as e:
        error_body = e.read().decode() if e.fp else str(e)
        return False, f'HTTP {e.code}: {error_body[:200]}'
    except Exception as e:
        return False, str(e)


def send_textblast(leads):
    """Send TextBlast SMS to eligible reps. Returns dict with details."""
    from zoneinfo import ZoneInfo

    eastern = ZoneInfo('America/New_York')
    now = datetime.now(eastern)
    eligible_reps = list(Rep.objects.filter(
        textblast_eligible=True, is_active=True,
    ).exclude(name='TextBlast').exclude(phone_number=''))

    if not eligible_reps:
        return {'sent': 0, 'errors': ['No eligible reps with TextBlast enabled and a phone number']}
    if not leads:
        return {'sent': 0, 'errors': ['No leads to blast']}

    # Build numbered list of appointments
    lines = ['Available appointments:']
    for i, lead in enumerate(leads, 1):
        dt = lead.appointment_datetime.astimezone(eastern)
        appt_type = (lead.appointment_type or 'unknown').upper()
        city = lead.city or ''
        address = lead.address or ''
        time_str = dt.strftime('%I:%M %p').lstrip('0')
        lines.append(f'{i}. {address}, {city} - {time_str} - {appt_type}')
    lines.append('')
    lines.append('Reply with the # to claim or describe which one (e.g. "I can take the one in Waltham")')
    message = '\n'.join(lines)

    sent_count = 0
    errors = []
    for rep in eligible_reps:
        ok, err = send_sms_with_result(rep.phone_number, message)
        if ok:
            sent_count += 1
        else:
            errors.append(f'{rep.name} ({rep.phone_number}): {err}')

    # Mark leads as blasted
    lead_ids = [l.id for l in leads]
    Lead.objects.filter(id__in=lead_ids).update(textblast_sent_at=now)
    return {'sent': sent_count, 'errors': errors, 'reps_tried': [f'{r.name}: {r.phone_number}' for r in eligible_reps]}


@csrf_exempt
@require_POST
@manager_required
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


@csrf_exempt
@require_POST
@manager_required
def textblast_send_api(request):
    """Send TextBlast SMS for all un-blasted appointments assigned to TextBlast rep."""
    from zoneinfo import ZoneInfo

    data = json.loads(request.body) if request.body else {}
    date_str = data.get('date', '')

    textblast_rep = Rep.objects.filter(name='TextBlast').first()
    if not textblast_rep:
        return JsonResponse({'error': 'No TextBlast rep found. Assign appointments to TextBlast first.'}, status=400)

    qs = Lead.objects.filter(
        rep=textblast_rep,
        cancelled=False,
        appointment_datetime__isnull=False,
    )
    # If a date is provided, filter to that date only
    if date_str:
        qs = qs.filter(appointment_datetime__date=date_str)

    textblast_leads = list(qs.order_by('appointment_datetime'))
    if not textblast_leads:
        return JsonResponse({'error': 'No TextBlast appointments found for this date.'}, status=400)

    result = send_textblast(textblast_leads)
    response = {
        'status': 'ok',
        'leads_blasted': len(textblast_leads),
        'reps_notified': result['sent'],
        'reps_tried': result.get('reps_tried', []),
    }
    if result.get('errors'):
        response['sms_errors'] = result['errors']
    return JsonResponse(response)


@manager_required
def dashboard_view(request):
    reps = Rep.objects.filter(is_active=True).order_by('name')
    return render(request, 'maps/dashboard.html', {'reps': reps, 'active_tab': 'dashboard'})


@manager_required
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

    DISPO_KEYS = ['sale', 'no_sale', 'follow_up', 'credit_fail', 'cancel_door',
                  'cpfu', 'rep_no_show', 'no_coverage', 'needs_reschedule',
                  'incomplete_deal', 'future_contact']
    DISPO_LABELS = {
        'sale': 'Sale', 'no_sale': 'No Sale', 'follow_up': 'Follow Up',
        'credit_fail': 'Credit Fail', 'cancel_door': 'Cancel at Door',
        'cpfu': 'CPFU', 'rep_no_show': 'Rep No Show',
        'no_coverage': 'No Coverage', 'needs_reschedule': 'Needs Reschedule',
        'incomplete_deal': 'Incomplete Deal', 'future_contact': 'Future Contact',
    }
    DISPO_COLORS = {
        'sale': '#27ae60', 'no_sale': '#8e44ad', 'follow_up': '#e67e22',
        'credit_fail': '#ff69b4', 'cancel_door': '#95a5a6', 'cpfu': '#98c1d9',
        'rep_no_show': '#111111', 'no_coverage': '#c0392b', 'needs_reschedule': '#3498db',
        'incomplete_deal': '#d4a017', 'future_contact': '#1abc9c', 'dq': '#8B4513',
        'no_show': '#800000',
    }
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


def apply_chart_filter(qs, f):
    key = f.get('key', '')
    cond = f.get('cond', '')
    val = f.get('val', '')
    val2 = f.get('val2', '')
    if not key or not cond:
        return qs
    if key in ('rep_id', 'sat'):
        if cond == 'is_empty':
            return qs.filter(**{f'{key}__isnull': True})
        if cond == 'is_not_empty':
            return qs.exclude(**{f'{key}__isnull': True})
        if key == 'rep_id' and val and cond in ('is', 'is_not'):
            try:
                v = int(val)
            except ValueError:
                return qs
            return qs.filter(rep_id=v) if cond == 'is' else qs.exclude(rep_id=v)
        if key == 'sat' and val and cond in ('is', 'is_not'):
            b = val == 'true'
            return qs.filter(sat=b) if cond == 'is' else qs.exclude(sat=b)
        return qs
    if key in ('appointment_datetime', 'follow_up_date'):
        if cond == 'is':
            return qs.filter(**{f'{key}__date': val})
        if cond == 'before':
            return qs.filter(**{f'{key}__lt': val})
        if cond == 'after':
            return qs.filter(**{f'{key}__gt': val})
        if cond == 'between' and val2:
            return qs.filter(**{f'{key}__gte': val, f'{key}__lte': val2})
        if cond == 'is_empty':
            return qs.filter(**{f'{key}__isnull': True})
        if cond == 'is_not_empty':
            return qs.exclude(**{f'{key}__isnull': True})
        return qs
    if cond == 'is':
        return qs.filter(**{f'{key}__iexact': val})
    if cond == 'is_not':
        return qs.exclude(**{f'{key}__iexact': val})
    if cond == 'contains':
        return qs.filter(**{f'{key}__icontains': val})
    if cond == 'not_contains':
        return qs.exclude(**{f'{key}__icontains': val})
    if cond == 'is_empty':
        return qs.filter(Q(**{key: ''}) | Q(**{f'{key}__isnull': True}))
    if cond == 'is_not_empty':
        return qs.exclude(Q(**{key: ''}) | Q(**{f'{key}__isnull': True}))
    return qs


@manager_required
def dashboard_chart_api(request):
    from django.db.models import Count, Q

    group_by = request.GET.get('group_by', 'disposition')
    start = request.GET.get('start', '')
    end = request.GET.get('end', '')
    metric = request.GET.get('metric', 'count')
    filters_raw = request.GET.get('filters', '[]')

    qs = Lead.objects.select_related('rep')
    if start:
        qs = qs.filter(appointment_datetime__date__gte=start)
    if end:
        qs = qs.filter(appointment_datetime__date__lte=end)
    try:
        for f in json.loads(filters_raw):
            qs = apply_chart_filter(qs, f)
    except (json.JSONDecodeError, TypeError):
        pass

    LABEL_MAPS = {
        'disposition': DISPO_NAMES,
        'appointment_type': {'solar': 'Solar', 'hvac': 'HVAC', 'both': 'Both'},
        'appointment_format': {'in_person': 'In Person', 'virtual': 'Virtual'},
        'sat': {'True': 'Sit', 'False': 'No Sit'},
    }
    COLOR_MAPS = {
        'disposition': {
            'sale': '#27ae60', 'no_sale': '#8e44ad', 'follow_up': '#e67e22',
            'credit_fail': '#e91e63', 'cancel_door': '#95a5a6', 'cpfu': '#00bcd4',
            'rep_no_show': '#2c3e50', 'no_coverage': '#c0392b', 'needs_reschedule': '#3498db',
            'incomplete_deal': '#d4a017', 'future_contact': '#1abc9c', 'dq': '#8B4513', 'no_show': '#800000',
        },
        'appointment_type': {'solar': '#f1c40f', 'hvac': '#e74c3c', 'both': '#27ae60'},
        'sat': {'True': '#2ecc40', 'False': '#cc0000'},
    }
    PALETTE = ['#3498db', '#e74c3c', '#2ecc71', '#f39c12', '#9b59b6', '#1abc9c', '#e67e22',
               '#34495e', '#d35400', '#16a085', '#c0392b', '#8e44ad', '#27ae60', '#2980b9', '#f1c40f']

    if group_by == 'rep_id':
        if metric == 'conversion_rate':
            rows = list(qs.filter(rep__isnull=False).values('rep__name', 'rep__color').annotate(
                total=Count('id'), sales=Count('id', filter=Q(disposition='sale'))).order_by('rep__name'))
            labels = [r['rep__name'] for r in rows]
            values = [round(r['sales'] / r['total'] * 100, 1) if r['total'] else 0 for r in rows]
            colors = [r['rep__color'] or '#98c1d9' for r in rows]
        else:
            rows = list(qs.filter(rep__isnull=False).values('rep__name', 'rep__color').annotate(
                count=Count('id')).order_by('-count'))
            labels = [r['rep__name'] for r in rows]
            values = [r['count'] for r in rows]
            colors = [r['rep__color'] or '#98c1d9' for r in rows]
    else:
        rows = list(qs.values(group_by).annotate(count=Count('id')).order_by('-count'))
        label_map = LABEL_MAPS.get(group_by, {})
        color_map = COLOR_MAPS.get(group_by, {})
        labels, values, raw_keys = [], [], []
        for r in rows:
            raw = r[group_by]
            raw_str = str(raw) if raw not in (None, '') else ''
            labels.append(label_map.get(raw_str, raw_str) if raw_str else '(empty)')
            raw_keys.append(raw_str)
            values.append(r['count'])
        colors = [color_map.get(k, PALETTE[i % len(PALETTE)]) for i, k in enumerate(raw_keys)] if color_map else [PALETTE[i % len(PALETTE)] for i in range(len(labels))]

    total = sum(values)
    sales_count = qs.filter(disposition='sale').count()
    return JsonResponse({
        'labels': labels, 'values': values, 'colors': colors,
        'total': total, 'sales': sales_count,
        'conversion_rate': round(sales_count / total * 100, 1) if total else 0,
    })


def parse_sms_fields(body):
    """Parse structured SMS line by line, matching 'label: value' patterns.
    Notes field is special — everything after 'Notes:' is captured as multi-line."""
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
        'source': 'source',
        'notes': 'notes',
    }

    fields = {}
    lines = body.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if ':' not in line:
            continue
        label_part, _, value_part = line.partition(':')
        label = label_part.strip().lower()
        value = value_part.strip()
        if label not in label_map:
            continue
        field_key = label_map[label]
        if field_key == 'notes':
            note_lines = []
            if value:
                note_lines.append(value)
            while i < len(lines):
                next_line = lines[i].strip()
                if ':' in next_line:
                    next_label = next_line.partition(':')[0].strip().lower()
                    if next_label in label_map:
                        break
                if next_line:
                    note_lines.append(next_line)
                i += 1
            if note_lines:
                fields['notes'] = ' '.join(note_lines)
        elif value:
            fields[field_key] = value
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


def compute_appointment_type(tags_str):
    if not tags_str:
        return ''
    tags = [t.strip() for t in tags_str.split(',')]
    has_solar = any(t in ('Solar', 'Roof', 'Battery') for t in tags)
    has_hvac = any(t == 'Hvac' for t in tags)
    has_masssave = any(t == 'MassSave' for t in tags)
    if has_masssave or (has_solar and has_hvac):
        return 'both'
    if has_solar:
        return 'solar'
    if has_hvac:
        return 'hvac'
    return ''


DISPO_NAMES = {
    'sale': 'Sale', 'no_sale': 'No Sale', 'follow_up': 'Follow Up',
    'credit_fail': 'Credit Fail', 'cancel_door': 'Cancel at Door',
    'cpfu': 'CPFU', 'rep_no_show': 'Rep No Show',
    'no_coverage': 'No Coverage', 'needs_reschedule': 'Needs Reschedule',
    'incomplete_deal': 'Incomplete Deal', 'future_contact': 'Future Contact',
    'dq': 'DQ', 'no_show': 'No Show',
}


def parse_manager_update_sms(body):
    """Use GPT-4o-mini to parse a manager's SMS into a CRM update request."""
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY)
    import zoneinfo
    eastern = zoneinfo.ZoneInfo('America/New_York')
    today_str = datetime.now(tz=eastern).strftime('%A, %Y-%m-%d')

    try:
        resp = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': f"""You parse SMS messages from a sales manager about updating solar/HVAC appointments. Today is {today_str}.

Managers text casually — expect typos, abbreviations, incomplete sentences, slang, and varied formats. Be flexible and use best judgment.

Extract these fields:
- "action": "reschedule", "cancel", "disposition", "assign", "notes", or "unknown"
- "lead_name": homeowner's last name or full name mentioned (empty string if not found)
- "lead_phone": phone number if mentioned (empty string if not found)
- "lead_city": city/town mentioned for identifying the appt (empty string if not found)
- "current_datetime": existing appt date/time if referenced for identification, YYYY-MM-DDTHH:MM format. Convert relative dates. Empty string if not mentioned.
- "new_datetime": new date/time if rescheduling, YYYY-MM-DDTHH:MM format. Convert "Friday 2pm", "tmrw at 10", "next tues 3", "this sat 1pm", "wed", etc. to actual dates. If only a day is given with no time, use 10:00 as default. Empty string if not rescheduling.
- "new_disposition": one of: sale, no_sale, follow_up, credit_fail, cancel_door, cpfu, rep_no_show, no_coverage, needs_reschedule, incomplete_deal, future_contact, dq, no_show. Empty string if not applicable.
- "rep_name": rep name if assigning/reassigning (empty string if not applicable)
- "notes": any extra context or notes. Empty string if none.

Common manager text patterns:
- "push smith to fri" → reschedule, lead_name: "Smith", new_datetime: Friday 10:00
- "move the medford appt to 3pm tmrw" → reschedule, lead_city: "Medford", new_datetime: tomorrow 15:00
- "bump jones 2 thursday 11a" → reschedule, lead_name: "Jones", new_datetime: Thursday 11:00
- "cancel garcia" → cancel, lead_name: "Garcia"
- "scratch the 4pm springfield" → cancel, lead_city: "Springfield", current_datetime: today 16:00
- "johnson was a sale" → disposition, lead_name: "Johnson", new_disposition: "sale"
- "no show williams" → disposition, lead_name: "Williams", new_disposition: "no_show"
- "smith didnt sit" → disposition, lead_name: "Smith", new_disposition: "cancel_door"
- "fu on martinez" → disposition, lead_name: "Martinez", new_disposition: "follow_up"
- "need to follow up w chen" → disposition, lead_name: "Chen", new_disposition: "follow_up"
- "give the davis lead to mike" → assign, lead_name: "Davis", rep_name: "Mike"

Try hard to identify an action. Only return "unknown" if the text truly has nothing to do with appointments.

Return ONLY valid JSON, no other text or markdown."""},
                {'role': 'user', 'content': body},
            ],
            max_tokens=250,
        )
        raw = resp.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```\w*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
        return json.loads(raw)
    except Exception:
        return None


def find_leads_for_update(parsed):
    """Find leads matching the identifier from a parsed manager SMS."""
    from datetime import timedelta
    from django.utils import timezone as tz

    name = (parsed.get('lead_name') or '').strip()
    phone = (parsed.get('lead_phone') or '').strip()
    city = (parsed.get('lead_city') or '').strip()
    current_dt = (parsed.get('current_datetime') or '').strip()

    now = tz.now()
    # Search upcoming appointments (today through next 7 days)
    leads_qs = Lead.objects.select_related('rep').filter(
        appointment_datetime__gte=now.replace(hour=0, minute=0, second=0),
        appointment_datetime__lte=now + timedelta(days=7),
    ).order_by('appointment_datetime')

    # Build filters progressively — name + city is the strongest signal
    if name and city:
        matches = list(leads_qs.filter(homeowner_name__icontains=name, city__icontains=city)[:5])
        if matches:
            return matches

    if name:
        exact = list(leads_qs.filter(homeowner_name__iexact=name)[:5])
        if exact:
            return exact
        partial = list(leads_qs.filter(homeowner_name__icontains=name)[:5])
        if partial:
            return partial

    # City + time combo (e.g. "the 2pm in Springfield")
    if city and current_dt:
        try:
            target_dt = dateparser.parse(current_dt)
            if target_dt:
                matches = list(leads_qs.filter(
                    city__icontains=city,
                    appointment_datetime__date=target_dt.date(),
                    appointment_datetime__hour=target_dt.hour,
                )[:5])
                if matches:
                    return matches
        except (ValueError, OverflowError):
            pass

    # City only — return all upcoming in that city
    if city:
        matches = list(leads_qs.filter(city__icontains=city)[:5])
        if matches:
            return matches

    if phone:
        clean = phone.replace('-', '').replace(' ', '').replace('(', '').replace(')', '').replace('+1', '')
        if len(clean) >= 7:
            return list(leads_qs.filter(phone_number__icontains=clean[-7:])[:5])

    return []


def apply_manager_sms_update(lead, parsed):
    """Apply a parsed manager SMS update to a lead. Returns list of change descriptions."""
    action = parsed.get('action', '')
    changes = []

    if action == 'reschedule':
        if parsed.get('new_datetime'):
            try:
                import zoneinfo
                eastern = zoneinfo.ZoneInfo('America/New_York')
                new_dt = dateparser.parse(parsed['new_datetime'])
                if new_dt:
                    if new_dt.tzinfo is None:
                        new_dt = new_dt.replace(tzinfo=eastern)
                    lead.appointment_datetime = new_dt
                    if lead.cancelled:
                        lead.cancelled = False
                        changes.append('Cancelled → Rescheduled')
                    changes.append(f"Rescheduled to {new_dt.astimezone(eastern).strftime('%m/%d/%Y at %I:%M %p')}")
            except (ValueError, OverflowError):
                pass
        else:
            lead.appointment_datetime = None
            lead.disposition = 'needs_reschedule'
            changes.append('Appointment date cleared — Needs Reschedule')

    if action == 'cancel':
        lead.cancelled = True
        changes.append('Appointment cancelled')

    if action == 'disposition' and parsed.get('new_disposition'):
        dispo = parsed['new_disposition']
        lead.disposition = dispo
        changes.append(f"Disposition set to {DISPO_NAMES.get(dispo, dispo)}")

    if action == 'assign' and parsed.get('rep_name'):
        rep_name = parsed['rep_name']
        rep = Rep.objects.filter(name__icontains=rep_name, active=True).first()
        if rep:
            lead.rep = rep
            changes.append(f"Assigned to {rep.name}")
        else:
            changes.append(f"Could not find active rep '{rep_name}'")

    if parsed.get('notes'):
        lead.call_notes = parsed['notes']
        changes.append(f"Notes: {parsed['notes']}")

    if changes:
        lead.save()

        if action in ('cancel', 'disposition'):
            _send_ghl_dispo_webhook(lead, source='sms')

    return changes


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
    import zoneinfo
    eastern = zoneinfo.ZoneInfo('America/New_York')
    today = datetime.now(tz=eastern).date()

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


@manager_required
def ghl_debug_view(request):
    type_filter = request.GET.get('type', '')
    success_filter = request.GET.get('success', '')
    logs = GHLWebhookLog.objects.order_by('-created_at')
    if type_filter:
        logs = logs.filter(webhook_type=type_filter)
    if success_filter == '1':
        logs = logs.filter(success=True)
    elif success_filter == '0':
        logs = logs.filter(success=False)
    total = GHLWebhookLog.objects.count()
    success_count = GHLWebhookLog.objects.filter(success=True).count()
    fail_count = GHLWebhookLog.objects.filter(success=False).count()
    logs = logs[:100]
    return render(request, 'maps/ghl_debug.html', {
        'logs': logs,
        'total': total,
        'success_count': success_count,
        'fail_count': fail_count,
        'active_tab': 'ghl_debug',
        'type_filter': type_filter,
        'success_filter': success_filter,
        'ghl_dispo_url': GHL_WEBHOOK_URL,
        'ghl_appt_url': GHL_APPT_WEBHOOK_URL,
    })


@csrf_exempt
@manager_required
def ghl_test_api(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only'}, status=405)
    webhook_type = request.POST.get('type', 'disposition')
    log_entry = GHLWebhookLog(
        webhook_type='test',
        lead_name='Test Webhook',
        source='test',
    )
    if webhook_type == 'appointment':
        params = urllib.parse.urlencode({
            'phone': '+10000000000',
            'appointment_type': 'Solar',
            'appointment_datetime': '01-01-2000 12:00 PM',
        })
        url = GHL_APPT_WEBHOOK_URL + '?' + params
        log_entry.url = url
        log_entry.payload = json.dumps({'phone': '+10000000000', 'appointment_type': 'Solar', 'appointment_datetime': '01-01-2000 12:00 PM'})
        try:
            req = urllib.request.Request(url, method='GET')
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode('utf-8', errors='replace')
            log_entry.response_status = resp.status
            log_entry.response_body = body[:2000]
            log_entry.success = 200 <= resp.status < 300
        except Exception as e:
            log_entry.error_message = str(e)
    else:
        payload = {
            'phone': '+10000000000',
            'name': 'Test Webhook',
            'disposition': 'Sale',
            'call_transcript': 'This is a test webhook from Sutton GHL diagnostics.',
        }
        log_entry.url = GHL_WEBHOOK_URL
        log_entry.payload = json.dumps(payload)
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                GHL_WEBHOOK_URL,
                data=data,
                headers={'Content-Type': 'application/json'},
            )
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode('utf-8', errors='replace')
            log_entry.response_status = resp.status
            log_entry.response_body = body[:2000]
            log_entry.success = 200 <= resp.status < 300
        except Exception as e:
            log_entry.error_message = str(e)
    log_entry.save()
    return JsonResponse({
        'success': log_entry.success,
        'status': log_entry.response_status,
        'response_body': log_entry.response_body[:500],
        'error': log_entry.error_message,
    })


@manager_required
def calls_view(request):
    reps = Rep.objects.filter(is_active=True).order_by('name')
    logs = VoiceCallLog.objects.select_related('rep').order_by('-created_at')

    rep_filter = request.GET.get('rep')
    if rep_filter:
        logs = logs.filter(rep_id=rep_filter)
    date_filter = request.GET.get('date')
    if date_filter:
        logs = logs.filter(created_at__date=date_filter)
    search = request.GET.get('q', '').strip()
    if search:
        logs = logs.filter(
            Q(transcript__icontains=search) | Q(summary__icontains=search)
        )

    logs = logs[:200]
    return render(request, 'maps/calls.html', {
        'logs': logs, 'reps': reps, 'active_tab': 'calls',
        'selected_rep': rep_filter or '', 'selected_date': date_filter or '', 'search_q': search,
    })


@manager_required
def users_view(request):
    reps = list(Rep.objects.order_by('name').values('id', 'name'))
    import json as json_mod
    raw_sources = Lead.objects.exclude(source='').values_list('source', flat=True).distinct()
    seen = {}
    for s in raw_sources:
        key = s.strip().lower()
        if key and key not in seen:
            seen[key] = s.strip()
    all_sources = sorted(seen.values(), key=lambda x: x.lower())
    return render(request, 'maps/users.html', {
        'reps_json': json_mod.dumps(reps),
        'sources_json': json_mod.dumps(all_sources),
        'active_tab': 'users',
    })


@csrf_exempt
@manager_required
def users_api(request):
    """GET: list users. POST: create user."""
    if request.method == 'GET':
        users = User.objects.select_related('profile', 'profile__rep').order_by('username')
        data = [{
            'id': u.id,
            'username': u.username,
            'role': u.profile.role if hasattr(u, 'profile') else 'unknown',
            'rep_id': u.profile.rep_id if hasattr(u, 'profile') else None,
            'rep_name': u.profile.rep.name if hasattr(u, 'profile') and u.profile.rep else '',
            'is_active': u.is_active,
            'lead_sources': u.profile.lead_sources if hasattr(u, 'profile') else '',
            'hourly_availability': u.profile.hourly_availability if hasattr(u, 'profile') else False,
        } for u in users]
        return JsonResponse(data, safe=False)

    if request.method == 'POST':
        data = json.loads(request.body)
        username = data.get('username', '').strip()
        password = data.get('password', '')
        role = data.get('role', 'rep')
        rep_id = data.get('rep_id')
        if not username or not password:
            return JsonResponse({'error': 'Username and password required'}, status=400)
        if User.objects.filter(username=username).exists():
            return JsonResponse({'error': 'Username already taken'}, status=400)
        user = User.objects.create_user(username=username, password=password)
        rep = Rep.objects.get(pk=rep_id) if rep_id else None
        lead_sources = data.get('lead_sources', '')
        hourly = data.get('hourly_availability', False)
        UserProfile.objects.create(user=user, role=role, rep=rep, lead_sources=lead_sources, hourly_availability=hourly)
        return JsonResponse({'status': 'ok', 'id': user.id})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@manager_required
def user_update_api(request, pk):
    """PUT: update user. DELETE: delete user."""
    user = get_object_or_404(User, pk=pk)

    if request.method == 'DELETE':
        if user == request.user:
            return JsonResponse({'error': 'Cannot delete yourself'}, status=400)
        user.delete()
        return JsonResponse({'status': 'ok'})

    if request.method == 'PUT':
        data = json.loads(request.body)
        profile, _ = UserProfile.objects.get_or_create(user=user)
        if 'role' in data:
            profile.role = data['role']
        if 'rep_id' in data:
            profile.rep = Rep.objects.get(pk=data['rep_id']) if data['rep_id'] else None
        if 'lead_sources' in data:
            profile.lead_sources = data['lead_sources']
        if 'hourly_availability' in data:
            profile.hourly_availability = data['hourly_availability']
        profile.save()
        if 'is_active' in data:
            user.is_active = data['is_active']
            user.save()
        if 'password' in data and data['password']:
            user.set_password(data['password'])
            user.save()
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@login_required
def lead_updates_api(request, lead_id):
    """GET: list updates for a lead. POST: add an update."""
    lead = get_object_or_404(Lead, pk=lead_id)
    user_rep = get_user_rep(request.user)
    if user_rep and lead.rep != user_rep:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    if request.method == 'GET':
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo('America/New_York')
        updates = lead.updates.select_related('user').order_by('created_at')
        data = [{
            'id': u.id,
            'username': u.user.username,
            'text': u.text,
            'created_at': u.created_at.astimezone(eastern).strftime('%m/%d/%Y %I:%M %p'),
        } for u in updates]
        return JsonResponse(data, safe=False)

    if request.method == 'POST':
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo('America/New_York')
        data = json.loads(request.body)
        text = data.get('text', '').strip()
        if not text:
            return JsonResponse({'error': 'Text required'}, status=400)
        update = LeadUpdate.objects.create(lead=lead, user=request.user, text=text)
        return JsonResponse({
            'status': 'ok',
            'id': update.id,
            'username': request.user.username,
            'text': update.text,
            'created_at': update.created_at.astimezone(eastern).strftime('%m/%d/%Y %I:%M %p'),
        })

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@login_required
def lead_messages_api(request, lead_id):
    """Return all SMS messages for a lead, grouped by phone number."""
    lead = get_object_or_404(Lead, pk=lead_id)
    user_rep = get_user_rep(request.user)
    if user_rep and lead.rep != user_rep:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    from collections import OrderedDict
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')
    messages = lead.messages.order_by('created_at')

    threads = OrderedDict()
    for msg in messages:
        if msg.phone_number not in threads:
            threads[msg.phone_number] = []
        threads[msg.phone_number].append({
            'direction': msg.direction,
            'body': msg.body,
            'created_at': msg.created_at.astimezone(eastern).strftime('%m/%d/%Y %I:%M %p'),
        })

    # Fallback: show original raw_message if no stored messages yet
    if not threads and lead.raw_message and lead.from_number:
        threads[lead.from_number] = [{
            'direction': 'inbound',
            'body': lead.raw_message,
            'created_at': lead.created_at.strftime('%m/%d/%Y %I:%M %p'),
        }]

    return JsonResponse({
        'threads': threads,
        'lead_name': lead.homeowner_name,
    })


def _match_textblast_claim(body, textblast_leads):
    """Match a rep's SMS reply to a TextBlast appointment.
    Returns the matched Lead or None."""
    text = body.strip()

    # Try matching a plain number (e.g. "1", "2", "#3")
    num_match = re.match(r'^#?\s*(\d+)$', text)
    if num_match:
        idx = int(num_match.group(1)) - 1
        if 0 <= idx < len(textblast_leads):
            return textblast_leads[idx]
        return None

    # Try matching by city name (e.g. "I can take the one in Waltham")
    text_lower = text.lower()
    for lead in textblast_leads:
        if lead.city and lead.city.lower() in text_lower:
            return lead

    # Try matching by address fragment
    for lead in textblast_leads:
        if lead.address:
            # Match street name (first significant word of address)
            addr_words = [w.lower() for w in lead.address.split() if len(w) > 2 and not w.isdigit()]
            for word in addr_words:
                if word in text_lower:
                    return lead

    return None


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
                            date_str = f'{p.start_date:%m/%d/%Y}' if not p.end_date or p.end_date == p.start_date else f'{p.start_date:%m/%d/%Y}-{p.end_date:%m/%d/%Y}'
                        lines.append(f'  #{p.id} — {p.rep.name} {date_str} {time_str}')
                        send_sms(from_number, '\n'.join(lines))
                    else:
                        send_sms(from_number, 'No pending time off requests.')

                if tor:
                    new_status = 'approved' if is_approve else 'denied'
                    tor.status = new_status
                    tor.save(update_fields=['status'])
                    date_str = f'{tor.start_date:%m/%d/%Y}' if not tor.end_date or tor.end_date == tor.start_date else f'{tor.start_date:%m/%d/%Y} to {tor.end_date:%m/%d/%Y}'
                    send_sms(from_number, f'{tor.rep.name} time off {date_str} has been {new_status}.')
                    if tor.rep.phone_number:
                        send_sms(tor.rep.phone_number, f'Your time off request for {date_str} has been {new_status}.')

                return HttpResponse(
                    '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type='text/xml',
                )

    # Check if sender is a manager with an update request (not APPROVE/DENY)
    if from_number and body:
        manager = Manager.objects.filter(phone_number__icontains=from_number[-10:]).first()
        if manager:
            parsed = parse_manager_update_sms(body)
            if parsed and parsed.get('action') not in ('unknown', '', None):
                matches = find_leads_for_update(parsed)

                if len(matches) == 1:
                    lead = matches[0]
                    LeadMessage.objects.create(lead=lead, phone_number=from_number, direction='inbound', body=body)
                    changes = apply_manager_sms_update(lead, parsed)
                    if changes:
                        rep_name = lead.rep.name if lead.rep else 'Unassigned'
                        msg = f"Updated {lead.homeowner_name}:\n"
                        msg += '\n'.join(f"- {c}" for c in changes)
                        msg += f"\n\nRep: {rep_name}"
                        if lead.appointment_datetime:
                            from django.utils import timezone as tz
                            try:
                                import zoneinfo
                                eastern = zoneinfo.ZoneInfo('America/New_York')
                            except ImportError:
                                eastern = tz.get_fixed_timezone(-300)
                            msg += f"\nAppt: {lead.appointment_datetime.astimezone(eastern).strftime('%m/%d/%Y at %I:%M %p')}"
                        send_sms(from_number, msg)
                        LeadMessage.objects.create(lead=lead, phone_number=from_number, direction='outbound', body=msg)
                    else:
                        send_sms(from_number, "Couldn't determine what to update. Include the homeowner name and the change (e.g. 'Reschedule Smith to Friday 2pm').")

                elif len(matches) > 1:
                    from django.utils import timezone as tz
                    try:
                        import zoneinfo
                        eastern = zoneinfo.ZoneInfo('America/New_York')
                    except ImportError:
                        eastern = tz.get_fixed_timezone(-300)
                    msg = f"Found {len(matches)} matches:\n"
                    for lead in matches:
                        dt = lead.appointment_datetime.astimezone(eastern).strftime('%m/%d at %I:%M %p') if lead.appointment_datetime else 'No date'
                        rep_name = lead.rep.name if lead.rep else 'Unassigned'
                        msg += f"\n- {lead.homeowner_name} — {dt} ({rep_name})"
                    msg += "\n\nPlease text again with the full name."
                    send_sms(from_number, msg)

                else:
                    send_sms(from_number, "Couldn't find that appointment. Please include the homeowner's full name and try again.")

                return HttpResponse(
                    '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    content_type='text/xml',
                )

            send_sms(from_number, "I didn't understand that update. Try something like:\n- 'Reschedule Smith to Friday 2pm'\n- 'Cancel Garcia'\n- 'Johnson was a sale'\n- 'Give Davis to Mike'")
            return HttpResponse(
                '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                content_type='text/xml',
            )

    # Check for APPOINTMENT CANCELLED format
    if body and 'APPOINTMENT CANCELLED' in body.upper():
        cancel_fields = {}
        for line in body.splitlines():
            line = line.strip()
            if ':' in line:
                key, val = line.split(':', 1)
                key = key.strip().lower()
                val = val.strip()
                if 'name' in key:
                    cancel_fields['name'] = val
                elif 'phone' in key:
                    cancel_fields['phone'] = val
                elif 'address' in key:
                    cancel_fields['address'] = val
                elif 'city' in key:
                    cancel_fields['city'] = val
                elif 'day and time' in key or 'time' in key:
                    cancel_fields['datetime'] = val

        name = cancel_fields.get('name', '')
        phone = cancel_fields.get('phone', '')
        address = cancel_fields.get('address', '')
        lead = None
        if name and phone:
            lead = Lead.objects.filter(
                homeowner_name__iexact=name,
                phone_number__icontains=phone[-10:]
            ).order_by('-created_at').first()
        if not lead and name and address:
            lead = Lead.objects.filter(
                homeowner_name__iexact=name,
                address__icontains=address
            ).order_by('-created_at').first()
        if not lead and name:
            lead = Lead.objects.filter(
                homeowner_name__iexact=name
            ).order_by('-created_at').first()

        if lead:
            lead.cancelled = True
            lead.raw_message = body
            lead.save(update_fields=['cancelled', 'raw_message'])
            LeadMessage.objects.create(lead=lead, phone_number=from_number, direction='inbound', body=body)
            system_user = User.objects.filter(is_superuser=True).first()
            if system_user:
                LeadUpdate.objects.create(lead=lead, user=system_user, text='Appointment cancelled via SMS')
            send_sms(from_number, f"Cancelled: {lead.homeowner_name} appointment has been marked as cancelled.")
        else:
            Lead.objects.create(
                address=cancel_fields.get('address', ''),
                city=cancel_fields.get('city', ''),
                homeowner_name=name,
                phone_number=phone,
                from_number=from_number,
                raw_message=body,
                cancelled=True,
            )
            send_sms(from_number, f"Cancelled: {name or 'Unknown'} — no matching appointment found, saved as cancelled lead.")

        return HttpResponse(
            '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            content_type='text/xml',
        )

    # Check if sender is a rep
    rep = Rep.objects.filter(phone_number__icontains=from_number[-10:]).first() if from_number else None

    # Check for TextBlast claim reply
    if rep and body:
        textblast_rep = Rep.objects.filter(name='TextBlast').first()
        if textblast_rep:
            textblast_leads = list(
                Lead.objects.filter(
                    rep=textblast_rep,
                    textblast_sent_at__isnull=False,
                    cancelled=False,
                    appointment_datetime__isnull=False,
                ).order_by('appointment_datetime')
            )
            if textblast_leads:
                claimed_lead = _match_textblast_claim(body, textblast_leads)
                if claimed_lead:
                    from zoneinfo import ZoneInfo
                    eastern = ZoneInfo('America/New_York')
                    # Assign to claiming rep
                    claimed_lead.rep = rep
                    claimed_lead.textblast_sent_at = None
                    claimed_lead.save(update_fields=['rep', 'textblast_sent_at'])
                    # Log as lead update
                    from django.contrib.auth.models import User
                    system_user = User.objects.filter(is_superuser=True).first()
                    if system_user:
                        LeadUpdate.objects.create(
                            lead=claimed_lead,
                            user=system_user,
                            text=f'{rep.name} claimed this appointment via TextBlast',
                        )
                    dt = claimed_lead.appointment_datetime.astimezone(eastern)
                    send_sms(rep.phone_number,
                        f"You're assigned! {claimed_lead.address}, {claimed_lead.city} "
                        f"at {dt.strftime('%I:%M %p').lstrip('0')} "
                        f"({(claimed_lead.appointment_type or 'unknown').upper()})")
                    return HttpResponse(
                        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        content_type='text/xml',
                    )

    # Check if sender is a rep — if so, treat as time off request
    if rep and body:
        time_off = parse_time_off_request(body, rep)
        if time_off:
            for req_date, start_t, end_t, reason in time_off:
                tor = TimeOffRequest.objects.create(
                    rep=rep,
                    start_date=req_date,
                    end_date=req_date,
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

            # Parse appointment datetime (treat as Eastern time)
            appt_datetime = None
            raw_datetime = fields.get('appointment_datetime', '')
            if raw_datetime:
                try:
                    import zoneinfo
                    eastern = zoneinfo.ZoneInfo('America/New_York')
                    appt_datetime = dateparser.parse(raw_datetime, fuzzy=True)
                    if appt_datetime and appt_datetime.tzinfo is None:
                        appt_datetime = appt_datetime.replace(tzinfo=eastern)
                except (ValueError, OverflowError):
                    pass

            appt_type = normalize_type(fields.get('type', ''))
            tags = ''
            if appt_type == 'solar':
                tags = 'Solar'
            elif appt_type == 'hvac':
                tags = 'Hvac'
            elif appt_type == 'both':
                tags = 'Solar,Hvac'

            # Try to match existing lead by name+phone or name+address
            name = fields.get('name', '')
            phone = fields.get('phone', '')
            existing = None
            if name and phone:
                existing = Lead.objects.filter(
                    homeowner_name__iexact=name,
                    phone_number__icontains=phone[-10:]
                ).order_by('-created_at').first()
            if not existing and name and address:
                existing = Lead.objects.filter(
                    homeowner_name__iexact=name,
                    address__iexact=address
                ).order_by('-created_at').first()

            if existing:
                lead = existing
                changes = []
                update_map = {
                    'address': address,
                    'city': city,
                    'state': fields.get('state', ''),
                    'phone_number': phone,
                    'source': fields.get('source', ''),
                    'appointment_type': appt_type,
                    'appointment_format': normalize_format(fields.get('format', '')),
                    'appointment_datetime': appt_datetime,
                    'appt_notes': fields.get('notes', ''),
                }
                if tags:
                    update_map['tags'] = tags
                # Auto-clear cancelled flag when a lead is rescheduled
                if lead.cancelled and appt_datetime:
                    lead.cancelled = False
                    changes.append('Cancelled → Rescheduled')
                FIELD_DISPLAY = {
                    'address': 'Address', 'city': 'City', 'state': 'State',
                    'phone_number': 'Phone', 'source': 'Source', 'tags': 'Tags',
                    'appointment_type': 'Appt Type', 'appointment_format': 'Appt Format',
                    'appointment_datetime': 'Appt Time', 'appt_notes': 'Appt Notes',
                }
                for field, new_val in update_map.items():
                    if not new_val:
                        continue
                    old_val = getattr(lead, field)
                    old_str = str(old_val) if old_val not in (None, '') else ''
                    new_str = str(new_val) if new_val not in (None, '') else ''
                    if old_str != new_str:
                        label = FIELD_DISPLAY.get(field, field)
                        changes.append(f"{label}: {old_str or '—'} → {new_str}")
                        setattr(lead, field, new_val)
                if 'address' in update_map and update_map['address'] and (update_map['address'] != (existing.address or '')):
                    lead.latitude, lead.longitude = lat, lng
                elif 'city' in update_map and update_map['city'] and (update_map['city'] != (existing.city or '')):
                    lead.latitude, lead.longitude = lat, lng
                if tags and 'tags' in update_map:
                    computed = compute_appointment_type(lead.tags)
                    if computed and computed != lead.appointment_type:
                        lead.appointment_type = computed
                lead.raw_message = body
                lead.save()
                if changes:
                    system_user = User.objects.filter(is_superuser=True).first()
                    if system_user:
                        LeadUpdate.objects.create(lead=lead, user=system_user, text='SMS update:\n' + '\n'.join(changes))
                LeadMessage.objects.create(lead=lead, phone_number=from_number, direction='inbound', body=body)
            else:
                lead = Lead.objects.create(
                    address=address,
                    city=city,
                    state=fields.get('state', ''),
                    latitude=lat,
                    longitude=lng,
                    from_number=from_number,
                    homeowner_name=name,
                    phone_number=phone,
                    source=fields.get('source', ''),
                    tags=tags,
                    appointment_type=appt_type,
                    appointment_format=normalize_format(fields.get('format', '')),
                    appointment_datetime=appt_datetime,
                    appt_notes=fields.get('notes', ''),
                    raw_message=body,
                )
                LeadMessage.objects.create(lead=lead, phone_number=from_number, direction='inbound', body=body)
    except Exception:
        # Always save the lead even if parsing fails
        lead = Lead.objects.create(
            address=body,
            from_number=from_number,
            raw_message=body,
        )
        LeadMessage.objects.create(lead=lead, phone_number=from_number, direction='inbound', body=body)

    # Return empty TwiML response
    return HttpResponse(
        '<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        content_type='text/xml',
    )


# ===== Time block helpers =====
TIME_BLOCKS = [
    ('morning', '9-12 PM', 9, 12),
    ('midday', '12-3 PM', 12, 15),
    ('afternoon', '3-6 PM', 15, 18),
    ('evening', '6-9 PM', 18, 21),
]


def _count_bookings_for_block(date_obj, hour_start, hour_end):
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')
    leads = Lead.objects.filter(appointment_datetime__date=date_obj, cancelled=False)
    count = 0
    for lead in leads:
        if lead.appointment_datetime:
            local_dt = lead.appointment_datetime.astimezone(eastern)
            if hour_start <= local_dt.hour < hour_end:
                count += 1
    return count


def _count_bookings_for_hour(date_obj, hour):
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')
    leads = Lead.objects.filter(appointment_datetime__date=date_obj, cancelled=False)
    count = 0
    for lead in leads:
        if lead.appointment_datetime:
            local_dt = lead.appointment_datetime.astimezone(eastern)
            if local_dt.hour == hour:
                count += 1
    return count


def _get_rep_count(date_obj, block_key):
    try:
        override = RepCountOverride.objects.get(date=date_obj, time_block=block_key)
        return override.count
    except RepCountOverride.DoesNotExist:
        return RepCountDefault.get_default(block_key)


# ===== Rep Count (Manager) =====
@manager_required
def rep_count_view(request):
    return render(request, 'maps/rep_count.html', {'active_tab': 'rep_count'})


@csrf_exempt
@manager_required
def rep_count_default_api(request):
    if request.method == 'GET':
        blocks = ['morning', 'midday', 'afternoon', 'evening']
        defaults = {b: RepCountDefault.get_default(b) for b in blocks}
        return JsonResponse({'defaults': defaults})
    if request.method == 'PUT':
        data = json.loads(request.body)
        block_key = data.get('time_block', '')
        count = data.get('count', 3)
        obj, _ = RepCountDefault.objects.get_or_create(time_block=block_key, defaults={'count': 3})
        obj.count = count
        obj.save()
        return JsonResponse({'status': 'ok', 'time_block': block_key, 'count': obj.count})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@manager_required
def rep_count_overrides_api(request):
    if request.method == 'GET':
        week_start = request.GET.get('week_start')
        if not week_start:
            return JsonResponse({'error': 'week_start required'}, status=400)
        start = datetime.strptime(week_start, '%Y-%m-%d').date()
        from datetime import timedelta
        end = start + timedelta(days=6)
        overrides = RepCountOverride.objects.filter(date__gte=start, date__lte=end)
        data = [{'date': o.date.isoformat(), 'time_block': o.time_block, 'count': o.count} for o in overrides]
        return JsonResponse({'overrides': data})

    if request.method == 'POST':
        data = json.loads(request.body)
        date_str = data.get('date')
        block = data.get('time_block')
        count = data.get('count')
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        obj, _ = RepCountOverride.objects.update_or_create(
            date=date_obj, time_block=block,
            defaults={'count': count}
        )
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@manager_required
def rep_count_bookings_api(request):
    week_start = request.GET.get('week_start')
    if not week_start:
        return JsonResponse({'error': 'week_start required'}, status=400)
    start = datetime.strptime(week_start, '%Y-%m-%d').date()
    from datetime import timedelta
    bookings = []
    for day_offset in range(7):
        date_obj = start + timedelta(days=day_offset)
        for block_key, block_label, hour_start, hour_end in TIME_BLOCKS:
            count = _count_bookings_for_block(date_obj, hour_start, hour_end)
            bookings.append({
                'date': date_obj.isoformat(),
                'time_block': block_key,
                'booked': count,
            })
    return JsonResponse({'bookings': bookings})


# ===== Provider Portal =====
@provider_required
def provider_view(request):
    hourly = False
    if hasattr(request.user, 'profile'):
        hourly = request.user.profile.hourly_availability
    return render(request, 'maps/provider.html', {'active_tab': 'provider', 'hourly_availability': hourly})


@provider_required
def provider_availability_api(request):
    week_start = request.GET.get('week_start')
    if not week_start:
        return JsonResponse({'error': 'week_start required'}, status=400)
    start = datetime.strptime(week_start, '%Y-%m-%d').date()
    from datetime import timedelta

    hourly = False
    if hasattr(request.user, 'profile'):
        hourly = request.user.profile.hourly_availability

    availability = []
    for day_offset in range(7):
        date_obj = start + timedelta(days=day_offset)
        for block_key, block_label, hour_start, hour_end in TIME_BLOCKS:
            rep_count = _get_rep_count(date_obj, block_key)
            booked = _count_bookings_for_block(date_obj, hour_start, hour_end)
            block_open = max(0, rep_count - booked)

            if hourly:
                for h in range(hour_start, hour_end):
                    hour_booked = _count_bookings_for_hour(date_obj, h)
                    label = datetime.strptime(str(h), '%H').strftime('%I %p').lstrip('0')
                    availability.append({
                        'date': date_obj.isoformat(),
                        'hour': h,
                        'hour_label': label,
                        'block_key': block_key,
                        'booked': hour_booked,
                        'open': block_open,
                    })
            else:
                availability.append({
                    'date': date_obj.isoformat(),
                    'time_block': block_key,
                    'rep_count': rep_count,
                    'booked': booked,
                    'open': block_open,
                })
    return JsonResponse({'availability': availability, 'hourly': hourly})


@provider_required
def provider_leads_api(request):
    start_str = request.GET.get('start')
    end_str = request.GET.get('end')
    if not start_str or not end_str:
        return JsonResponse({'error': 'start and end required'}, status=400)
    start = datetime.strptime(start_str, '%Y-%m-%d').date()
    end = datetime.strptime(end_str, '%Y-%m-%d').date()

    profile = request.user.profile
    sources = profile.get_lead_sources_list()
    source_q = Q()
    for s in sources:
        source_q |= Q(source__iexact=s)

    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')

    all_leads = Lead.objects.filter(
        appointment_datetime__date__gte=start,
        appointment_datetime__date__lte=end,
    ).select_related('rep').order_by('-appointment_datetime')

    own_leads = []
    other_leads = []

    for lead in all_leads:
        is_own = any(lead.source.strip().lower() == s.lower() for s in sources) if sources else False
        local_dt = lead.appointment_datetime.astimezone(eastern) if lead.appointment_datetime else None
        time_str = local_dt.strftime('%I:%M %p') if local_dt else ''
        block_label = ''
        if local_dt:
            for bk, bl, hs, he in TIME_BLOCKS:
                if hs <= local_dt.hour < he:
                    block_label = bl
                    break

        if is_own:
            own_leads.append({
                'id': lead.id,
                'homeowner_name': lead.homeowner_name,
                'phone_number': lead.phone_number,
                'address': lead.address,
                'city': lead.city,
                'appointment_datetime': local_dt.strftime('%m/%d/%Y %I:%M %p') if local_dt else '',
                'time': time_str,
                'time_block': block_label,
                'disposition': lead.disposition,
                'rep_name': lead.rep.name if lead.rep else '',
                'appointment_type': lead.appointment_type,
                'source': lead.source,
            })
        else:
            other_leads.append({
                'city': lead.city,
                'time_block': block_label,
                'date': local_dt.strftime('%m/%d/%Y') if local_dt else '',
                'appointment_type': lead.appointment_type,
            })

    return JsonResponse({'own_leads': own_leads, 'other_leads': other_leads})


@provider_required
def provider_crm_view(request):
    return render(request, 'maps/provider_crm.html', {'active_tab': 'provider_crm'})


@provider_required
def provider_crm_api(request):
    profile = request.user.profile
    sources = profile.get_lead_sources_list()
    if not sources:
        return JsonResponse({'leads': []})
    source_q = Q()
    for s in sources:
        source_q |= Q(source__iexact=s)
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')
    leads = Lead.objects.filter(source_q).select_related('rep').order_by('-appointment_datetime')

    search = request.GET.get('search', '').strip().lower()
    data = []
    for lead in leads:
        local_dt = lead.appointment_datetime.astimezone(eastern) if lead.appointment_datetime else None
        row = {
            'id': lead.id,
            'homeowner_name': lead.homeowner_name,
            'phone_number': lead.phone_number,
            'address': lead.address,
            'city': lead.city,
            'state': lead.state,
            'source': lead.source,
            'tags': lead.tags,
            'appointment_type': lead.appointment_type,
            'appointment_format': lead.appointment_format,
            'appointment_datetime': local_dt.strftime('%m/%d/%Y %I:%M %p') if local_dt else '',
            'appointment_date': local_dt.strftime('%Y-%m-%d') if local_dt else '',
            'rep_name': lead.rep.name if lead.rep else '',
            'sat': lead.sat,
            'disposition': lead.disposition,
            'follow_up_date': lead.follow_up_date.isoformat() if lead.follow_up_date else '',
            'call_notes': lead.call_notes,
            'appt_notes': lead.appt_notes,
        }
        if search:
            haystack = (row['homeowner_name'] + row['phone_number'] + row['address'] + row['city']).lower()
            if search not in haystack:
                continue
        data.append(row)
    return JsonResponse({'leads': data})


@provider_required
def provider_slot_api(request):
    date_str = request.GET.get('date')
    block_key = request.GET.get('block')
    if not date_str or not block_key:
        return JsonResponse({'error': 'date and block required'}, status=400)
    date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    block_hours = {bk: (hs, he) for bk, bl, hs, he in TIME_BLOCKS}
    if block_key not in block_hours:
        return JsonResponse({'error': 'invalid block'}, status=400)
    hour_start, hour_end = block_hours[block_key]
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')
    start_dt = datetime.combine(date_obj, datetime.min.time().replace(hour=hour_start), tzinfo=eastern)
    end_dt = datetime.combine(date_obj, datetime.min.time().replace(hour=hour_end), tzinfo=eastern)
    leads = Lead.objects.filter(
        appointment_datetime__gte=start_dt,
        appointment_datetime__lt=end_dt,
        cancelled=False,
    ).select_related('rep')
    items = []
    for lead in leads:
        local_dt = lead.appointment_datetime.astimezone(eastern)
        items.append({
            'city': lead.city,
            'time': local_dt.strftime('%I:%M %p'),
            'appointment_type': lead.appointment_type,
        })
    return JsonResponse({'appointments': items})


# ===== API Tenant System =====

def api_key_required(view_func):
    @wraps(view_func)
    @csrf_exempt
    def wrapper(request, *args, **kwargs):
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if not auth_header.startswith('Bearer '):
            return JsonResponse({'error': 'Authentication required'}, status=401)
        key = auth_header[7:].strip()
        try:
            tenant = APITenant.objects.get(api_key=key)
        except (APITenant.DoesNotExist, ValueError):
            return JsonResponse({'error': 'Authentication required'}, status=401)
        if not tenant.is_active:
            return JsonResponse({'error': 'Authentication required'}, status=401)
        from django.utils import timezone as tz
        tenant.last_used_at = tz.now()
        tenant.save(update_fields=['last_used_at'])
        request.api_tenant = tenant
        return view_func(request, *args, **kwargs)
    return wrapper


def _paginate(queryset, request, default_per_page=50):
    try:
        page = max(1, int(request.GET.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(100, max(1, int(request.GET.get('per_page', default_per_page))))
    except (ValueError, TypeError):
        per_page = default_per_page
    total = queryset.count()
    start = (page - 1) * per_page
    items = queryset[start:start + per_page]
    return items, {
        'page': page,
        'per_page': per_page,
        'total': total,
        'total_pages': (total + per_page - 1) // per_page,
    }


# --- V1 API: Leads ---

@api_key_required
def v1_leads_list(request):
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')

    qs = Lead.objects.select_related('rep').order_by('-created_at')

    date = request.GET.get('date')
    if date:
        qs = qs.filter(appointment_datetime__date=date)
    start = request.GET.get('start')
    if start:
        qs = qs.filter(appointment_datetime__date__gte=start)
    end = request.GET.get('end')
    if end:
        qs = qs.filter(appointment_datetime__date__lte=end)
    rep_id = request.GET.get('rep_id')
    if rep_id:
        qs = qs.filter(rep_id=rep_id)
    disposition = request.GET.get('disposition')
    if disposition:
        qs = qs.filter(disposition=disposition)
    since = request.GET.get('since')
    if since:
        try:
            since_dt = dateparser.parse(since)
            qs = qs.filter(created_at__gte=since_dt)
        except (ValueError, TypeError):
            pass

    leads, pagination = _paginate(qs, request)
    data = []
    for lead in leads:
        local_dt = lead.appointment_datetime.astimezone(eastern) if lead.appointment_datetime else None
        data.append({
            'id': lead.id,
            'homeowner_name': lead.homeowner_name,
            'phone_number': lead.phone_number,
            'address': lead.address,
            'city': lead.city,
            'state': lead.state,
            'latitude': lead.latitude,
            'longitude': lead.longitude,
            'source': lead.source,
            'tags': lead.tags,
            'appointment_type': lead.appointment_type,
            'appointment_format': lead.appointment_format,
            'appointment_datetime': local_dt.isoformat() if local_dt else None,
            'rep_id': lead.rep_id,
            'rep_name': lead.rep.name if lead.rep else None,
            'disposition': lead.disposition,
            'sat': lead.sat,
            'follow_up_date': lead.follow_up_date.isoformat() if lead.follow_up_date else None,
            'call_notes': lead.call_notes,
            'appt_notes': lead.appt_notes,
            'cancelled': lead.cancelled,
            'created_at': lead.created_at.astimezone(eastern).isoformat(),
        })
    return JsonResponse({'leads': data, 'pagination': pagination})


@api_key_required
def v1_lead_detail(request, pk):
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')

    if request.method == 'GET':
        lead = get_object_or_404(Lead, pk=pk)
        local_dt = lead.appointment_datetime.astimezone(eastern) if lead.appointment_datetime else None
        data = {
            'id': lead.id,
            'homeowner_name': lead.homeowner_name,
            'phone_number': lead.phone_number,
            'address': lead.address,
            'city': lead.city,
            'state': lead.state,
            'latitude': lead.latitude,
            'longitude': lead.longitude,
            'source': lead.source,
            'tags': lead.tags,
            'appointment_type': lead.appointment_type,
            'appointment_format': lead.appointment_format,
            'appointment_datetime': local_dt.isoformat() if local_dt else None,
            'rep_id': lead.rep_id,
            'rep_name': lead.rep.name if lead.rep else None,
            'disposition': lead.disposition,
            'sat': lead.sat,
            'follow_up_date': lead.follow_up_date.isoformat() if lead.follow_up_date else None,
            'call_notes': lead.call_notes,
            'appt_notes': lead.appt_notes,
            'call_transcript': lead.call_transcript,
            'cancelled': lead.cancelled,
            'created_at': lead.created_at.astimezone(eastern).isoformat(),
        }
        return JsonResponse({'lead': data})

    if request.method == 'PUT':
        lead = get_object_or_404(Lead, pk=pk)
        data = json.loads(request.body)
        allowed = [
            'homeowner_name', 'phone_number', 'address', 'city', 'state',
            'source', 'tags', 'appointment_type', 'appointment_format',
            'appointment_datetime', 'disposition', 'sat', 'follow_up_date',
            'call_notes', 'appt_notes', 'call_transcript', 'cancelled',
        ]
        for field in allowed:
            if field in data:
                value = data[field]
                if field in ('appointment_datetime', 'follow_up_date') and value == '':
                    value = None
                if field == 'sat':
                    value = {'true': True, 'false': False}.get(str(value).lower().strip()) if value != '' else None
                setattr(lead, field, value)
        if 'rep_id' in data:
            lead.rep_id = int(data['rep_id']) if data['rep_id'] else None
        if 'address' in data or 'city' in data:
            geocode_address = lead.address
            if lead.city:
                geocode_address = f"{lead.address}, {lead.city}, MA"
            lead.latitude, lead.longitude = geocode(geocode_address) if lead.address else (None, None)
        lead.save()
        return JsonResponse({'status': 'ok', 'id': lead.id})

    if request.method == 'DELETE':
        lead = get_object_or_404(Lead, pk=pk)
        lead.delete()
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': 'Method not allowed'}, status=405)


@api_key_required
def v1_lead_create(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'Method not allowed'}, status=405)
    data = json.loads(request.body)
    if not data.get('address'):
        return JsonResponse({'error': 'address is required'}, status=400)
    lead = Lead(
        homeowner_name=data.get('homeowner_name', ''),
        phone_number=data.get('phone_number', ''),
        address=data['address'],
        city=data.get('city', ''),
        state=data.get('state', 'MA'),
        source=data.get('source', ''),
        tags=data.get('tags', ''),
        appointment_type=data.get('appointment_type', ''),
        appointment_format=data.get('appointment_format', ''),
        appointment_datetime=data.get('appointment_datetime'),
        disposition=data.get('disposition', ''),
        call_notes=data.get('call_notes', ''),
        appt_notes=data.get('appt_notes', ''),
    )
    if data.get('rep_id'):
        lead.rep_id = int(data['rep_id'])
    geocode_address = lead.address
    if lead.city:
        geocode_address = f"{lead.address}, {lead.city}, MA"
    lead.latitude, lead.longitude = geocode(geocode_address)
    lead.save()
    return JsonResponse({'status': 'ok', 'id': lead.id}, status=201)


# --- V1 API: Reps ---

@api_key_required
def v1_reps_list(request):
    reps = Rep.objects.filter(is_active=True).order_by('name')
    data = [{
        'id': rep.id,
        'name': rep.name,
        'phone_number': rep.phone_number,
        'home_address': rep.home_address,
        'city': rep.city,
        'latitude': rep.latitude,
        'longitude': rep.longitude,
        'specialty': rep.specialty,
        'rating': rep.rating,
        'color': rep.color,
        'is_active': rep.is_active,
    } for rep in reps]
    return JsonResponse({'reps': data})


# --- V1 API: Dashboard / Stats ---

@api_key_required
def v1_stats(request):
    from django.db.models import Count
    qs = Lead.objects.select_related('rep')
    start = request.GET.get('start')
    if start:
        qs = qs.filter(appointment_datetime__date__gte=start)
    end = request.GET.get('end')
    if end:
        qs = qs.filter(appointment_datetime__date__lte=end)
    rep_id = request.GET.get('rep_id')
    if rep_id:
        qs = qs.filter(rep_id=rep_id)

    total = qs.count()
    sales = qs.filter(disposition='sale').count()
    by_dispo = list(qs.values('disposition').annotate(count=Count('id')).order_by('-count'))
    by_rep = list(qs.filter(rep__isnull=False).values('rep__name', 'rep__color').annotate(
        total=Count('id'), sales=Count('id', filter=Q(disposition='sale'))
    ).order_by('-total'))

    return JsonResponse({
        'summary': {
            'total': total,
            'total_sales': sales,
            'conversion_rate': round(sales / total * 100, 1) if total else 0,
        },
        'by_disposition': [{'disposition': r['disposition'] or 'none', 'count': r['count']} for r in by_dispo],
        'by_rep': [{
            'name': r['rep__name'],
            'color': r['rep__color'],
            'total': r['total'],
            'sales': r['sales'],
            'conversion_rate': round(r['sales'] / r['total'] * 100, 1) if r['total'] else 0,
        } for r in by_rep],
    })


# --- V1 API: Time Off ---

@api_key_required
def v1_time_off(request):
    date_str = request.GET.get('date')
    if not date_str:
        return JsonResponse({'error': 'date query param required (YYYY-MM-DD)'}, status=400)
    try:
        target_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'Invalid date format'}, status=400)
    reqs = TimeOffRequest.objects.filter(
        start_date__lte=target_date,
        status='approved',
    ).filter(
        Q(end_date__gte=target_date) | Q(end_date__isnull=True)
    ).select_related('rep')
    data = [{
        'rep_name': r.rep.name,
        'all_day': r.start_time is None,
        'start_time': r.start_time.strftime('%H:%M') if r.start_time else None,
        'end_time': r.end_time.strftime('%H:%M') if r.end_time else None,
        'reason': r.reason,
    } for r in reqs]
    return JsonResponse({'time_off': data})


# ===== Tenant Management (Manager-only) =====

@manager_required
def tenants_view(request):
    tenants = APITenant.objects.order_by('-created_at')
    return render(request, 'maps/tenants.html', {'tenants': tenants, 'active_tab': 'tenants'})


@csrf_exempt
@manager_required
def tenants_api(request):
    if request.method == 'POST':
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        if not name:
            return JsonResponse({'error': 'name is required'}, status=400)
        kwargs = {
            'name': name,
            'notes': data.get('notes', ''),
            'allowed_origins': data.get('allowed_origins', ''),
            'rate_limit': data.get('rate_limit', 1000),
        }
        if data.get('slug'):
            kwargs['slug'] = data['slug'].strip()
        tenant = APITenant.objects.create(**kwargs)
        return JsonResponse({
            'status': 'ok',
            'id': tenant.id,
            'api_key': str(tenant.api_key),
        }, status=201)
    return JsonResponse({'error': 'Method not allowed'}, status=405)


@csrf_exempt
@manager_required
def tenant_update_api(request, pk):
    tenant = get_object_or_404(APITenant, pk=pk)
    if request.method == 'PUT':
        data = json.loads(request.body)
        simple_fields = [
            'name', 'notes', 'allowed_origins', 'rate_limit', 'is_active',
            'slug', 'company_name', 'logo_url',
            'color_primary', 'color_secondary', 'color_accent',
            'color_bg', 'color_text', 'color_text_muted', 'font_family',
        ]
        for field in simple_fields:
            if field in data:
                setattr(tenant, field, data[field])
        tenant.save()
        return JsonResponse({'status': 'ok'})
    if request.method == 'DELETE':
        tenant.delete()
        return JsonResponse({'status': 'ok'})
    return JsonResponse({'error': 'Method not allowed'}, status=405)


# ===== Tenant-Facing Views (Branded) =====

def _get_tenant_or_404(slug):
    return get_object_or_404(APITenant, slug=slug, is_active=True)


def _tenant_context(tenant, active_tab='map', extra=None):
    ctx = {
        'tenant': tenant,
        'tenant_theme': tenant.get_theme(),
        'active_tab': active_tab,
    }
    if extra:
        ctx.update(extra)
    return ctx


def tenant_login_required(view_func):
    @wraps(view_func)
    def wrapper(request, tenant_slug, *args, **kwargs):
        tenant = _get_tenant_or_404(tenant_slug)
        if not request.user.is_authenticated:
            return redirect(f'/t/{tenant_slug}/login/?next={request.path}')
        profile = getattr(request.user, 'profile', None)
        if profile and profile.tenant_id != tenant.id:
            return redirect(f'/t/{tenant_slug}/login/')
        request.tenant = tenant
        return view_func(request, tenant_slug, *args, **kwargs)
    return wrapper


def tenant_manager_required(view_func):
    @wraps(view_func)
    def wrapper(request, tenant_slug, *args, **kwargs):
        tenant = _get_tenant_or_404(tenant_slug)
        if not request.user.is_authenticated:
            return redirect(f'/t/{tenant_slug}/login/?next={request.path}')
        profile = getattr(request.user, 'profile', None)
        if not profile or not profile.is_manager or profile.tenant_id != tenant.id:
            return JsonResponse({'error': 'Forbidden'}, status=403)
        request.tenant = tenant
        return view_func(request, tenant_slug, *args, **kwargs)
    return wrapper


def tenant_login_view(request, tenant_slug):
    tenant = _get_tenant_or_404(tenant_slug)
    ctx = _tenant_context(tenant, 'login')
    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')
        user = authenticate(request, username=username, password=password)
        if user:
            profile = getattr(user, 'profile', None)
            if profile and profile.tenant_id == tenant.id:
                login(request, user)
                next_url = request.GET.get('next', f'/t/{tenant_slug}/')
                return redirect(next_url)
            else:
                ctx['error'] = 'Account not associated with this organization'
        else:
            ctx['error'] = 'Invalid username or password'
    return render(request, 'maps/tenant_login.html', ctx)


def tenant_logout_view(request, tenant_slug):
    logout(request)
    return redirect(f'/t/{tenant_slug}/login/')


@tenant_login_required
def tenant_map_view(request, tenant_slug):
    tenant = request.tenant
    profile = getattr(request.user, 'profile', None)
    is_mgr = profile and profile.is_manager
    user_rep = profile.rep if profile and profile.role == 'rep' else None
    ctx = _tenant_context(tenant, 'map', {
        'is_manager': is_mgr,
        'user_rep': user_rep,
    })
    return render(request, 'maps/index.html', ctx)


@tenant_login_required
def tenant_crm_view(request, tenant_slug):
    tenant = request.tenant
    profile = getattr(request.user, 'profile', None)
    is_mgr = profile and profile.is_manager
    ctx = _tenant_context(tenant, 'crm', {
        'is_manager': is_mgr,
    })
    return render(request, 'maps/crm.html', ctx)


@tenant_login_required
def tenant_daily_view(request, tenant_slug):
    tenant = request.tenant
    from zoneinfo import ZoneInfo
    eastern = ZoneInfo('America/New_York')
    from django.utils import timezone as tz
    today = tz.now().astimezone(eastern).date()
    profile = getattr(request.user, 'profile', None)
    is_mgr = profile and profile.is_manager
    user_rep = get_user_rep(request.user)
    ctx = _tenant_context(tenant, 'daily', {
        'today': today.isoformat(),
        'is_manager': is_mgr,
        'user_rep_id': user_rep.id if user_rep else 'null',
    })
    return render(request, 'maps/daily.html', ctx)


@tenant_manager_required
def tenant_dashboard_view(request, tenant_slug):
    tenant = request.tenant
    ctx = _tenant_context(tenant, 'dashboard', {'is_manager': True})
    return render(request, 'maps/dashboard.html', ctx)


@tenant_manager_required
def tenant_reps_view(request, tenant_slug):
    tenant = request.tenant
    active_reps = Rep.objects.filter(is_active=True).order_by('-rating', 'name')
    inactive_reps = Rep.objects.filter(is_active=False).order_by('-rating', 'name')
    ctx = _tenant_context(tenant, 'reps', {
        'active_reps': active_reps,
        'inactive_reps': inactive_reps,
        'is_manager': True,
    })
    return render(request, 'maps/reps.html', ctx)


@tenant_manager_required
def tenant_time_off_view(request, tenant_slug):
    tenant = request.tenant
    ctx = _tenant_context(tenant, 'time_off', {'is_manager': True})
    return render(request, 'maps/time_off.html', ctx)
