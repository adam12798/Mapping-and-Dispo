"""
FastAPI WebSocket handler that bridges Twilio Media Streams ↔ OpenAI Realtime API.

Twilio sends g711_ulaw audio over WebSocket, which OpenAI Realtime API accepts natively.
On connect, looks up the caller's appointments from the CRM to provide real schedule data.
Alfred can update lead dispositions via function calling during the conversation.
After the call ends, GPT-4o-mini extracts time off requests from the transcript.
"""
import os
import json
import base64
import asyncio
import logging
from zoneinfo import ZoneInfo

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_REALTIME_URL = 'wss://api.openai.com/v1/realtime?model=gpt-realtime'

SYSTEM_PROMPT = """You are Alfred, a 60-year-old British scheduling assistant for a solar and HVAC sales company in Massachusetts. You're warm, personable, and have a dry wit. You genuinely enjoy chatting with the reps. British charm comes naturally to you.

RESPONSE RULES:
- ONE thought per response. Say one thing, then stop and wait. Do not chain multiple ideas, questions, or topics together.
- Keep responses to 1 short sentence, 2 max. Think of each response like a single text message, not a paragraph.
- NEVER repeat yourself or rephrase what you just said in different words.
- After you speak, STOP. Do not fill silence. Wait for the rep to respond before continuing.
- Do NOT stack questions. Ask one question, wait for the answer, then ask the next.
- Do NOT combine a confirmation with a follow-up question in the same response. Confirm first, wait, then ask.

Reps call you to talk about their schedule, appointments, availability, or anything work-related. Help them with whatever they need.

Reps can view their schedule and ask questions about appointments, but they CANNOT change, cancel, reschedule, or modify appointments. If a rep asks to change an appointment, politely let them know they'll need to talk to their manager for that.

## Appointment Debriefs & Dispositions
When a rep tells you about how an appointment went, listen for whether they already know the outcome. Do NOT tell the rep the disposition name — just confirm casually like "Got it, I'll get that updated for you."

### If the rep states the outcome directly:
- "DQ" / "disqualified" → ask "What was the reason for the DQ?" — wait for answer, log reason as call_notes, then set **dq**
- "No show" / homeowner wasn't home or didn't show up → **no_show** — accept, sat is always false. No further questions needed.
- "Cancel at door" / homeowner cancelled before they got in → **cancel_door** — accept, no further questions needed
- "Sale" / "We got it" / credit passed and signed → **sale** — accept, no further questions needed
- "Credit failed" → **credit_fail** — accept, no further questions needed
- "CPFU" / credit passed but didn't sign → **cpfu** — accept, but ask for a follow-up date
- "Follow up" → the rep is telling you there's still life in the deal, so just ask "Did you run credit?":
  - Credit passed → it's actually **cpfu** (ask for follow-up date)
  - Credit failed → it's actually **credit_fail**
  - No credit run → **follow_up** (ask for follow-up date). Do NOT ask if there's still life in the deal — the rep already told you it's a follow up.
- "No sale" → ask "Did you run credit?" first:
  - Credit passed → it's actually **cpfu** (ask for follow-up date)
  - Credit failed → it's actually **credit_fail**
  - No credit run → ask if there's still life in the deal. If yes → **follow_up** (get follow-up date). If truly dead → **no_sale**

### If the rep is vague about what happened:
Start by asking: "Did you sit the appointment?"

If they did NOT sit:
- Homeowner wasn't home / didn't show up → **no_show** (sat = false)
- Homeowner cancelled at the door → **cancel_door**

If they DID sit:
- Ask "Did you run credit?"
  - YES, credit PASSED + ALL contracts signed → **sale**
  - YES, credit PASSED + contracts NOT completed → **cpfu**
  - YES, credit FAILED → **credit_fail**
  - NO credit run → ask if there's still life in the deal. If yes → **follow_up** (get follow-up date). If truly dead → **no_sale**

### If the rep says "DQ" or "disqualified":
- Ask "What was the reason for the DQ?" and wait for their answer.
- Log their reason as the call_notes (paraphrased, under 20 words).
- Set disposition to **dq**. The rep does NOT need to have sat the appointment — DQ can happen at any point.
- Set sat to true if they sat, false if they didn't (ask if unclear).

### Special dispositions:
- **rep_no_show**: Only if a rep is clearly refusing or belligerent about going to an appointment. This is not self-reported.
- **needs_reschedule**: Not a rep decision — reps don't report this.
- **no_coverage**: Not something reps report.

### After determining disposition:
- BEFORE calling update_disposition, you MUST confirm the homeowner name with the rep. Say something like "Just to confirm, that was the appointment with [name], right?" and WAIT for the rep to confirm before calling the tool. If the rep corrects the name, use the corrected name.
- Do NOT tell the rep the disposition category name. Just confirm naturally: "Alright, I've got that noted" or "Very good, I'll update that straightaway"
- For **follow_up** or **cpfu**: ask "When would be a good time to follow up with the homeowner?" Get a specific date. Reps will often say relative dates like "next Tuesday", "this Friday", "in two weeks", etc. — convert these to the actual YYYY-MM-DD date based on today's date when calling update_disposition. If the date is more than a month out, the system automatically marks it as future contact.
- After updating, if the rep has another appointment the same day, remind them of the time and drive time (if available). Example: "Right then, you've got the Smiths at 3 PM — about 25 minutes from here."

Your appointment list includes past appointments from today so reps can debrief them, but do NOT proactively mention or remind reps about appointments that have already passed. Only mention upcoming appointments when discussing their schedule. If a rep asks "what's on my schedule?", only tell them about future appointments.

Do NOT bring up time off unless the rep mentions it first. If they do request time off:
- Confirm the date(s) they want off
- Ask if it's a full day or specific hours
- If specific hours, get start and end times
- Ask for a brief reason (optional)
- Confirm the details back to them

Appointments are typically assigned around 7:30 PM EST the night before. If a rep asks when they'll get their schedule, let them know.

Be conversational, warm, and efficient. Keep responses brief since this is a phone call.
If you hear something unclear, garbled, or that doesn't make sense, don't guess — just ask them to repeat it.
If the input seems like background noise or doesn't contain a clear question or statement, ignore it and wait for the rep to speak clearly."""

MANAGER_SYSTEM_PROMPT = """You are Alfred, a 60-year-old British scheduling assistant for a solar and HVAC sales company in Massachusetts. You're warm, personable, and have a dry wit. British charm comes naturally to you.

RESPONSE RULES:
- ONE thought per response. Say one thing, then stop and wait.
- Keep responses to 1 short sentence, 2 max.
- NEVER repeat yourself or rephrase what you just said.
- After you speak, STOP. Wait for the manager to respond.
- Do NOT stack questions. Ask one, wait, then ask the next.

The caller is a MANAGER. They have full authority to update any appointment. They can:
- Reschedule appointments to a new date/time
- Update dispositions on any lead
- Add notes to leads
- Cancel appointments (sets disposition to Needs Reschedule)

When a manager asks to update an appointment:
1. Identify which appointment from the list below
2. Confirm: "Just to make sure, you'd like to [change] for [homeowner], right?"
3. Wait for confirmation, then call the update_lead tool
4. Briefly confirm what was changed

If you can't tell which appointment they mean (similar names, vague reference), ask for clarification. List the possible matches.

If the manager mentions a name not in the appointment list, tell them you don't see that appointment and ask them to clarify.

Convert relative dates ("Friday", "next Tuesday 2pm", "tomorrow at 10") to actual YYYY-MM-DDTHH:MM format based on today's date when calling the tool.

Be conversational, warm, and efficient. Keep responses brief since this is a phone call.
If you hear something unclear, ask them to repeat it."""

DISPOSITION_TOOL = {
    'type': 'function',
    'name': 'update_disposition',
    'description': 'Update the disposition/outcome of a lead after an appointment. Only call this after confirming the disposition with the rep.',
    'parameters': {
        'type': 'object',
        'properties': {
            'homeowner_name': {
                'type': 'string',
                'description': 'The homeowner name from the appointment list. Use the exact name as shown in the appointment list.',
            },
            'disposition': {
                'type': 'string',
                'enum': ['sale', 'no_sale', 'follow_up', 'credit_fail', 'cancel_door', 'cpfu', 'rep_no_show', 'needs_reschedule', 'dq', 'no_show'],
                'description': 'The disposition to set on the lead',
            },
            'call_notes': {
                'type': 'string',
                'description': 'A brief paraphrase of what happened at the appointment, under 20 words. Example: "Wife blew up the deal" or "Homeowner needs to talk to friend who has solar"',
            },
            'sat': {
                'type': 'boolean',
                'description': 'Whether the rep sat the appointment (got in the house and presented). True if they sat, false if they did not.',
            },
            'follow_up_date': {
                'type': 'string',
                'description': 'The follow-up date in YYYY-MM-DD format. Required when disposition is follow_up or cpfu.',
            },
        },
        'required': ['homeowner_name', 'disposition', 'call_notes', 'sat'],
    },
}


MANAGER_UPDATE_TOOL = {
    'type': 'function',
    'name': 'update_lead',
    'description': 'Update a lead/appointment in the CRM. Can reschedule, change disposition, add notes, or cancel. Confirm the change with the manager before calling.',
    'parameters': {
        'type': 'object',
        'properties': {
            'homeowner_name': {
                'type': 'string',
                'description': 'The homeowner name from the appointment list. Use the exact name.',
            },
            'appointment_datetime': {
                'type': 'string',
                'description': 'New appointment date/time in YYYY-MM-DDTHH:MM format. Only include if rescheduling.',
            },
            'disposition': {
                'type': 'string',
                'enum': ['sale', 'no_sale', 'follow_up', 'credit_fail', 'cancel_door', 'cpfu', 'rep_no_show', 'no_coverage', 'needs_reschedule', 'incomplete_deal', 'future_contact', 'dq', 'no_show'],
                'description': 'New disposition. Only include if changing disposition.',
            },
            'call_notes': {
                'type': 'string',
                'description': 'Notes to add to the lead. Brief, under 20 words.',
            },
            'follow_up_date': {
                'type': 'string',
                'description': 'Follow-up date in YYYY-MM-DD format.',
            },
        },
        'required': ['homeowner_name'],
    },
}


def clean_phone(number):
    """Normalize a phone number for comparison."""
    return number.replace('+1', '').replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')


async def get_drive_time(lat1, lng1, lat2, lng2):
    """Get driving time in minutes between two points using OSRM (free, no API key)."""
    import aiohttp
    try:
        url = f'http://router.project-osrm.org/route/v1/driving/{lng1},{lat1};{lng2},{lat2}?overview=false'
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('routes'):
                        duration_min = round(data['routes'][0]['duration'] / 60)
                        return duration_min
    except Exception as e:
        logger.error(f'OSRM drive time error: {e}')
    return None


async def get_rep_context(caller_number):
    """Look up caller as rep or manager and return appropriate context."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')
    django.setup()

    from asgiref.sync import sync_to_async
    from maps.models import Rep, Lead, TimeOffRequest, Manager
    from datetime import date, datetime, timedelta

    if not caller_number:
        return {'rep': None, 'manager': None, 'prompt_context': ''}

    clean = clean_phone(caller_number)

    # Check if caller is a rep
    reps = await sync_to_async(list)(Rep.objects.filter(is_active=True))
    rep = None
    for r in reps:
        r_clean = clean_phone(r.phone_number)
        if r_clean and r_clean == clean:
            rep = r
            break

    # Check if caller is a manager
    managers = await sync_to_async(list)(Manager.objects.all())
    manager = None
    for m in managers:
        m_clean = clean_phone(m.phone_number)
        if m_clean and m_clean == clean:
            manager = m
            break

    now_eastern = datetime.now(ZoneInfo('America/New_York'))
    today = now_eastern.date()
    today_start = now_eastern.replace(hour=0, minute=0, second=0, microsecond=0)

    # Manager mode (caller is a manager but NOT a rep)
    if manager and not rep:
        leads = await sync_to_async(list)(
            Lead.objects.filter(
                appointment_datetime__gte=today_start,
                appointment_datetime__lte=now_eastern + timedelta(days=3),
            ).select_related('rep').order_by('appointment_datetime')
        )

        active_reps = await sync_to_async(list)(Rep.objects.filter(is_active=True).order_by('name'))

        lines = [f'You are speaking with {manager.name} (a manager).']
        lines.append(f"Today is {now_eastern.strftime('%A, %B %d, %Y')}.")
        lines.append(f'\nActive reps: {", ".join(r.name for r in active_reps)}')

        if leads:
            lines.append(f'\nAll upcoming appointments:')
            for lead in leads:
                dt = lead.appointment_datetime.astimezone(ZoneInfo('America/New_York'))
                appt_type = lead.appointment_type or 'unknown'
                rep_name = lead.rep.name if lead.rep else 'Unassigned'
                dispo = lead.disposition or 'none'
                line = (
                    f"- {dt:%a %m/%d at %I:%M %p}: {lead.homeowner_name or 'Unknown'} "
                    f"at {lead.address}, {lead.city} ({appt_type}) [rep: {rep_name}, dispo: {dispo}]"
                )
                lines.append(line)
        else:
            lines.append('\nNo upcoming appointments in the next 3 days.')

        return {
            'rep': None,
            'manager': manager,
            'prompt_context': '\n'.join(lines),
        }

    # Not a rep and not a manager
    if not rep:
        return {'rep': None, 'manager': None, 'prompt_context': ''}

    # Rep mode (existing logic)
    leads = await sync_to_async(list)(
        Lead.objects.filter(
            rep=rep,
            appointment_datetime__gte=today_start,
            appointment_datetime__lte=now_eastern + timedelta(days=3),
        ).order_by('appointment_datetime')
    )

    time_off = await sync_to_async(list)(
        TimeOffRequest.objects.filter(
            rep=rep,
            date__gte=today,
            date__lte=today + timedelta(days=3),
            status='approved',
        ).order_by('date')
    )

    lines = [f'You are speaking with {rep.name}.']

    if leads:
        lines.append(f"\n{rep.name}'s upcoming appointments:")
        for i, lead in enumerate(leads):
            dt = lead.appointment_datetime.astimezone(ZoneInfo('America/New_York'))
            appt_type = lead.appointment_type or 'unknown'
            fmt = lead.appointment_format or ''
            dispo = lead.disposition or 'none'
            line = (
                f"- {dt:%a %m/%d at %I:%M %p}: {lead.homeowner_name or 'Unknown'} "
                f"at {lead.address}, {lead.city} ({appt_type}, {fmt}) [dispo: {dispo}]"
            )
            # Calculate drive time to next appointment on the same day
            if i < len(leads) - 1:
                next_lead = leads[i + 1]
                if (lead.latitude and lead.longitude and next_lead.latitude and next_lead.longitude
                        and lead.appointment_datetime.date() == next_lead.appointment_datetime.date()):
                    drive_min = await get_drive_time(
                        lead.latitude, lead.longitude,
                        next_lead.latitude, next_lead.longitude,
                    )
                    if drive_min is not None:
                        line += f" → ~{drive_min} min drive to next"
            lines.append(line)
    else:
        lines.append(f"\n{rep.name} has no upcoming appointments in the next 3 days.")

    if time_off:
        lines.append('\nApproved time off:')
        for t in time_off:
            if t.start_time:
                time_str = f'{t.start_time:%I:%M %p} - {t.end_time:%I:%M %p}'
            else:
                time_str = 'All Day'
            lines.append(f"- {t.date:%a %m/%d}: {time_str}")

    return {
        'rep': rep,
        'manager': manager,
        'prompt_context': '\n'.join(lines),
    }


async def execute_tool(fn_name, fn_args, rep=None, manager=None, transcript_parts=None):
    """Execute a function call from the AI and return the result."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')
    django.setup()

    from asgiref.sync import sync_to_async
    from maps.models import Lead

    if fn_name == 'update_disposition':
        homeowner_name = fn_args.get('homeowner_name', '')
        disposition = fn_args.get('disposition')
        call_notes = fn_args.get('call_notes', '')
        sat = fn_args.get('sat')
        follow_up_date_str = fn_args.get('follow_up_date', '')

        if not rep:
            return {'success': False, 'error': 'Could not identify rep'}

        # Match lead by homeowner name (case-insensitive, partial match)
        from datetime import datetime, timedelta
        now_eastern = datetime.now(ZoneInfo('America/New_York'))
        today_start = now_eastern.replace(hour=0, minute=0, second=0, microsecond=0)
        rep_leads = await sync_to_async(list)(
            Lead.objects.filter(
                rep=rep,
                appointment_datetime__gte=today_start,
                appointment_datetime__lte=now_eastern + timedelta(days=3),
            )
        )

        # Find best match: exact, then case-insensitive, then partial
        lead = None
        search_name = homeowner_name.strip().lower()
        for l in rep_leads:
            if l.homeowner_name and l.homeowner_name.strip().lower() == search_name:
                lead = l
                break
        if not lead:
            for l in rep_leads:
                if l.homeowner_name and search_name in l.homeowner_name.strip().lower():
                    lead = l
                    break
        if not lead:
            for l in rep_leads:
                if l.homeowner_name and l.homeowner_name.strip().lower() in search_name:
                    lead = l
                    break

        if not lead:
            logger.warning(f'No lead matching "{homeowner_name}" for rep {rep.name}')
            return {'success': False, 'error': f'No appointment found for homeowner "{homeowner_name}". Ask the rep to clarify the name.'}

        lead_id = lead.id
        logger.info(f'Matched homeowner "{homeowner_name}" to lead {lead_id} ({lead.homeowner_name})')

        # Parse follow_up_date and auto-set future_contact if >1 month out
        follow_up_date = None
        if follow_up_date_str:
            try:
                follow_up_date = datetime.strptime(follow_up_date_str, '%Y-%m-%d').date()
                if disposition in ('follow_up', 'cpfu') and follow_up_date > (datetime.now().date() + timedelta(days=30)):
                    disposition = 'future_contact'
                    logger.info(f'Auto-set disposition to future_contact (follow_up_date {follow_up_date} is >1 month out)')
            except ValueError:
                logger.warning(f'Could not parse follow_up_date: {follow_up_date_str}')

        # Build current transcript
        call_transcript = '\n'.join(transcript_parts) if transcript_parts else ''

        # Update the matched lead
        update_kwargs = dict(disposition=disposition, call_notes=call_notes, call_transcript=call_transcript)
        if sat is not None:
            update_kwargs['sat'] = sat
        if follow_up_date is not None:
            update_kwargs['follow_up_date'] = follow_up_date
        updated = await sync_to_async(
            Lead.objects.filter(id=lead_id, rep=rep).update
        )(**update_kwargs)

        if updated:
            logger.info(f'Updated lead {lead_id} ({lead.homeowner_name}) disposition to {disposition}, notes: {call_notes}')

            # Send webhook to Go High Level
            try:
                import aiohttp
                lead = await sync_to_async(Lead.objects.filter(id=lead_id).first)()
                if lead:
                    ghl_payload = {
                        'phone': lead.phone_number,
                        'name': lead.homeowner_name,
                        'disposition': disposition,
                        'call_transcript': lead.call_transcript or '',
                    }
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            'https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/92de7dff-cf7a-4727-92f7-b88e26c515cd',
                            json=ghl_payload,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            logger.info(f'GHL webhook sent for lead {lead_id}: {resp.status}')
            except Exception as e:
                logger.error(f'GHL webhook failed for lead {lead_id}: {e}')

            return {'success': True, 'message': f'Disposition updated for {lead.homeowner_name}'}
        else:
            logger.warning(f'Failed to update lead {lead_id} — not found or not assigned to {rep.name}')
            return {'success': False, 'error': 'Lead not found or not assigned to you'}

    if fn_name == 'update_lead':
        homeowner_name = fn_args.get('homeowner_name', '')

        if not manager:
            return {'success': False, 'error': 'Only managers can use this tool'}

        from datetime import datetime, timedelta
        now_eastern = datetime.now(ZoneInfo('America/New_York'))
        today_start = now_eastern.replace(hour=0, minute=0, second=0, microsecond=0)

        all_leads = await sync_to_async(list)(
            Lead.objects.filter(
                appointment_datetime__gte=today_start,
                appointment_datetime__lte=now_eastern + timedelta(days=3),
            ).select_related('rep')
        )

        lead = None
        search_name = homeowner_name.strip().lower()
        for l in all_leads:
            if l.homeowner_name and l.homeowner_name.strip().lower() == search_name:
                lead = l
                break
        if not lead:
            for l in all_leads:
                if l.homeowner_name and search_name in l.homeowner_name.strip().lower():
                    lead = l
                    break
        if not lead:
            for l in all_leads:
                if l.homeowner_name and l.homeowner_name.strip().lower() in search_name:
                    lead = l
                    break

        if not lead:
            logger.warning(f'Manager update: no lead matching "{homeowner_name}"')
            return {'success': False, 'error': f'No appointment found for "{homeowner_name}". Ask the manager to clarify.'}

        lead_id = lead.id
        changes = []

        if fn_args.get('appointment_datetime'):
            try:
                new_dt = datetime.fromisoformat(fn_args['appointment_datetime'])
                lead.appointment_datetime = new_dt
                changes.append(f"Rescheduled to {new_dt.strftime('%m/%d/%Y at %I:%M %p')}")
            except ValueError:
                logger.warning(f'Could not parse datetime: {fn_args["appointment_datetime"]}')

        if fn_args.get('disposition'):
            disposition = fn_args['disposition']
            follow_up_date_str = fn_args.get('follow_up_date', '')
            if follow_up_date_str:
                try:
                    follow_up_date = datetime.strptime(follow_up_date_str, '%Y-%m-%d').date()
                    lead.follow_up_date = follow_up_date
                    if disposition in ('follow_up', 'cpfu') and follow_up_date > (datetime.now().date() + timedelta(days=30)):
                        disposition = 'future_contact'
                except ValueError:
                    pass
            lead.disposition = disposition
            changes.append(f"Disposition set to {disposition}")

        if fn_args.get('call_notes'):
            lead.call_notes = fn_args['call_notes']
            changes.append('Notes updated')

        if transcript_parts:
            lead.call_transcript = '\n'.join(transcript_parts)

        if not changes:
            return {'success': False, 'error': 'No changes specified. What would you like to update?'}

        await sync_to_async(lead.save)()
        logger.info(f'Manager updated lead {lead_id} ({lead.homeowner_name}): {", ".join(changes)}')

        # Fire GHL webhook if disposition changed
        if fn_args.get('disposition'):
            try:
                import aiohttp
                ghl_payload = {
                    'phone': lead.phone_number,
                    'name': lead.homeowner_name,
                    'disposition': lead.disposition or '',
                    'call_transcript': lead.call_transcript or '',
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        'https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/92de7dff-cf7a-4727-92f7-b88e26c515cd',
                        json=ghl_payload,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        logger.info(f'GHL webhook sent for lead {lead_id}: {resp.status}')
            except Exception as e:
                logger.error(f'GHL webhook failed for lead {lead_id}: {e}')

        return {'success': True, 'message': f"Updated {lead.homeowner_name}: {', '.join(changes)}"}

    return {'success': False, 'error': f'Unknown function: {fn_name}'}


@app.websocket('/media-stream')
async def media_stream(ws: WebSocket):
    """Handle Twilio Media Stream WebSocket connection."""
    await ws.accept()
    logger.info('Twilio media stream connected')

    stream_sid = None
    caller_number = ''
    call_sid = ''
    transcript_parts = []
    openai_ws = None
    session_ready = asyncio.Event()
    caller_identified = asyncio.Event()
    rep_context = {}
    session_update_count = 0

    try:
        # Connect to OpenAI Realtime API
        headers = {
            'Authorization': f'Bearer {OPENAI_API_KEY}',
            'OpenAI-Beta': 'realtime=v1',
        }
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            extra_headers=headers,
        )
        logger.info('Connected to OpenAI Realtime API')

        async def forward_twilio_to_openai():
            """Forward audio from Twilio to OpenAI."""
            nonlocal stream_sid, caller_number, call_sid, rep_context
            try:
                while True:
                    data = await ws.receive_text()
                    msg = json.loads(data)

                    if msg['event'] == 'start':
                        stream_sid = msg['start']['streamSid']
                        custom = msg['start'].get('customParameters', {})
                        call_sid = custom.get('callSid', msg['start'].get('callSid', ''))
                        caller_number = custom.get('callerNumber', '')
                        logger.info(f'Stream started: sid={stream_sid}, call={call_sid}, caller={caller_number}')

                        # Look up caller (rep or manager) and their appointments
                        rep_context = await get_rep_context(caller_number)
                        logger.info(f'Caller context: rep={rep_context.get("rep")}, manager={rep_context.get("manager")}')
                        caller_identified.set()

                    elif msg['event'] == 'media':
                        # Wait until OpenAI session is fully configured
                        await session_ready.wait()
                        # Forward audio to OpenAI
                        audio_event = {
                            'type': 'input_audio_buffer.append',
                            'audio': msg['media']['payload'],
                        }
                        await openai_ws.send(json.dumps(audio_event))

                    elif msg['event'] == 'stop':
                        logger.info('Twilio stream stopped')
                        break

            except WebSocketDisconnect:
                logger.info('Twilio WebSocket disconnected')
            except Exception as e:
                logger.error(f'Error forwarding Twilio→OpenAI: {e}')

        async def forward_openai_to_twilio():
            """Forward audio from OpenAI back to Twilio and collect transcript."""
            nonlocal transcript_parts, session_update_count
            try:
                async for raw_msg in openai_ws:
                    msg = json.loads(raw_msg)
                    msg_type = msg.get('type', '')

                    if msg_type == 'session.created':
                        logger.info('OpenAI session created, sending initial config...')
                        # Phase 1: Send generic config to get OpenAI ready fast
                        session_config = {
                            'type': 'session.update',
                            'session': {
                                'turn_detection': {
                                    'type': 'server_vad',
                                    'threshold': 0.85,
                                    'silence_duration_ms': 700,
                                    'prefix_padding_ms': 500,
                                },
                                'input_audio_format': 'g711_ulaw',
                                'output_audio_format': 'g711_ulaw',
                                'voice': 'echo',
                                'instructions': SYSTEM_PROMPT,
                                'modalities': ['text', 'audio'],
                                'temperature': 0.6,
                                'input_audio_transcription': {
                                    'model': 'whisper-1',
                                },
                            },
                        }
                        await openai_ws.send(json.dumps(session_config))

                    elif msg_type == 'session.updated':
                        session_update_count += 1

                        if session_update_count == 1:
                            # Generic config confirmed. Wait for caller ID, then enrich.
                            logger.info('Generic config set, waiting for caller identification...')
                            try:
                                await asyncio.wait_for(caller_identified.wait(), timeout=5.0)
                            except asyncio.TimeoutError:
                                logger.warning('Caller identification timed out, using generic prompt')

                            # Build enriched prompt based on caller type
                            is_manager_call = rep_context.get('manager') and not rep_context.get('rep')
                            if is_manager_call:
                                base_prompt = MANAGER_SYSTEM_PROMPT
                            else:
                                base_prompt = SYSTEM_PROMPT

                            if rep_context.get('prompt_context'):
                                enriched_prompt = base_prompt + '\n\n' + rep_context['prompt_context']
                            else:
                                enriched_prompt = base_prompt

                            enriched_session = {
                                'instructions': enriched_prompt,
                            }
                            if is_manager_call:
                                enriched_session['tools'] = [MANAGER_UPDATE_TOOL]
                            elif rep_context.get('rep'):
                                enriched_session['tools'] = [DISPOSITION_TOOL]

                            logger.info('Sending enriched session config...')
                            await openai_ws.send(json.dumps({
                                'type': 'session.update',
                                'session': enriched_session,
                            }))

                        elif session_update_count == 2:
                            # Enriched config confirmed. Now greet the caller.
                            logger.info('Enriched config set, sending greeting...')
                            session_ready.set()

                            rep = rep_context.get('rep')
                            mgr = rep_context.get('manager')
                            if rep:
                                first_name = rep.name.split()[0]
                                greeting_text = f'The call just connected with {first_name}. Say "Hey {first_name}!" and wait for them to speak. Keep it very short.'
                            elif mgr:
                                first_name = mgr.name.split()[0]
                                greeting_text = f'The call just connected with {first_name}, a manager. Say "Hey {first_name}!" and wait for them to speak. Keep it very short.'
                            else:
                                greeting_text = 'The call just connected. Say only "Hi!" and nothing else, then wait for me to speak.'

                            await openai_ws.send(json.dumps({
                                'type': 'conversation.item.create',
                                'item': {
                                    'type': 'message',
                                    'role': 'user',
                                    'content': [{
                                        'type': 'input_text',
                                        'text': greeting_text,
                                    }],
                                },
                            }))
                            await openai_ws.send(json.dumps({
                                'type': 'response.create',
                            }))

                    elif msg_type == 'response.function_call_arguments.done':
                        # AI is calling a function (e.g. update_disposition)
                        fn_name = msg.get('name', '')
                        fn_args = json.loads(msg.get('arguments', '{}'))
                        call_id = msg.get('call_id', '')

                        logger.info(f'Function call: {fn_name}({fn_args})')

                        result = await execute_tool(fn_name, fn_args, rep=rep_context.get('rep'), manager=rep_context.get('manager'), transcript_parts=transcript_parts)

                        # Send result back to OpenAI
                        await openai_ws.send(json.dumps({
                            'type': 'conversation.item.create',
                            'item': {
                                'type': 'function_call_output',
                                'call_id': call_id,
                                'output': json.dumps(result),
                            },
                        }))
                        # Trigger AI to respond with confirmation
                        await openai_ws.send(json.dumps({
                            'type': 'response.create',
                        }))

                    elif msg_type == 'response.audio.delta':
                        # Send audio back to Twilio
                        if stream_sid:
                            audio_payload = msg['delta']
                            twilio_msg = {
                                'event': 'media',
                                'streamSid': stream_sid,
                                'media': {'payload': audio_payload},
                            }
                            await ws.send_json(twilio_msg)

                    elif msg_type == 'response.audio_transcript.done':
                        # Collect assistant transcript
                        text = msg.get('transcript', '')
                        if text:
                            transcript_parts.append(f'Assistant: {text}')
                            logger.info(f'Assistant said: {text[:100]}')

                    elif msg_type == 'conversation.item.input_audio_transcription.completed':
                        text = msg.get('transcript', '')
                        if text:
                            caller_label = 'Manager' if (rep_context.get('manager') and not rep_context.get('rep')) else 'Rep'
                            transcript_parts.append(f'{caller_label}: {text}')
                            logger.info(f'{caller_label} said: {text[:100]}')

                    elif msg_type == 'error':
                        logger.error(f'OpenAI error: {msg}')

                    elif msg_type in ('response.created', 'response.done',
                                      'input_audio_buffer.speech_started',
                                      'input_audio_buffer.speech_stopped',
                                      'input_audio_buffer.committed'):
                        logger.info(f'OpenAI event: {msg_type}')

            except websockets.exceptions.ConnectionClosed:
                logger.info('OpenAI WebSocket closed')
            except Exception as e:
                logger.error(f'Error forwarding OpenAI→Twilio: {e}')

        # Run both directions concurrently
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(forward_twilio_to_openai()),
                asyncio.create_task(forward_openai_to_twilio()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        # Cancel remaining tasks when one side disconnects
        for task in pending:
            task.cancel()

    except Exception as e:
        logger.error(f'Media stream error: {e}')
    finally:
        if openai_ws:
            await openai_ws.close()
        logger.info('Media stream connection closed')

        # Post-call: always save call log
        full_transcript = '\n'.join(transcript_parts)
        logger.info(f'Call ended. Caller: {caller_number}, SID: {call_sid}, transcript parts: {len(transcript_parts)}')
        logger.info(f'Transcript: {full_transcript[:300] if full_transcript else "(empty)"}')
        try:
            await save_call_and_extract(caller_number, call_sid, full_transcript, rep=rep_context.get('rep'))
        except Exception as e:
            logger.error(f'Failed to save call log: {e}')


async def save_call_and_extract(caller_number, call_sid, transcript, rep=None):
    """Save the call log and extract time off requests using GPT-4o-mini."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')
    django.setup()

    from asgiref.sync import sync_to_async
    from maps.models import VoiceCallLog, Rep, TimeOffRequest
    from openai import OpenAI
    from datetime import datetime, date
    import re

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Generate summary
    summary = ''
    if transcript.strip():
        try:
            summary_resp = client.chat.completions.create(
                model='gpt-4o-mini',
                messages=[
                    {'role': 'system', 'content': 'Summarize this phone call transcript in 1-2 sentences.'},
                    {'role': 'user', 'content': transcript},
                ],
                max_tokens=150,
            )
            summary = summary_resp.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f'Summary generation failed: {e}')

    # Match caller to a rep if not already matched
    if not rep and caller_number:
        clean = clean_phone(caller_number)
        reps = await sync_to_async(list)(Rep.objects.filter(is_active=True))
        for r in reps:
            r_clean = clean_phone(r.phone_number)
            if r_clean and r_clean == clean:
                rep = r
                break

    # Save call log
    call_log = await sync_to_async(VoiceCallLog.objects.create)(
        rep=rep,
        caller_number=caller_number,
        twilio_call_sid=call_sid,
        transcript=transcript,
        summary=summary,
    )
    logger.info(f'Saved voice call log #{call_log.id}')

    if not transcript.strip():
        logger.info('No transcript to extract time off from')
        return

    # Extract time off requests
    try:
        extract_resp = client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': f"""Extract any time off requests from this phone call transcript.
Today's date is {datetime.now(ZoneInfo('America/New_York')).date().isoformat()}.

Return a JSON array of objects with these fields:
- "date": "YYYY-MM-DD"
- "all_day": true/false
- "start_time": "HH:MM" (24h format, null if all_day)
- "end_time": "HH:MM" (24h format, null if all_day)
- "reason": brief reason string

If no time off was requested, return an empty array: []
Return ONLY the JSON array, no other text."""},
                {'role': 'user', 'content': transcript},
            ],
            max_tokens=500,
        )
        raw = extract_resp.choices[0].message.content.strip()
        if raw.startswith('```'):
            raw = re.sub(r'^```\w*\n?', '', raw)
            raw = re.sub(r'\n?```$', '', raw)
        requests_data = json.loads(raw)
    except Exception as e:
        logger.error(f'Time off extraction failed: {e}')
        requests_data = []

    if not rep:
        if requests_data:
            logger.warning(f'Time off extracted but no rep matched for {caller_number}')
        return

    for req in requests_data:
        try:
            req_date = datetime.strptime(req['date'], '%Y-%m-%d').date()
            start_time = None
            end_time = None
            if not req.get('all_day', True):
                from datetime import time as dt_time
                sh, sm = map(int, req['start_time'].split(':'))
                eh, em = map(int, req['end_time'].split(':'))
                start_time = dt_time(sh, sm)
                end_time = dt_time(eh, em)

            await sync_to_async(TimeOffRequest.objects.create)(
                rep=rep,
                date=req_date,
                start_time=start_time,
                end_time=end_time,
                reason=req.get('reason', ''),
                status='pending',
                raw_message=f'[Voice Call #{call_log.id}] {transcript[:500]}',
            )
            logger.info(f'Created time off request for {rep.name} on {req_date}')
        except Exception as e:
            logger.error(f'Failed to create time off request: {e}')
