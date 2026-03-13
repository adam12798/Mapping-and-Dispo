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

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_REALTIME_URL = 'wss://api.openai.com/v1/realtime?model=gpt-realtime'
GHL_WEBHOOK_URL = os.environ.get(
    'GHL_WEBHOOK_URL',
    'https://services.leadconnectorhq.com/hooks/YKmi8a53KJWDRbv2ZnFB/webhook-trigger/92de7dff-cf7a-4727-92f7-b88e26c515cd',
)

SYSTEM_PROMPT = """You are Alfred, a 60-year-old British scheduling assistant for a solar and HVAC sales company in Massachusetts. You're warm, personable, and have a dry wit. You genuinely enjoy chatting with the reps. British charm comes naturally to you.

RESPONSE RULES:
- Keep responses to 1-2 short sentences. Be bright and engaging but concise.
- NEVER repeat yourself or rephrase what you just said in different words.
- After you speak, wait for the rep to respond. Do not fill silence.

Reps call you to talk about their schedule, appointments, availability, or anything work-related. Help them with whatever they need.

Reps can view their schedule and ask questions about appointments, but they CANNOT change, cancel, reschedule, or modify appointments. If a rep asks to change an appointment, politely let them know they'll need to talk to their manager for that.

## Appointment Debriefs & Dispositions
When a rep tells you about how an appointment went, ask natural follow-up questions to figure out what happened. Do NOT tell the rep the disposition name — just confirm casually like "Got it, I'll get that updated for you."

Start by asking: "Did you sit the appointment?" This is the key first question.

If they did NOT sit:
- They got caught up / couldn't make it → **needs_reschedule**
- They refused to go → **rep_no_show** (rare, only if clearly refusing)
- Homeowner cancelled at the door before they got in → **cancel_door**

If they DID sit (got in the house and presented):
- Ask "Did you run credit?"
  - YES, credit PASSED + ALL contracts signed → **sale**
  - YES, credit PASSED + contracts NOT completed → **cpfu**
  - YES, credit FAILED → **credit_fail**
  - NO credit run:
    - Still sounds like life in the deal → **follow_up**
    - Dead deal, no interest → **no_sale**

CRITICAL RULES:
- ALWAYS ask "Did you sit?" first, then "Did you run credit?" — these are the two most important questions
- Be skeptical of reps claiming a sale. A sale means credit passed AND all contracts were signed. If contracts weren't completed, it's a CPFU, not a sale.
- If credit was run and passed, it is ALWAYS cpfu (never follow_up) unless all contracts were signed (then it's a sale)
- If credit was run and failed, it is ALWAYS credit_fail
- Do NOT tell the rep the disposition category name. Just confirm naturally: "Alright, I've got that noted" or "Very good, I'll update that straightaway"
- Do NOT use the no_coverage disposition — that is not something reps report
- After determining follow_up or cpfu, ask "When would be a good time to follow up with the homeowner?" Get a specific date. If the date is more than a month out, the system will automatically mark it as a future contact instead of a regular follow up.
- After updating a disposition, if the rep has another appointment the same day, remind them of the time and drive time (if available). Example: "Right then, you've got the Smiths at 3 PM — about 25 minutes from here."

Do NOT bring up time off unless the rep mentions it first. If they do request time off:
- Confirm the date(s) they want off
- Ask if it's a full day or specific hours
- If specific hours, get start and end times
- Ask for a brief reason (optional)
- Confirm the details back to them

Be conversational, warm, and efficient. Keep responses brief since this is a phone call.
If you hear something unclear, garbled, or that doesn't make sense, don't guess — just ask them to repeat it.
If the input seems like background noise or doesn't contain a clear question or statement, ignore it and wait for the rep to speak clearly."""

DISPOSITION_TOOL = {
    'type': 'function',
    'name': 'update_disposition',
    'description': 'Update the disposition/outcome of a lead after an appointment. Only call this after confirming the disposition with the rep.',
    'parameters': {
        'type': 'object',
        'properties': {
            'lead_id': {
                'type': 'integer',
                'description': 'The ID of the lead to update',
            },
            'disposition': {
                'type': 'string',
                'enum': ['sale', 'no_sale', 'follow_up', 'credit_fail', 'cancel_door', 'cpfu', 'rep_no_show', 'needs_reschedule'],
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
        'required': ['lead_id', 'disposition', 'call_notes', 'sat'],
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
    """Look up rep and their upcoming appointments by phone number."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')
    django.setup()

    from asgiref.sync import sync_to_async
    from maps.models import Rep, Lead, TimeOffRequest
    from datetime import date, timedelta

    if not caller_number:
        return {'rep': None, 'prompt_context': ''}

    clean = clean_phone(caller_number)
    reps = await sync_to_async(list)(Rep.objects.filter(is_active=True))
    rep = None
    for r in reps:
        r_clean = clean_phone(r.phone_number)
        if r_clean and r_clean == clean:
            rep = r
            break

    if not rep:
        return {'rep': None, 'prompt_context': ''}

    today = date.today()
    # Get appointments for next 3 days
    leads = await sync_to_async(list)(
        Lead.objects.filter(
            rep=rep,
            appointment_datetime__date__gte=today,
            appointment_datetime__date__lte=today + timedelta(days=3),
        ).order_by('appointment_datetime')
    )

    # Get approved time off
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
            dt = lead.appointment_datetime
            appt_type = lead.appointment_type or 'unknown'
            fmt = lead.appointment_format or ''
            dispo = lead.disposition or 'none'
            line = (
                f"- [ID: {lead.id}] {dt:%a %m/%d at %I:%M %p}: {lead.homeowner_name or 'Unknown'} "
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
        'prompt_context': '\n'.join(lines),
    }


async def execute_tool(fn_name, fn_args, rep, transcript_parts=None):
    """Execute a function call from the AI and return the result."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')
    django.setup()

    from asgiref.sync import sync_to_async
    from maps.models import Lead

    if fn_name == 'update_disposition':
        lead_id = fn_args.get('lead_id')
        disposition = fn_args.get('disposition')
        call_notes = fn_args.get('call_notes', '')
        sat = fn_args.get('sat')
        follow_up_date_str = fn_args.get('follow_up_date', '')

        if not rep:
            return {'success': False, 'error': 'Could not identify rep'}

        # Parse follow_up_date and auto-set future_contact if >1 month out
        follow_up_date = None
        if follow_up_date_str:
            try:
                from datetime import datetime, timedelta
                follow_up_date = datetime.strptime(follow_up_date_str, '%Y-%m-%d').date()
                if disposition in ('follow_up', 'cpfu') and follow_up_date > (datetime.now().date() + timedelta(days=30)):
                    disposition = 'future_contact'
                    logger.info(f'Auto-set disposition to future_contact (follow_up_date {follow_up_date} is >1 month out)')
            except ValueError:
                logger.warning(f'Could not parse follow_up_date: {follow_up_date_str}')

        # Build current transcript
        call_transcript = '\n'.join(transcript_parts) if transcript_parts else ''

        # Only allow updating leads assigned to this rep
        update_kwargs = dict(disposition=disposition, call_notes=call_notes, call_transcript=call_transcript)
        if sat is not None:
            update_kwargs['sat'] = sat
        if follow_up_date is not None:
            update_kwargs['follow_up_date'] = follow_up_date
        updated = await sync_to_async(
            Lead.objects.filter(id=lead_id, rep=rep).update
        )(**update_kwargs)

        if updated:
            logger.info(f'Updated lead {lead_id} disposition to {disposition}, notes: {call_notes}')

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
                            GHL_WEBHOOK_URL,
                            json=ghl_payload,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as resp:
                            logger.info(f'GHL webhook sent for lead {lead_id}: {resp.status}')
            except Exception as e:
                logger.error(f'GHL webhook failed for lead {lead_id}: {e}')

            return {'success': True, 'message': f'Disposition updated to {disposition}'}
        else:
            logger.warning(f'Failed to update lead {lead_id} — not found or not assigned to {rep.name}')
            return {'success': False, 'error': 'Lead not found or not assigned to you'}

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

                        # Look up rep and their appointments
                        rep_context = await get_rep_context(caller_number)
                        logger.info(f'Rep context: {rep_context.get("rep")}')
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

                            # Build enriched prompt with real appointment data
                            if rep_context.get('prompt_context'):
                                enriched_prompt = SYSTEM_PROMPT + '\n\n' + rep_context['prompt_context']
                            else:
                                enriched_prompt = SYSTEM_PROMPT

                            # Include disposition tool if we have a matched rep
                            enriched_session = {
                                'instructions': enriched_prompt,
                            }
                            if rep_context.get('rep'):
                                enriched_session['tools'] = [DISPOSITION_TOOL]

                            logger.info('Sending enriched session config...')
                            await openai_ws.send(json.dumps({
                                'type': 'session.update',
                                'session': enriched_session,
                            }))

                        elif session_update_count == 2:
                            # Enriched config confirmed. Now greet the rep.
                            logger.info('Enriched config set, sending greeting...')
                            session_ready.set()

                            rep = rep_context.get('rep')
                            if rep:
                                first_name = rep.name.split()[0]
                                greeting_text = f'The call just connected with {first_name}. Say "Hey {first_name}!" and wait for them to speak. Keep it very short.'
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

                        result = await execute_tool(fn_name, fn_args, rep_context.get('rep'), transcript_parts)

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
                        # Collect user (rep) transcript
                        text = msg.get('transcript', '')
                        if text:
                            transcript_parts.append(f'Rep: {text}')
                            logger.info(f'Rep said: {text[:100]}')

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
Today's date is {date.today().isoformat()}.

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
