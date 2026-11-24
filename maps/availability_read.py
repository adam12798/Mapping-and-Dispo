"""Block availability for Lurch, read only. Ventana API keys only.

    GET /api/v1/availability/?week_start=YYYY-MM-DD[&days=7]   (days 1-14)

The numbers are the Provider Portal's (views.provider_availability_api), from
the same functions: views.TIME_BLOCKS in New York time, the rep count from
/rep-count/ (views._get_rep_count: the date's override, else the block's
default), booked = the block's non-cancelled leads
(views._count_bookings_for_block), open = max(0, rep_count - booked).

Nothing here writes, sends or books anything. The one write is the existing
one inside _get_rep_count: a block's default rep-count row is created on its
first read, as the Provider Portal and /rep-count/ already do. Contract:
INTEGRATION.md §6.7.
"""
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .models import APITenant
from .tenancy import get_current_org_id
from .views import TIME_BLOCKS, _count_bookings_for_block, _get_rep_count, api_key_required

EASTERN = ZoneInfo('America/New_York')
ORG_SLUG = 'ventana'
DEFAULT_DAYS = 7
MAX_DAYS = 14


def _unauthenticated():
    # api_key_required's own 401 body, so a client sees one shape for it.
    return JsonResponse({'error': 'Authentication required'}, status=401)


def _not_ventana():
    return JsonResponse({'error': 'Availability is only for Ventana; this key is not a Ventana key.'}, status=403)


def _ventana_key_only(view_func):
    """Lets only an active Ventana key reach api_key_required and the view.

    It runs BEFORE api_key_required, so another organization's key, or a key
    with no organization, is refused having read only its own APITenant row:
    api_key_required never runs for it (no last_used_at stamp, and never its
    default-inbound fallback to Team Sunshine for a key with no organization).
    The key is found exactly as api_key_required finds it: Bearer header,
    then ?api_key=, then X-API-Key."""
    authenticated = api_key_required(view_func)

    @wraps(view_func)
    @csrf_exempt
    def gate(request, *args, **kwargs):
        key = None
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            key = auth_header[7:].strip()
        if not key:
            key = request.GET.get('api_key', '').strip()
        if not key:
            key = request.META.get('HTTP_X_API_KEY', '').strip()
        if not key:
            return _unauthenticated()
        try:
            tenant = APITenant.objects.select_related('organization').get(api_key=key)
        except (APITenant.DoesNotExist, ValueError, ValidationError):
            return _unauthenticated()
        if not tenant.is_active:
            return _unauthenticated()
        org = tenant.organization if tenant.organization_id else None
        if org is None or org.slug != ORG_SLUG or not org.is_active:
            return _not_ventana()
        return authenticated(request, *args, **kwargs)
    return gate


def _days(value):
    if value in (None, ''):
        return DEFAULT_DAYS
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if 1 <= days <= MAX_DAYS else None


@_ventana_key_only
def v1_availability(request):
    # Belt and braces: api_key_required has made the key's own organization
    # the active one; it must be Ventana's.
    org = request.api_tenant.organization
    if org is None or org.slug != ORG_SLUG or get_current_org_id() != org.id:
        return _not_ventana()
    if request.method != 'GET':
        return JsonResponse({'error': 'GET only.'}, status=405)
    try:
        start = datetime.strptime(request.GET.get('week_start', ''), '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': 'week_start is required, as YYYY-MM-DD.'}, status=400)
    days = _days(request.GET.get('days'))
    if days is None:
        return JsonResponse({'error': f'days must be a whole number from 1 to {MAX_DAYS}.'}, status=400)
    if not (2000 <= start.year <= 2100):
        return JsonResponse({'error': 'week_start must be a date between 2000 and 2100.'}, status=400)

    availability = []
    for day_offset in range(days):
        date_obj = start + timedelta(days=day_offset)
        for block_key, block_label, hour_start, hour_end in TIME_BLOCKS:
            rep_count = _get_rep_count(date_obj, block_key)
            booked = _count_bookings_for_block(date_obj, hour_start, hour_end)
            availability.append({
                'date': date_obj.isoformat(),
                'time_block': block_key,
                'label': block_label,
                'start': datetime(date_obj.year, date_obj.month, date_obj.day, hour_start,
                                  tzinfo=EASTERN).isoformat(),
                'end': datetime(date_obj.year, date_obj.month, date_obj.day, hour_end,
                                tzinfo=EASTERN).isoformat(),
                'rep_count': rep_count,
                'booked': booked,
                'open': max(0, rep_count - booked),
            })
    return JsonResponse({
        'week_start': start.isoformat(),
        'days': days,
        'timezone': 'America/New_York',
        'availability': availability,
    })
