"""
FastAPI WebSocket handler that bridges Twilio Media Streams ↔ OpenAI Realtime API.

Twilio sends g711_ulaw audio over WebSocket, which OpenAI Realtime API accepts natively.
After the call ends, GPT-4o-mini extracts time off requests from the transcript.
"""
import os
import json
import base64
import asyncio
import logging

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

app = FastAPI()

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_REALTIME_URL = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17'

SYSTEM_PROMPT = """You are a friendly scheduling assistant for a solar and HVAC sales company in Massachusetts.

Reps call you to:
- Request time off (full day or specific hours)
- Check their schedule
- Ask general questions

When a rep requests time off:
- Confirm the date(s) they want off
- Ask if it's a full day or specific hours
- If specific hours, get start and end times
- Ask for a brief reason (optional)
- Confirm the details back to them

Be conversational, warm, and efficient. Keep responses brief since this is a phone call.
If you don't understand something, ask them to repeat it.
Always confirm time off details before ending the call."""


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

    try:
        # Connect to OpenAI Realtime API
        headers = {
            'Authorization': f'Bearer {OPENAI_API_KEY}',
            'OpenAI-Beta': 'realtime=v1',
        }
        openai_ws = await websockets.connect(
            OPENAI_REALTIME_URL,
            additional_headers=headers,
        )
        logger.info('Connected to OpenAI Realtime API')

        # Configure the session
        session_config = {
            'type': 'session.update',
            'session': {
                'turn_detection': {'type': 'server_vad'},
                'input_audio_format': 'g711_ulaw',
                'output_audio_format': 'g711_ulaw',
                'voice': 'alloy',
                'instructions': SYSTEM_PROMPT,
                'modalities': ['text', 'audio'],
                'temperature': 0.8,
            },
        }
        await openai_ws.send(json.dumps(session_config))

        async def forward_twilio_to_openai():
            """Forward audio from Twilio to OpenAI."""
            nonlocal stream_sid, caller_number, call_sid
            try:
                while True:
                    data = await ws.receive_text()
                    msg = json.loads(data)

                    if msg['event'] == 'start':
                        stream_sid = msg['start']['streamSid']
                        call_sid = msg['start'].get('callSid', '')
                        custom = msg['start'].get('customParameters', {})
                        caller_number = custom.get('callerNumber', '')
                        logger.info(f'Stream started: {stream_sid}, caller: {caller_number}')

                    elif msg['event'] == 'media':
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
            nonlocal transcript_parts
            try:
                async for raw_msg in openai_ws:
                    msg = json.loads(raw_msg)
                    msg_type = msg.get('type', '')

                    if msg_type == 'response.audio.delta':
                        # Send audio back to Twilio
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

                    elif msg_type == 'conversation.item.input_audio_transcription.completed':
                        # Collect user (rep) transcript
                        text = msg.get('transcript', '')
                        if text:
                            transcript_parts.append(f'Rep: {text}')

                    elif msg_type == 'error':
                        logger.error(f'OpenAI error: {msg}')

            except websockets.exceptions.ConnectionClosed:
                logger.info('OpenAI WebSocket closed')
            except Exception as e:
                logger.error(f'Error forwarding OpenAI→Twilio: {e}')

        # Run both directions concurrently
        await asyncio.gather(
            forward_twilio_to_openai(),
            forward_openai_to_twilio(),
        )

    except Exception as e:
        logger.error(f'Media stream error: {e}')
    finally:
        if openai_ws:
            await openai_ws.close()
        logger.info('Media stream connection closed')

        # Post-call: save transcript and extract time off requests
        full_transcript = '\n'.join(transcript_parts)
        if full_transcript.strip():
            await save_call_and_extract(caller_number, call_sid, full_transcript)


async def save_call_and_extract(caller_number, call_sid, transcript):
    """Save the call log and extract time off requests using GPT-4o-mini."""
    import django
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')
    django.setup()

    from maps.models import VoiceCallLog, Rep, TimeOffRequest
    from openai import OpenAI
    from datetime import datetime, date
    import re

    client = OpenAI(api_key=OPENAI_API_KEY)

    # Generate summary
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
        summary = ''

    # Match caller to a rep
    rep = None
    if caller_number:
        # Normalize number for lookup
        clean = caller_number.replace('+1', '').replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        reps = Rep.objects.filter(is_active=True)
        for r in reps:
            r_clean = r.phone_number.replace('+1', '').replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
            if r_clean and r_clean == clean:
                rep = r
                break

    # Save call log
    call_log = VoiceCallLog.objects.create(
        rep=rep,
        caller_number=caller_number,
        twilio_call_sid=call_sid,
        transcript=transcript,
        summary=summary,
    )
    logger.info(f'Saved voice call log #{call_log.id}')

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
        # Strip markdown code fences if present
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

            TimeOffRequest.objects.create(
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
