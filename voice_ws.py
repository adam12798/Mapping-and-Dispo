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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
OPENAI_REALTIME_URL = 'wss://api.openai.com/v1/realtime?model=gpt-realtime'

SYSTEM_PROMPT = """You are a friendly scheduling assistant for a solar and HVAC sales company in Massachusetts.

Reps call you to talk about their schedule, appointments, availability, or anything work-related. Help them with whatever they need.

Do NOT bring up time off unless the rep mentions it first. If they do request time off:
- Confirm the date(s) they want off
- Ask if it's a full day or specific hours
- If specific hours, get start and end times
- Ask for a brief reason (optional)
- Confirm the details back to them

Be conversational, warm, and efficient. Keep responses brief since this is a phone call.
If you don't understand something, ask them to repeat it."""


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
            nonlocal stream_sid, caller_number, call_sid
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
                        logger.info(f'Custom params: {custom}')

                    elif msg['event'] == 'media':
                        # Wait until OpenAI session is configured
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
            nonlocal transcript_parts
            try:
                async for raw_msg in openai_ws:
                    msg = json.loads(raw_msg)
                    msg_type = msg.get('type', '')

                    if msg_type == 'session.created':
                        logger.info('OpenAI session created, sending config...')
                        # Now configure the session
                        session_config = {
                            'type': 'session.update',
                            'session': {
                                'turn_detection': {
                                    'type': 'server_vad',
                                    'threshold': 0.7,
                                    'silence_duration_ms': 700,
                                },
                                'input_audio_format': 'g711_ulaw',
                                'output_audio_format': 'g711_ulaw',
                                'voice': 'echo',
                                'instructions': SYSTEM_PROMPT,
                                'modalities': ['text', 'audio'],
                                'temperature': 0.8,
                                'input_audio_transcription': {
                                    'model': 'whisper-1',
                                },
                            },
                        }
                        await openai_ws.send(json.dumps(session_config))

                    elif msg_type == 'session.updated':
                        logger.info('OpenAI session configured, sending initial greeting...')
                        session_ready.set()
                        # Trigger OpenAI to speak first with a simple greeting
                        await openai_ws.send(json.dumps({
                            'type': 'conversation.item.create',
                            'item': {
                                'type': 'message',
                                'role': 'assistant',
                                'content': [{
                                    'type': 'input_text',
                                    'text': 'Hi!',
                                }],
                            },
                        }))
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
            await save_call_and_extract(caller_number, call_sid, full_transcript)
        except Exception as e:
            logger.error(f'Failed to save call log: {e}')


async def save_call_and_extract(caller_number, call_sid, transcript):
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

    # Match caller to a rep
    rep = None
    if caller_number:
        clean = caller_number.replace('+1', '').replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
        reps = await sync_to_async(list)(Rep.objects.filter(is_active=True))
        for r in reps:
            r_clean = r.phone_number.replace('+1', '').replace('+', '').replace('-', '').replace(' ', '').replace('(', '').replace(')', '')
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
