"""Slot availability and claims for Lurch, the setters' booking app.

Ventana only: every endpoint is behind views.ventana_api_key_required (the
Ventana slug AND the organization's own slot_claims_enabled, off by default).

    GET  /api/v1/availability/?week_start=YYYY-MM-DD[&days=7]
    POST /api/v1/availability/book/     {date, time_block, idempotency_key, session_ref[, phone]}
    POST /api/v1/availability/release/  {claim_id} or {session_ref}

The blocks are views.TIME_BLOCKS, in New York time. A block's capacity is the
manager's rep count for it (/rep-count/: the date's override, else the block's
default). A block's booked count is its non-cancelled leads plus its live
claims. A claim reserves one spot while a setter books. It is not a lead: the
lead arrives later through Wednesday's push (v1 lead create), which consumes
the claim (save_lead_against_claims). A claim stops counting once it is
consumed, released, or older than the organization's slot_claim_ttl_hours.

Nothing here sends anything (no SMS, email, call or webhook) or creates a
lead. Contract: INTEGRATION.md §6.7.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone

from .models import Lead, SlotBlockLock, SlotClaim
from .views import TIME_BLOCKS, _count_bookings_for_block, _get_rep_count, ventana_api_key_required

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo('America/New_York')
BLOCKS = {key: (label, start, end) for key, label, start, end in TIME_BLOCKS}
MAX_DAYS = 14       # one availability request covers at most two weeks
MAX_REF = 100       # idempotency_key and session_ref: the columns' width


class _Bad(Exception):
    """A 400: the message is the response's error."""


# ── Blocks and counts ─────────────────────────────────────────────────────

def block_of(when):
    """(date, block key) of an aware datetime in New York time, or None when
    it falls in no block. The same rule as views._count_bookings_for_block."""
    local = when.astimezone(EASTERN)
    for key, _label, start, end in TIME_BLOCKS:
        if start <= local.hour < end:
            return local.date(), key
    return None


def _block_times(day, key):
    _label, start, end = BLOCKS[key]
    midnight = datetime(day.year, day.month, day.day)
    return ((midnight + timedelta(hours=start)).replace(tzinfo=EASTERN),
            (midnight + timedelta(hours=end)).replace(tzinfo=EASTERN))


def live_claims(now):
    """The active organization's claims that still hold a spot."""
    return SlotClaim.objects.filter(released_at__isnull=True, consumed_at__isnull=True, expires_at__gt=now)


def _leads_in(day, key):
    _label, start, end = BLOCKS[key]
    return _count_bookings_for_block(day, start, end)


def _open(day, key, now):
    return max(0, _get_rep_count(day, key) - _leads_in(day, key)
               - live_claims(now).filter(date=day, time_block=key).count())


def _lock(day, key):
    """Holds the block's row lock until the surrounding transaction ends.
    Everything that counts a block in order to add to it takes this first."""
    lock, _ = SlotBlockLock.objects.get_or_create(date=day, time_block=key)
    SlotBlockLock.objects.select_for_update().get(pk=lock.pk)


def _phone10(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ''


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


# ── Request parsing ───────────────────────────────────────────────────────

def _json(request):
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        raise _Bad('The body must be JSON.')
    if not isinstance(data, dict):
        raise _Bad('The body must be a JSON object.')
    return data


def _date(value, name):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise _Bad(f'{name} must be a date, YYYY-MM-DD.')


def _days(value):
    if value in (None, ''):
        return 7
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= MAX_DAYS:
        raise _Bad(f'days must be a whole number from 1 to {MAX_DAYS}.')
    return days


def _time_block(value):
    if value not in BLOCKS:
        raise _Bad(f"time_block must be one of: {', '.join(BLOCKS)}.")
    return value


def _ref(value, name):
    if not isinstance(value, str) or not value.strip():
        raise _Bad(f'{name} is required.')
    value = value.strip()
    if len(value) > MAX_REF:
        raise _Bad(f'{name} is longer than {MAX_REF} characters.')
    return value


def _claim_body(claim, now):
    return {
        'claim_id': str(claim.claim_id),
        'date': claim.date.isoformat(),
        'time_block': claim.time_block,
        'expires_at': claim.expires_at.astimezone(EASTERN).isoformat(),
        'state': claim.state(now),
    }


def _replay(claim, day, key, now):
    """The answer to a repeated idempotency_key: the original claim, whatever
    became of it since. Never a second claim."""
    if (claim.date, claim.time_block) != (day, key):
        return JsonResponse({'error': 'idempotency_key_reused',
                             'detail': 'This idempotency_key already claimed a different block.'}, status=422)
    body = _claim_body(claim, now)
    body.update(open_after=_open(day, key, now), released=[], replayed=True)
    return JsonResponse(body, status=200)


# ── The endpoints ─────────────────────────────────────────────────────────

@ventana_api_key_required
def availability_week(request):
    """Each block of `days` days from week_start (default 7, at most 14): its
    rep count, booked (leads + live claims) and open. Reads only (the rep
    count's default row is made on first read, as /rep-count/ does)."""
    if request.method != 'GET':
        return JsonResponse({'error': 'GET only.'}, status=405)
    try:
        start = _date(request.GET.get('week_start'), 'week_start')
        days = _days(request.GET.get('days'))
    except _Bad as bad:
        return JsonResponse({'error': str(bad)}, status=400)
    now = timezone.now()
    held = {
        (row['date'], row['time_block']): row['n']
        for row in live_claims(now).filter(date__gte=start, date__lt=start + timedelta(days=days))
        .values('date', 'time_block').annotate(n=Count('id')).order_by()
    }
    blocks = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        for key, label, _start, _end in TIME_BLOCKS:
            rep_count = _get_rep_count(day, key)
            leads = _leads_in(day, key)
            claims = held.get((day, key), 0)
            begins, ends = _block_times(day, key)
            blocks.append({
                'date': day.isoformat(),
                'time_block': key,
                'label': label,
                'start': begins.isoformat(),
                'end': ends.isoformat(),
                'rep_count': rep_count,
                'booked': leads + claims,
                'open': max(0, rep_count - leads - claims),
                'leads': leads,
                'claims': claims,
            })
    return JsonResponse({'availability': blocks, 'timezone': 'America/New_York'})


@ventana_api_key_required
def book_slot(request):
    """Claims one spot in a block, atomically: 201 with the claim, or 409
    slot_full. The same idempotency_key again returns the original claim (200),
    never a second one. A new claim releases its session's other live claims."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only.'}, status=405)
    try:
        data = _json(request)
        day = _date(data.get('date'), 'date')
        key = _time_block(data.get('time_block'))
        idempotency_key = _ref(data.get('idempotency_key'), 'idempotency_key')
        session_ref = _ref(data.get('session_ref'), 'session_ref')
        phone10 = _phone10(data.get('phone'))
    except _Bad as bad:
        return JsonResponse({'error': str(bad)}, status=400)
    org = request.slot_claims_org
    now = timezone.now()

    earlier = SlotClaim.objects.filter(idempotency_key=idempotency_key).first()
    if earlier is not None:
        return _replay(earlier, day, key, now)
    try:
        with transaction.atomic():
            _lock(day, key)
            # A request with this key may have committed while we waited.
            earlier = SlotClaim.objects.filter(idempotency_key=idempotency_key).first()
            if earlier is not None:
                return _replay(earlier, day, key, now)
            rep_count = _get_rep_count(day, key)
            # The session's own claims in this block don't count against it:
            # the new claim replaces them (released below).
            booked = _leads_in(day, key) + (
                live_claims(now).filter(date=day, time_block=key).exclude(session_ref=session_ref).count())
            if booked >= rep_count:
                return JsonResponse({'error': 'slot_full', 'date': day.isoformat(), 'time_block': key,
                                     'open': 0}, status=409)
            claim = SlotClaim.objects.create(
                date=day, time_block=key, idempotency_key=idempotency_key, session_ref=session_ref,
                phone10=phone10, created_at=now, expires_at=now + timedelta(hours=org.slot_claim_ttl_hours))
            released = list(live_claims(now).filter(session_ref=session_ref).exclude(pk=claim.pk)
                            .values_list('claim_id', flat=True))
            if released:
                SlotClaim.objects.filter(claim_id__in=released).update(released_at=now)
    except IntegrityError:
        # The same key from a request that claimed another block meanwhile.
        earlier = SlotClaim.objects.filter(idempotency_key=idempotency_key).first()
        if earlier is None:
            raise
        return _replay(earlier, day, key, now)
    body = _claim_body(claim, now)
    body.update(open_after=max(0, rep_count - booked - 1), released=[str(c) for c in released])
    return JsonResponse(body, status=201)


@ventana_api_key_required
def release_slot(request):
    """Releases one claim ({claim_id}) or every open claim of a session
    ({session_ref}): the setter skipped, cancelled, or filed a non-appointment
    outcome. A consumed claim belongs to its lead and is never released."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only.'}, status=405)
    try:
        data = _json(request)
        has_claim, has_session = bool(data.get('claim_id')), bool(data.get('session_ref'))
        if has_claim == has_session:
            raise _Bad('Send claim_id or session_ref, one of them.')
        if has_claim:
            claim_id = _uuid(data['claim_id'])
            if claim_id is None:
                raise _Bad('claim_id is not a claim id.')
            claims = SlotClaim.objects.filter(claim_id=claim_id)
            if not claims.exists():
                return JsonResponse({'error': 'claim_not_found'}, status=404)
        else:
            claims = SlotClaim.objects.filter(session_ref=_ref(data['session_ref'], 'session_ref'))
    except _Bad as bad:
        return JsonResponse({'error': str(bad)}, status=400)
    now = timezone.now()
    with transaction.atomic():
        open_ids = list(claims.filter(released_at__isnull=True, consumed_at__isnull=True)
                        .select_for_update().values_list('claim_id', flat=True))
        if open_ids:
            SlotClaim.objects.filter(claim_id__in=open_ids).update(released_at=now)
    return JsonResponse({'released': [str(c) for c in open_ids]})


# ── v1 lead create (views.v1_lead_create, slot claims on only) ────────────

def _appointment(value):
    """The aware datetime lead.save() will store for this body value, or None.
    Mirrors DateTimeField: a naive time is New York time."""
    if value in (None, ''):
        return None
    when = Lead._meta.get_field('appointment_datetime').to_python(value)
    if when is not None and timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_default_timezone())
    return when


def _check(lead, claim_id, now):
    """(over_capacity, pk of the claim to consume) for a lead not yet saved.
    Takes the block's lock, so a claim can't take the spot meanwhile."""
    when = _appointment(lead.appointment_datetime)
    slot = block_of(when) if when else None
    if slot is not None:
        _lock(*slot)
    claim = None
    wanted = _uuid(claim_id) if claim_id else None
    if wanted is not None:
        claim = SlotClaim.objects.filter(claim_id=wanted, consumed_at__isnull=True).first()
    if slot is None:
        return None, (claim.pk if claim else None)
    day, key = slot
    if claim is None:
        # Fallback when the push carries no claim (or an unknown one): a live
        # claim in this very block for the same phone.
        phone = _phone10(lead.phone_number)
        if phone:
            claim = (live_claims(now).filter(date=day, time_block=key, phone10=phone)
                     .order_by('created_at').first())
    if claim is not None and claim.state(now) == 'live' and (claim.date, claim.time_block) == slot:
        return False, claim.pk          # its claim held the spot
    booked = _leads_in(day, key) + live_claims(now).filter(date=day, time_block=key).count()
    return booked >= _get_rep_count(day, key), (claim.pk if claim else None)


def save_lead_against_claims(lead, claim_id):
    """Saves a v1-created lead for an organization with slot claims on.

    Never refuses and never fails because of claims: the customer was already
    promised the time. With a claim (claim_id, else a live claim in the block
    for the same phone) the lead consumes it. Without one, a lead landing in a
    block that is already full is saved with over_capacity=True. If the check
    itself fails, the lead is saved unchecked (over_capacity NULL) and logged."""
    now = timezone.now()
    with transaction.atomic():
        decision = None
        try:
            with transaction.atomic():
                decision = _check(lead, claim_id, now)
        except Exception:
            logger.exception('slot claims: lead %s not checked against its block; saved unchecked',
                             lead.hub_customer_id or '(no hub id)')
        if decision is not None:
            lead.over_capacity = decision[0]
        lead.save()
        if decision is not None and decision[1] is not None:
            try:
                with transaction.atomic():
                    SlotClaim.objects.filter(pk=decision[1], consumed_at__isnull=True).update(
                        consumed_at=now, consumed_by_lead=lead)
            except Exception:
                logger.exception('slot claims: lead %s did not consume its claim', lead.id)
