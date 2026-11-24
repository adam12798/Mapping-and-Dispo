"""Slot availability and claims for Lurch, the setters' booking app.

Ventana only: every endpoint is behind views.ventana_api_key_required (the
Ventana slug AND the organization's own slot_claims_enabled, off by default).

    GET  /api/v1/availability/?week_start=YYYY-MM-DD[&days=7]
    POST /api/v1/availability/book/     {idempotency_key, session_ref, start_time | date + time_block, phone?}
    POST /api/v1/availability/release/  {claim_id} or {session_ref}

The blocks are views.TIME_BLOCKS, in New York time. A block's capacity is the
manager's rep count for it (/rep-count/: the date's override, else the block's
default). A block's booked count is its non-cancelled leads plus its live
claims. A booking may start at any minute inside a block; the GET suggests
start times every slot_start_step_minutes. A booking into a full block is
refused (409) or, with slot_full_policy 'flag', taken and marked over
capacity. A claim reserves one spot while a setter books. It is not a
lead: the lead arrives later through Wednesday's push (v1 lead create), which
consumes the claim (save_lead_against_claims). A claim stops counting once it
is consumed, released, or older than the organization's slot_claim_ttl_hours.

Every error is {"error": <code>, "detail": <sentence>}. Nothing here sends
anything (no SMS, email, call or webhook) or creates a lead. Contract:
INTEGRATION.md §6.7.
"""
import json
import logging
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, connection, transaction
from django.db.models import Count
from django.http import JsonResponse
from django.utils import timezone

from .models import Lead, SlotBlockLock, SlotClaim
from .views import TIME_BLOCKS, _count_bookings_for_block, _get_rep_count, ventana_api_key_required

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo('America/New_York')
BLOCKS = {key: (label, start, end) for key, label, start, end in TIME_BLOCKS}
MAX_DAYS = 14       # one availability request covers at most two weeks
MAX_REF = 100       # idempotency_key: the column's width
# First key of the two-key advisory lock a booking takes on its session, so
# these locks can't collide with any other advisory lock on the database.
SESSION_LOCK_SPACE = 51070


class _Refusal(Exception):
    def __init__(self, status, code, detail, **extra):
        super().__init__(detail)
        self.status, self.code, self.detail, self.extra = status, code, detail, extra

    def response(self):
        return _error(self.status, self.code, self.detail, **self.extra)


def _error(status, code, detail, **extra):
    return JsonResponse({'error': code, 'detail': detail, **extra}, status=status)


# ── Blocks, start times and counts ────────────────────────────────────────

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


def _start_times(day, key, step_minutes, now):
    """The block's exact start times still ahead of now: every step from the
    block's start, the last one leaving a step before the block ends."""
    begins, ends = _block_times(day, key)
    step = timedelta(minutes=max(1, step_minutes))
    starts, at = [], begins
    while at + step <= ends:
        if at > now:
            starts.append(at)
        at += step
    return starts


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


def _lock_session(session_ref):
    """Holds a lock on the setter's call (session_ref) until the transaction
    ends, so one call's requests run one at a time. Lock order: the session,
    then the block. (The lead path takes only the block.) SQLite: nothing."""
    if connection.vendor != 'postgresql':
        return
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_xact_lock(%s, hashtext(%s))', [SESSION_LOCK_SPACE, session_ref])


def _phone10(value):
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ''


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _iso(value):
    return value.astimezone(EASTERN).isoformat() if value else None


# ── Request parsing ───────────────────────────────────────────────────────

def _json(request):
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        raise _Refusal(400, 'invalid_body', 'The body must be JSON.')
    if not isinstance(data, dict):
        raise _Refusal(400, 'invalid_body', 'The body must be a JSON object.')
    return data


def _date(value, name):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise _Refusal(400, f'invalid_{name}', f'{name} must be a date, YYYY-MM-DD.')


def _days(value):
    if value in (None, ''):
        return 7
    try:
        days = int(value)
    except (TypeError, ValueError):
        days = 0
    if not 1 <= days <= MAX_DAYS:
        raise _Refusal(400, 'invalid_days', f'days must be a whole number from 1 to {MAX_DAYS}.')
    return days


def _time_block(value):
    if not isinstance(value, str) or value not in BLOCKS:
        raise _Refusal(400, 'invalid_time_block', f"time_block must be one of: {', '.join(BLOCKS)}.")
    return value


def _idempotency_key(value):
    if not isinstance(value, str) or not value.strip():
        raise _Refusal(400, 'invalid_idempotency_key', 'idempotency_key is required (a string).')
    value = value.strip()
    if len(value) > MAX_REF:
        raise _Refusal(400, 'invalid_idempotency_key', f'idempotency_key is longer than {MAX_REF} characters.')
    return value


def _session_ref(value):
    parsed = _uuid(value) if isinstance(value, str) else None
    if parsed is None:
        raise _Refusal(400, 'invalid_session_ref',
                       'session_ref must be a UUID, a new one for each setter call.')
    return str(parsed)


def _start_time(value):
    """An aware datetime in New York time, to the minute, or None when not
    given. A time with no offset is New York time."""
    if value in (None, ''):
        return None
    try:
        when = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        when = None
    if when is None:
        raise _Refusal(400, 'invalid_start_time', 'start_time must be an ISO date and time, e.g. '
                       '2026-12-01T15:30:00-05:00.')
    if when.second or when.microsecond:
        raise _Refusal(400, 'invalid_start_time', 'start_time must be a whole minute (no seconds).')
    if when.tzinfo is None:
        when = when.replace(tzinfo=EASTERN)
    return when.astimezone(EASTERN)


def _slot(data):
    """(date, block key, start or None) of a booking. A start_time decides its
    own block; date and time_block are then optional, and must agree when
    given. Without a start_time, date and time_block are required."""
    start = _start_time(data.get('start_time'))
    raw_day, raw_key = data.get('date'), data.get('time_block')
    if start is None:
        return _date(raw_day, 'date'), _time_block(raw_key), None
    day = _date(raw_day, 'date') if raw_day not in (None, '') else None
    key = _time_block(raw_key) if raw_key not in (None, '') else None
    slot = block_of(start)
    if slot is None:
        raise _Refusal(422, 'start_outside_block', 'start_time is not inside any time block.')
    if (day or slot[0], key or slot[1]) != slot:
        raise _Refusal(422, 'start_outside_block', 'start_time is not inside that date and time_block.')
    return slot[0], slot[1], start


def _bookable(day, key, start, now):
    """Refuses a block that has ended, or a start that is not ahead of now."""
    _begins, ends = _block_times(day, key)
    if ends <= now:
        raise _Refusal(422, 'block_past', 'That block has already ended.')
    if start is not None and start <= now:
        raise _Refusal(422, 'block_past', 'That start time has already passed.')


def _claim_body(claim, now):
    begins, ends = _block_times(claim.date, claim.time_block)
    return {
        'claim_id': str(claim.claim_id),
        'date': claim.date.isoformat(),
        'time_block': claim.time_block,
        'block_start': begins.isoformat(),
        'block_end': ends.isoformat(),
        'start_time': _iso(claim.start_time),
        'over_capacity': claim.over_capacity,
        'expires_at': _iso(claim.expires_at),
        'state': claim.state(now),
    }


def _replay(claim, day, key, start, now):
    """The answer to a repeated idempotency_key: the original claim, never a
    second one. 200 while it is live (or a lead has consumed it); 409
    claim_not_live once it was released or expired."""
    if (claim.date, claim.time_block, claim.start_time) != (day, key, start):
        return _error(422, 'idempotency_key_reused',
                      'This idempotency_key already claimed a different block or start time.')
    body = _claim_body(claim, now)
    if body['state'] in ('released', 'expired'):
        return _error(409, 'claim_not_live', f"This claim was {body['state']}; book again with a new idempotency_key.",
                      **body)
    body.update(open_after=_open(day, key, now), released=[], replayed=True)
    return JsonResponse(body, status=200)


# ── The endpoints ─────────────────────────────────────────────────────────

@ventana_api_key_required
def availability_week(request):
    """Each block of `days` days from week_start (default 7, at most 14): its
    suggested start times still ahead, rep count, booked (leads + live
    claims), open and bookable. Reads only (the rep count's default row is
    made on first read, as /rep-count/ does)."""
    if request.method != 'GET':
        return _error(405, 'method_not_allowed', 'GET only.')
    try:
        start = _date(request.GET.get('week_start'), 'week_start')
        days = _days(request.GET.get('days'))
    except _Refusal as refusal:
        return refusal.response()
    org = request.slot_claims_org
    step = org.slot_start_step_minutes
    flag_when_full = org.slot_full_policy == 'flag'
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
            starts = _start_times(day, key, step, now)
            open_ = max(0, rep_count - leads - claims)
            blocks.append({
                'date': day.isoformat(),
                'time_block': key,
                'label': label,
                'start': begins.isoformat(),
                'end': ends.isoformat(),
                'start_times': [at.isoformat() for at in starts],
                'rep_count': rep_count,
                'booked': leads + claims,
                'open': open_,
                'bookable': bool(starts) and (open_ > 0 or flag_when_full),
                'leads': leads,
                'claims': claims,
            })
    return JsonResponse({'availability': blocks, 'timezone': 'America/New_York', 'start_step_minutes': step,
                         'full_policy': 'flag' if flag_when_full else 'refuse'})


@ventana_api_key_required
def book_slot(request):
    """Claims one spot in a block, atomically: 201 with the claim, or 409
    slot_full (with slot_full_policy 'flag': 201, marked over_capacity). The
    same idempotency_key again returns the original claim, never a second
    one. A new claim releases its session's other live claims."""
    if request.method != 'POST':
        return _error(405, 'method_not_allowed', 'POST only.')
    try:
        data = _json(request)
        day, key, start = _slot(data)
        idempotency_key = _idempotency_key(data.get('idempotency_key'))
        session_ref = _session_ref(data.get('session_ref'))
        phone10 = _phone10(data.get('phone'))
    except _Refusal as refusal:
        return refusal.response()
    org = request.slot_claims_org
    now = timezone.now()

    earlier = SlotClaim.objects.filter(idempotency_key=idempotency_key).first()
    if earlier is not None:
        return _replay(earlier, day, key, start, now)
    try:
        _bookable(day, key, start, now)
    except _Refusal as refusal:
        return refusal.response()
    try:
        with transaction.atomic():
            _lock_session(session_ref)
            _lock(day, key)
            # A request with this key may have committed while we waited.
            earlier = SlotClaim.objects.filter(idempotency_key=idempotency_key).first()
            if earlier is not None:
                return _replay(earlier, day, key, start, now)
            rep_count = _get_rep_count(day, key)
            # The session's own claims in this block don't count against it:
            # the new claim replaces them (released below).
            booked = _leads_in(day, key) + (
                live_claims(now).filter(date=day, time_block=key).exclude(session_ref=session_ref).count())
            over = booked >= rep_count
            if over and org.slot_full_policy != 'flag':
                return _error(409, 'slot_full', 'That block is full. Pick another.',
                              date=day.isoformat(), time_block=key, open=0)
            claim = SlotClaim.objects.create(
                date=day, time_block=key, start_time=start, over_capacity=over,
                idempotency_key=idempotency_key, session_ref=session_ref, phone10=phone10, created_at=now,
                expires_at=now + timedelta(hours=org.slot_claim_ttl_hours))
            released = list(live_claims(now).filter(session_ref=session_ref).exclude(pk=claim.pk)
                            .values_list('claim_id', flat=True))
            if released:
                SlotClaim.objects.filter(claim_id__in=released).update(released_at=now)
    except IntegrityError:
        # The same key, from a request in another session that claimed meanwhile.
        earlier = SlotClaim.objects.filter(idempotency_key=idempotency_key).first()
        if earlier is None:
            raise
        return _replay(earlier, day, key, start, now)
    body = _claim_body(claim, now)
    body.update(open_after=max(0, rep_count - booked - 1), released=[str(c) for c in released])
    return JsonResponse(body, status=201)


@ventana_api_key_required
def release_slot(request):
    """Releases one claim ({claim_id}) or every open claim of a setter call
    ({session_ref}): the setter skipped, cancelled, or filed a non-appointment
    outcome. A consumed claim belongs to its lead and is never released."""
    if request.method != 'POST':
        return _error(405, 'method_not_allowed', 'POST only.')
    session_ref = None
    try:
        data = _json(request)
        has_claim, has_session = bool(data.get('claim_id')), bool(data.get('session_ref'))
        if has_claim == has_session:
            raise _Refusal(400, 'invalid_body', 'Send claim_id or session_ref, one of them.')
        if has_claim:
            claim_id = _uuid(data['claim_id']) if isinstance(data['claim_id'], str) else None
            if claim_id is None:
                raise _Refusal(400, 'invalid_claim_id', 'claim_id is not a claim id.')
            claims = SlotClaim.objects.filter(claim_id=claim_id)
            if not claims.exists():
                raise _Refusal(404, 'claim_not_found', 'No such claim.')
        else:
            session_ref = _session_ref(data['session_ref'])
            claims = SlotClaim.objects.filter(session_ref=session_ref)
    except _Refusal as refusal:
        return refusal.response()
    now = timezone.now()
    with transaction.atomic():
        if session_ref:
            _lock_session(session_ref)
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
    Takes the block's lock (only the block's), so a claim can't take the spot
    meanwhile."""
    hub = lead.hub_customer_id or '(no hub id)'
    when = _appointment(lead.appointment_datetime)
    slot = block_of(when) if when else None
    if slot is not None:
        _lock(*slot)
    claim = None
    wanted = _uuid(claim_id) if claim_id else None
    if wanted is not None:
        claim = SlotClaim.objects.filter(claim_id=wanted, consumed_at__isnull=True).first()
        if claim is not None and (slot is None or (claim.date, claim.time_block) != slot):
            logger.warning('slot claims: lead %s consumes claim %s for %s %s, but its appointment is %s',
                           hub, claim.claim_id, claim.date, claim.time_block, _iso(when) or 'not set')
    if slot is None:
        return None, (claim.pk if claim else None)
    day, key = slot
    if claim is None:
        live_here = live_claims(now).filter(date=day, time_block=key)
        # Fallback when the push carries no claim (or an unknown one): a live
        # claim in this very block for the same phone.
        phone = _phone10(lead.phone_number)
        if phone:
            claim = live_here.filter(phone10=phone).order_by('created_at').first()
        held = live_here.count()
        if held:
            logger.warning('slot claims: lead %s for %s %s came without a usable claim_id while %d live '
                           'claim(s) hold that block; %s', hub, day, key, held,
                           f'consumed claim {claim.claim_id} by phone' if claim else 'none matched its phone')
    if claim is not None and claim.state(now) == 'live' and (claim.date, claim.time_block) == slot:
        return claim.over_capacity, claim.pk     # its claim held the spot (or was itself over)
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
