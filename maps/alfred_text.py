"""Alfred by text: a rep texts how an appointment went, and Alfred reads it
with the call-in Alfred's own instructions and fields, writes it to the lead
the way a call does, and answers with what it set and what to ask next.

    POST /api/v1/alfred/text/        (API key — Ventana's keys only)
    {
      "rep_id": 12,                  // or "rep_phone": "+16175550001"; both must agree
      "lead_id": 345,                // the Sutton lead
      "text": "sold it, battery",    // the rep's text
      "context": {                   // optional, from the previous exchange
        "last_summary": "Sold, credit passed",
        "last_question": "Any adders on this one?",
        "empty_fields": ["adders", "monthly_cost", "total_cost", "post_appt_notes"]
      }
    }
    → 200 {"set": {...}, "summary": "...", "next_question": "..." | null,
           "appointment_move": {"date": "YYYY-MM-DD", "time": "HH:MM"} | null,
           "still_empty": [...], "understood": true | false}

Pass back each response's summary, next_question and still_empty as the
next text's context. The lead must be the rep's (or have no rep), not
cancelled, and booked between two weeks ago and three days ahead.

The instructions and the field list are voice_ws.py's (imported, not copied),
with a text-mode note on top. Nothing is guessed: what the model leaves out or
gets wrong is not written, and Alfred asks about it instead. A follow-up time
and the appointment itself being moved come back as different fields; a move
is returned, never written — reps can't reschedule (the call says so too).
"""
import copy
import json
import logging
import re
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Case, F, Q, TextField, Value, When
from django.db.models.functions import Concat
from django.http import JsonResponse
from django.utils import timezone

from .models import APIRateWindow, Lead, Rep, VoiceCallLog
from .tenancy import get_current_org_id
from .views import api_key_required, fire_webhooks

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo('America/New_York')
ORG_SLUG = 'ventana'
RATE_SCOPE = 'alfred_text'
TEXTS_PER_HOUR = 60          # per key; a key's own rate_limit lowers it, never raises it
MAX_TEXT = 1600              # a long SMS
# Which appointments a text can debrief. The call reaches the rep's own
# appointments from today to three days out; a text names the lead, so it
# also reaches the last two weeks (a late debrief, a follow-up that closed).
DAYS_BACK, DAYS_AHEAD = 14, 3
MODEL = 'gpt-4o-mini'
TOOL_NAME = 'read_rep_text'

# What Alfred still asks after the outcome, in the call's order. The deal
# questions are the call's own words (a test pins them to voice_ws.py).
QUESTION_ORDER = ['follow_up_date', 'follow_up_time', 'adders', 'monthly_cost', 'total_cost', 'post_appt_notes']
DEAL_FIELDS = ['adders', 'monthly_cost', 'total_cost', 'post_appt_notes']
QUESTIONS = {
    'follow_up_date': 'When would be a good time to follow up with the homeowner?',
    'adders': 'Any adders on this one?',
    'monthly_cost': "What's the monthly looking like?",
    'total_cost': 'And the total?',
    'post_appt_notes': 'Anything else worth noting?',
}
# Which outcomes get a follow-up date and the deal questions (the call asks
# them for sale, follow_up and cpfu; future_contact is a far-out follow_up).
FOLLOW_UP_DISPOSITIONS = {'follow_up', 'cpfu', 'future_contact'}
DEAL_DISPOSITIONS = {'sale'} | FOLLOW_UP_DISPOSITIONS

ASK_OUTCOME = 'Did you sit the appointment?'
ASK_CREDIT = 'Did you run credit?'
CALL_NOTE_WORDS = 19         # "under 20 words"
ASK_FOLLOW_UP_TIME = 'And what time on {day}?'
ASK_MOVE = 'What day and time does the homeowner want instead?'
ASK_UNREADABLE = "Sorry, I couldn't make that out — how did the appointment with {name} go?"
FIELD_LIMITS = {'monthly_cost': 100, 'total_cost': 100, 'adders': 2000, 'post_appt_notes': 2000}

TEXT_MODE = """## TEXT MODE — this overrides anything above that assumes a phone call
This is not a call. {rep} has sent you a text message about ONE appointment, already identified below. Don't ask which appointment and don't confirm the homeowner's name. You never write a reply: you always call read_rep_text exactly once with what this text tells you.

- Decide the disposition with the debrief rules above. Set only what the text (with the context below) actually says. Never guess. If you can't tell the outcome, a date or a time, leave that field out and put the one question that would settle it in clarifying_question: the decision tree's next question (e.g. "Did you run credit?") when the outcome isn't settled yet, or a short "didn't catch that" question when the text can't be read. Set understood to false when the text can't be read as anything about this appointment.
- call_notes is a one-line summary of the appointment under 20 words, the way a manager would jot it: "Needed to talk to wife". Give call_notes whenever you set a disposition, folding in the call note already on the lead when this text adds to it. Leave call_notes out when the text only answers a detail (adders, the monthly, a date).
- When the text adds to a detail already on the lead ("also a panel upgrade"), give the whole new value ("Battery, panel upgrade").
- A follow-up (follow_up_date / follow_up_time) is when to get back to the homeowner after the appointment. The appointment ITSELF being moved — the homeowner asked for another day or time, or the visit is being rebooked — is not a follow-up: put the new day and time in appointment_moved_to_date / appointment_moved_to_time and leave follow_up_date and follow_up_time out. A move is not a disposition.
- Turn relative dates and times into real ones using "Now" and the appointment below. "Same time" means the appointment's time of day. Dates are YYYY-MM-DD, times HH:MM (24-hour). Give a date or time only when the rep gave one; never fill one in.
- If the lead is future_contact and the rep gives a follow-up within a month, set the disposition again (follow_up, or cpfu if credit passed).
- When the rep says there's nothing for a detail or they don't know it ("no adders", "nothing else"), list it in declined_fields instead of writing "none".
- A bare answer ("yes", "189", "no") answers Alfred's last question below.

Now: {now}
Rep: {rep}
Appointment: {appointment}
Disposition on the lead now: {disposition}
On the lead now: {details}"""


# ── The request ───────────────────────────────────────────────────────────

class _Refusal(Exception):
    def __init__(self, status, error):
        super().__init__(error)
        self.status, self.error = status, error


def _ventana_org(tenant):
    """The key's organization when it is Ventana's, else None. api_key_required
    falls back to the default inbound org (Team Sunshine) for a key with no
    organization, so the lock has to look at the key itself."""
    org = tenant.organization if tenant.organization_id else None
    if org is None or org.slug != ORG_SLUG or not org.is_active:
        return None
    return org


def _take_rate_slot(tenant, now):
    """Counts this request against the key's hour. (allowed, retry_after_s)."""
    limit = max(0, min(TEXTS_PER_HOUR, tenant.rate_limit if tenant.rate_limit is not None else TEXTS_PER_HOUR))
    window = now.replace(minute=0, second=0, microsecond=0)
    with transaction.atomic():
        row, created = APIRateWindow.objects.select_for_update().get_or_create(
            tenant=tenant, scope=RATE_SCOPE, window_start=window)
        if created:
            APIRateWindow.objects.filter(
                tenant=tenant, scope=RATE_SCOPE, window_start__lt=window - timedelta(days=2)).delete()
        if row.count >= limit:
            return False, int((window + timedelta(hours=1) - now).total_seconds()) + 1
        APIRateWindow.objects.filter(pk=row.pk).update(count=F('count') + 1)
    return True, 0


def _phone_digits(value):
    digits = re.sub(r'\D', '', str(value or ''))
    return digits[1:] if len(digits) == 11 and digits.startswith('1') else digits


def _int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,18}', value.strip()):
        return int(value.strip())
    return None


def _short(value, limit):
    # PostgreSQL text can't hold NUL, from the rep or from the model.
    return value.replace('\x00', '').strip()[:limit] if isinstance(value, str) else ''


def _parse_body(request):
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        raise _Refusal(400, 'The body must be JSON.')
    if not isinstance(data, dict):
        raise _Refusal(400, 'The body must be a JSON object.')

    lead_id = _int(data.get('lead_id'))
    if lead_id is None:
        raise _Refusal(400, 'lead_id (the Sutton lead id) is required.')
    rep_id = data.get('rep_id')
    if rep_id is not None and _int(rep_id) is None:
        raise _Refusal(400, 'rep_id must be a Sutton rep id.')
    rep_phone = data.get('rep_phone')
    if rep_phone is not None and not _phone_digits(rep_phone):
        raise _Refusal(400, 'rep_phone must be a phone number.')
    if rep_id is None and rep_phone is None:
        raise _Refusal(400, 'rep_id or rep_phone is required.')
    text = data.get('text')
    if not isinstance(text, str) or not _short(text, MAX_TEXT + 1):
        raise _Refusal(400, "text (the rep's message) is required.")
    text = _short(text, MAX_TEXT + 1)
    if len(text) > MAX_TEXT:
        raise _Refusal(400, f'text is longer than {MAX_TEXT} characters.')

    raw = data.get('context')
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _Refusal(400, 'context must be an object.')
    empty = raw.get('empty_fields')
    if empty is not None and not (isinstance(empty, list) and all(isinstance(f, str) for f in empty)):
        raise _Refusal(400, 'context.empty_fields must be a list of field names.')
    context = {
        'last_summary': _short(raw.get('last_summary'), 300),
        'last_question': _short(raw.get('last_question'), 300),
        'empty_fields': None if empty is None else [f for f in QUESTION_ORDER if f in empty],
    }
    return {'lead_id': lead_id, 'rep_id': _int(rep_id), 'rep_phone': rep_phone,
            'text': text, 'context': context}


def _find_rep(rep_id, rep_phone):
    """An active rep of the request's org (Rep.objects is scoped to it)."""
    rep = None
    if rep_id is not None:
        rep = Rep.objects.filter(id=rep_id, is_active=True).first()
        if rep is None:
            raise _Refusal(404, 'No active Ventana rep has that rep_id.')
    if rep_phone is not None:
        wanted = _phone_digits(rep_phone)
        matches = [r for r in Rep.objects.filter(is_active=True) if _phone_digits(r.phone_number) == wanted]
        if rep is not None:
            if rep not in matches:
                raise _Refusal(400, "rep_id and rep_phone don't name the same rep.")
        elif not matches:
            raise _Refusal(404, 'No active Ventana rep has that phone number.')
        elif len(matches) > 1:
            raise _Refusal(409, 'More than one active Ventana rep has that phone number; send rep_id.')
        else:
            rep = matches[0]
    return rep


def _find_lead(lead_id, rep, now):
    lead = Lead.objects.select_related('rep').filter(id=lead_id).first()
    if lead is None:
        raise _Refusal(404, 'No Ventana lead has that lead_id.')
    # The call only writes the rep's own leads. Most of Ventana's leads have
    # no rep in Sutton (reps are kept in Wednesday), so an unassigned lead is
    # the rep's to debrief too; another rep's lead is not.
    if lead.rep_id is not None and lead.rep_id != rep.id:
        raise _Refusal(403, 'That lead is assigned to another rep.')
    if lead.cancelled:
        raise _Refusal(409, 'That appointment is cancelled.')
    today_start = now.astimezone(EASTERN).replace(hour=0, minute=0, second=0, microsecond=0)
    if lead.appointment_datetime is None or not (
            today_start - timedelta(days=DAYS_BACK) <= lead.appointment_datetime <= now + timedelta(days=DAYS_AHEAD)):
        raise _Refusal(409, f'That appointment is not in the last {DAYS_BACK} days or the next {DAYS_AHEAD}.')
    return lead


# ── The model ─────────────────────────────────────────────────────────────

def text_tool():
    """The call's update_disposition fields (minus homeowner_name — the lead
    is given), plus what a text needs: whether it could be read, the question
    to ask when it couldn't, the appointment being moved, and the details the
    rep said there's nothing for. Chat Completions' tool format."""
    from voice_ws import DISPOSITION_TOOL

    props = copy.deepcopy(DISPOSITION_TOOL['parameters']['properties'])
    del props['homeowner_name']
    props['disposition']['description'] += (
        ". Only when the debrief rules settle it; leave out while the outcome isn't clear, "
        "and when the text only adds details to an outcome already on the lead.")
    props['call_notes']['description'] += ' A one-line summary, like "Needed to talk to wife".'
    # On a call Alfred asks until it has them; a text gives what it gives.
    for field in ('follow_up_date', 'follow_up_time'):
        props[field]['description'] = props[field]['description'].replace(
            'Required when disposition is follow_up or cpfu.', 'Only when the rep gave one; never fill one in.')
    props.update({
        'understood': {
            'type': 'boolean',
            'description': 'False when the text cannot be read as anything about this appointment.',
        },
        'clarifying_question': {
            'type': 'string',
            'description': ("One short question, in Alfred's voice, when something is unclear: the decision tree's "
                            'next question, or asking the rep to say it again. Leave out when nothing is unclear.'),
        },
        'appointment_moved_to_date': {
            'type': 'string',
            'description': ('The day the appointment ITSELF is moved to, YYYY-MM-DD — the homeowner asked for another '
                            'day or the visit is being rebooked. Not a follow-up date.'),
        },
        'appointment_moved_to_time': {
            'type': 'string',
            'description': 'The time the appointment itself is moved to, HH:MM (24-hour). Not a follow-up time.',
        },
        'declined_fields': {
            'type': 'array',
            'items': {'type': 'string', 'enum': QUESTION_ORDER},
            'description': 'Details the rep said there is nothing for, or does not know (e.g. "no adders").',
        },
    })
    return {
        'type': 'function',
        'function': {
            'name': TOOL_NAME,
            'description': "Record what the rep's text says about this appointment.",
            'parameters': {'type': 'object', 'properties': props, 'required': ['understood']},
        },
    }


def _dispositions():
    from voice_ws import DISPOSITION_TOOL
    return set(DISPOSITION_TOOL['parameters']['properties']['disposition']['enum'])


def _when(dt_value):
    local = dt_value.astimezone(EASTERN)
    return f"{local:%A, %B} {local.day}, {local.year} ({local:%Y-%m-%d}) at {local:%I:%M %p} ({local:%H:%M})"


def _on_the_lead(lead):
    def quoted(value):
        return f'"{value[:300]}"' if value else 'empty'
    follow_up = ' '.join(x for x in (
        lead.follow_up_date.isoformat() if lead.follow_up_date else '',
        lead.follow_up_time.strftime('%H:%M') if lead.follow_up_time else '') if x) or 'empty'
    sat = {True: 'yes', False: 'no'}.get(lead.sat, 'empty')
    return (f'call note {quoted(lead.call_notes)}; sat {sat}; follow-up {follow_up}; adders {quoted(lead.adders)}; '
            f'monthly {quoted(lead.monthly_cost)}; total {quoted(lead.total_cost)}; '
            f'post-appointment notes {quoted(lead.post_appt_notes)}')


def build_messages(rep, lead, text, context, now):
    from voice_ws import SYSTEM_PROMPT

    if lead.appointment_datetime:
        appointment = f"{lead.homeowner_name or 'the homeowner'}, {_when(lead.appointment_datetime)}"
    else:
        appointment = f"{lead.homeowner_name or 'the homeowner'}, no appointment time on record"
    if lead.appointment_type:
        appointment += f' ({lead.appointment_type})'
    lines = [TEXT_MODE.format(
        rep=rep.name, now=_when(now) + ', Eastern', appointment=appointment,
        disposition=lead.disposition or 'none', details=_on_the_lead(lead))]
    if context['last_summary']:
        lines.append(f"Last summary: {context['last_summary']}")
    if context['last_question']:
        lines.append(f"Alfred's last question: {context['last_question']}")
    if context['empty_fields'] is not None:
        lines.append(f"Details still empty, in the order Alfred asks: {', '.join(context['empty_fields']) or 'none'}")
    return [
        {'role': 'system', 'content': SYSTEM_PROMPT + '\n\n' + '\n'.join(lines)},
        {'role': 'user', 'content': text},
    ]


def _complete(messages, tool):
    """One forced tool call; its arguments. Tests replace this function."""
    from openai import OpenAI

    client = OpenAI(api_key=settings.OPENAI_API_KEY, timeout=20, max_retries=1)
    resp = client.chat.completions.create(
        model=MODEL, messages=messages, tools=[tool],
        tool_choice={'type': 'function', 'function': {'name': TOOL_NAME}},
        temperature=0, max_tokens=500,
    )
    calls = resp.choices[0].message.tool_calls or []
    if not calls:
        return {}
    args = json.loads(calls[0].function.arguments or '{}')
    return args if isinstance(args, dict) else {}


# ── Reading the answer ────────────────────────────────────────────────────

def _date(value):
    try:
        return datetime.strptime(value.strip(), '%Y-%m-%d').date() if isinstance(value, str) else None
    except ValueError:
        return None


def _time(value):
    # The call's parse — "HH:MM" (seconds, if any, ignored) — with the range checked.
    try:
        parts = value.strip().split(':')
        return dt_time(int(parts[0]), int(parts[1]))
    except (AttributeError, ValueError, IndexError):
        return None


def read_answer(args, lead, today):
    """What to write, what to return, and what didn't read. Never a guess:
    a field the model got wrong is dropped and becomes the question."""
    writes, problems = {}, []
    understood = args.get('understood') is not False
    question = _short(args.get('clarifying_question'), 300)
    declined = [f for f in QUESTION_ORDER if f in (args.get('declined_fields') or [])] \
        if isinstance(args.get('declined_fields'), list) else []
    move = None
    if not understood:
        return {'writes': {}, 'problems': [], 'understood': False, 'question': question,
                'declined': [], 'move': None}

    disposition = args.get('disposition')
    if disposition is not None:
        if isinstance(disposition, str) and disposition in _dispositions():
            writes['disposition'] = disposition
        else:
            problems.append(('disposition', ASK_OUTCOME))
    if 'sat' in args and args['sat'] is not None:
        if isinstance(args['sat'], bool):
            writes['sat'] = args['sat']
        else:
            problems.append(('sat', ASK_OUTCOME))

    if args.get('follow_up_date'):
        follow_up_date = _date(args['follow_up_date'])
        if follow_up_date is None or follow_up_date < today:
            problems.append(('follow_up_date', QUESTIONS['follow_up_date']))
        else:
            writes['follow_up_date'] = follow_up_date
    if args.get('follow_up_time'):
        follow_up_time = _time(args['follow_up_time'])
        if follow_up_time is None:
            problems.append(('follow_up_time', 'What time should we follow up?'))
        else:
            writes['follow_up_time'] = follow_up_time

    for field, limit in FIELD_LIMITS.items():
        value = args.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        value = _short(value, limit)
        if value:
            writes[field] = value
    notes = _short(args.get('call_notes'), 1000)
    if notes:
        # Lead.call_notes is 200 characters; the call asks for under 20 words.
        writes['call_notes'] = ' '.join(notes.split()[:CALL_NOTE_WORDS])[:200]

    if args.get('appointment_moved_to_date') or args.get('appointment_moved_to_time'):
        moved_date = _date(args.get('appointment_moved_to_date'))
        moved_time = _time(args.get('appointment_moved_to_time'))
        if moved_date is None or moved_time is None or moved_date < today:
            problems.append(('appointment_move', ASK_MOVE))
        else:
            move = {'date': moved_date.isoformat(), 'time': moved_time.strftime('%H:%M')}

    # The call's rule: a follow_up or cpfu more than a month out is future_contact.
    # A text can give the date after the outcome, so the lead's own counts too.
    outcome = writes.get('disposition') or lead.disposition
    follow_up_date = writes.get('follow_up_date')
    if follow_up_date and outcome in ('follow_up', 'cpfu') and follow_up_date > today + timedelta(days=30):
        writes['disposition'] = 'future_contact'

    return {'writes': writes, 'problems': problems, 'understood': True, 'question': question,
            'declined': declined, 'move': move}


def _to_ask(disposition):
    if disposition in FOLLOW_UP_DISPOSITIONS:
        return QUESTION_ORDER
    if disposition in DEAL_DISPOSITIONS:
        return DEAL_FIELDS
    return []


def _still_empty(lead, writes, declined, context):
    """The details Alfred still asks about, in the call's order: the ones this
    outcome asks for that are empty on the lead and that the rep hasn't said
    there's nothing for. The caller's empty_fields (the last response's
    still_empty) carries those earlier "nothing"s — but only for questions the
    lead's outcome already asked; ones this text's outcome newly brings in
    (a first outcome, sale → cpfu) are asked."""
    asked_before = _to_ask(lead.disposition)
    carried = context['empty_fields']
    return [f for f in _to_ask(writes.get('disposition') or lead.disposition)
            if f not in writes and not getattr(lead, f) and f not in declined
            and (carried is None or f in carried or f not in asked_before)]


def _ask(field, lead, writes):
    if field == 'follow_up_time':
        day = writes.get('follow_up_date') or lead.follow_up_date
        return ASK_FOLLOW_UP_TIME.format(day=f'{day:%A}, {day:%B} {day.day}') if day else 'And what time?'
    return QUESTIONS[field]


def _first_name(lead):
    return (lead.homeowner_name or '').split()[0] if (lead.homeowner_name or '').split() else 'the homeowner'


def _serialize(writes):
    out = {}
    for field, value in writes.items():
        if isinstance(value, date):
            out[field] = value.isoformat()
        elif isinstance(value, dt_time):
            out[field] = value.strftime('%H:%M')
        else:
            out[field] = value
    return out


# ── The endpoint ──────────────────────────────────────────────────────────

@api_key_required
def alfred_text(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST only.'}, status=405)
    tenant = request.api_tenant
    org = _ventana_org(tenant)
    if org is None or get_current_org_id() != org.id:
        return JsonResponse({'error': 'Alfred by text is only for Ventana; this key is not a Ventana key.'},
                            status=403)

    now = timezone.now()
    allowed, retry_after = _take_rate_slot(tenant, now)
    if not allowed:
        response = JsonResponse({'error': 'Too many texts on this key this hour.', 'retry_after': retry_after},
                                status=429)
        response['Retry-After'] = str(retry_after)
        return response

    try:
        body = _parse_body(request)
        rep = _find_rep(body['rep_id'], body['rep_phone'])
        lead = _find_lead(body['lead_id'], rep, now)
    except _Refusal as refusal:
        return JsonResponse({'error': refusal.error}, status=refusal.status)
    if not settings.OPENAI_API_KEY:
        return JsonResponse({'error': 'Alfred is not configured (no OpenAI key); nothing was saved.'}, status=503)

    context, text = body['context'], body['text']
    try:
        args = _complete(build_messages(rep, lead, text, context, now), text_tool())
    except Exception as e:
        logger.error(f'Alfred by text: model call failed for lead {lead.id}: {e}')
        return JsonResponse({'error': "Alfred couldn't read that just now; nothing was saved. Try again."},
                            status=502)

    today = now.astimezone(EASTERN).date()
    answer = read_answer(args, lead, today)
    writes = answer['writes']
    if answer['understood'] and not writes and not answer['move'] and not answer['question'] \
            and not answer['problems'] and not answer['declined']:
        answer['understood'] = False     # nothing in it: ask, don't guess

    if not answer['understood']:
        writes = {}
        still_empty = []
        next_question = answer['question'] or ASK_UNREADABLE.format(name=_first_name(lead))
    else:
        still_empty = _still_empty(lead, writes, answer['declined'], context)
        if answer['problems']:
            next_question = answer['problems'][0][1]
        elif answer['question']:
            next_question = answer['question']
        elif not (writes.get('disposition') or lead.disposition) and not answer['move']:
            # Details but no outcome yet: the decision tree's next question.
            sat = writes['sat'] if 'sat' in writes else lead.sat
            next_question = ASK_CREDIT if sat else ASK_OUTCOME
        elif still_empty:
            next_question = _ask(still_empty[0], lead, writes)
        else:
            next_question = None
    summary = writes.get('call_notes') or context['last_summary'] or lead.call_notes or ''

    stamp = now.astimezone(EASTERN)
    exchange = (f"[Text {stamp:%m/%d %I:%M %p}] {rep.name}: {text}\n"
                f"Alfred: {next_question or '(nothing more to ask)'}")
    updated = 0
    with transaction.atomic():
        if writes:
            # The call's write: these fields, and the transcript of the
            # debrief. A text debrief comes in pieces, so each exchange is
            # added to the transcript rather than replacing it.
            # Appended in the UPDATE itself, so two texts at once both keep theirs.
            update = dict(writes)
            update['call_transcript'] = Case(
                When(call_transcript='', then=Value(exchange)),
                default=Concat(F('call_transcript'), Value('\n\n' + exchange)),
                output_field=TextField())
            updated = Lead.objects.filter(id=lead.id).filter(Q(rep=rep) | Q(rep__isnull=True)).update(**update)
            if not updated:
                return JsonResponse({'error': 'That lead changed hands just now; nothing was saved.'}, status=409)
        VoiceCallLog.objects.create(
            rep=rep, caller_number=_phone_digits(rep.phone_number)[:20], channel='text',
            transcript=f"Lead #{lead.id} ({lead.homeowner_name or 'no name'})\n{exchange}",
            summary=summary,
        )
    if updated:
        logger.info(f'Alfred by text: lead {lead.id} set {sorted(writes)} for {rep.name}')
        # Like each write a call makes, every write fires the org's
        # disposition webhooks (debounced 60 s, so the receivers get the
        # latest details too) — once the lead has a disposition to report.
        fresh = Lead.objects.select_related('rep').filter(id=lead.id).first()
        if fresh and fresh.disposition:
            fire_webhooks('disposition_changed', fresh)

    return JsonResponse({
        'set': _serialize(writes),
        'summary': summary,
        'next_question': next_question,
        'appointment_move': answer['move'],
        'still_empty': still_empty,
        'understood': answer['understood'],
    })
