import os
import json
import asyncio
import websockets
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from maps.views import manager_required


@manager_required
def voice_debug(request):
    """Debug endpoint to test OpenAI Realtime API connection."""
    api_key = os.environ.get('OPENAI_API_KEY', '')
    results = {
        'api_key_set': bool(api_key),
        'api_key_prefix': api_key[:10] + '...' if api_key else 'NOT SET',
    }

    # Try connecting to OpenAI Realtime API and sending session config
    async def test_connection():
        try:
            headers = {
                'Authorization': f'Bearer {api_key}',
            }
            ws = await websockets.connect(
                'wss://api.openai.com/v1/realtime?model=gpt-realtime',
                extra_headers=headers,
            )
            # Wait for session.created
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            results['step1_connect'] = data.get('type', 'unknown')
            if data.get('type') == 'error':
                results['step1_error'] = data.get('error', {})
                await ws.close()
                return

            # Send session.update with same config as voice_ws.py
            session_config = {
                'type': 'session.update',
                'session': {
                    'type': 'realtime',
                    'instructions': 'You are a test assistant.',
                    'output_modalities': ['audio'],
                    'audio': {
                        'input': {
                            'format': {'type': 'audio/pcmu'},
                            'transcription': {'model': 'gpt-4o-mini-transcribe'},
                            'turn_detection': {
                                'type': 'server_vad',
                                'threshold': 0.85,
                                'silence_duration_ms': 700,
                                'prefix_padding_ms': 500,
                            },
                        },
                        'output': {
                            'format': {'type': 'audio/pcmu'},
                            'voice': 'echo',
                        },
                    },
                },
            }
            await ws.send(json.dumps(session_config))

            # Wait for session.updated or error
            msg2 = await asyncio.wait_for(ws.recv(), timeout=5)
            data2 = json.loads(msg2)
            results['step2_session_update'] = data2.get('type', 'unknown')
            if data2.get('type') == 'error':
                results['step2_error'] = data2.get('error', {})
                await ws.close()
                return

            # Step 3: Send enriched session update (like Phase 2 with tools)
            enriched = {
                'type': 'session.update',
                'session': {
                    'type': 'realtime',
                    'instructions': 'You are a test assistant.',
                    'tools': [{
                        'type': 'function',
                        'name': 'test_tool',
                        'description': 'A test tool',
                        'parameters': {
                            'type': 'object',
                            'properties': {'name': {'type': 'string'}},
                            'required': ['name'],
                        },
                    }],
                },
            }
            await ws.send(json.dumps(enriched))
            msg3 = await asyncio.wait_for(ws.recv(), timeout=5)
            data3 = json.loads(msg3)
            results['step3_enriched_update'] = data3.get('type', 'unknown')
            if data3.get('type') == 'error':
                results['step3_error'] = data3.get('error', {})
                await ws.close()
                return

            # Step 4: Send greeting + response.create
            await ws.send(json.dumps({
                'type': 'conversation.item.create',
                'item': {
                    'type': 'message',
                    'role': 'user',
                    'content': [{'type': 'input_text', 'text': 'Say hi'}],
                },
            }))
            await ws.send(json.dumps({'type': 'response.create'}))

            # Collect events for a few seconds to see what comes back
            events = []
            audio_sample = None
            try:
                end_time = asyncio.get_event_loop().time() + 5
                while asyncio.get_event_loop().time() < end_time:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    evt = json.loads(raw)
                    etype = evt.get('type', 'unknown')
                    events.append(etype)
                    if etype == 'response.output_audio.delta' and audio_sample is None:
                        audio_sample = {k: (v[:50] if isinstance(v, str) and len(v) > 50 else v) for k, v in evt.items()}
                    if etype == 'error':
                        results['step4_error'] = evt.get('error', {})
                        break
                    if etype == 'response.done':
                        break
            except asyncio.TimeoutError:
                pass
            results['step4_events'] = events
            if audio_sample:
                results['audio_event_sample'] = audio_sample

            await ws.close()
        except Exception as e:
            results['openai_error'] = str(e)

    asyncio.run(test_connection())
    return JsonResponse(results)


@manager_required
def voice_logs(request):
    """Debug endpoint to check voice call logs."""
    from maps.models import VoiceCallLog
    logs = VoiceCallLog.objects.order_by('-created_at')[:10]
    data = []
    for log in logs:
        data.append({
            'id': log.id,
            'rep': log.rep.name if log.rep else None,
            'caller_number': log.caller_number,
            'call_sid': log.twilio_call_sid,
            'transcript': log.transcript[:500] if log.transcript else '',
            'summary': log.summary,
            'created_at': str(log.created_at),
        })
    return JsonResponse({'logs': data})


@csrf_exempt
def voice_answer(request):
    """Twilio Voice webhook — returns TwiML that connects to our WebSocket media stream."""
    host = request.get_host()
    # Railway runs behind HTTPS proxy, so always use wss://
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '')
    protocol = 'wss' if (request.is_secure() or forwarded_proto == 'https') else 'ws'

    # Twilio sends caller info in POST params
    caller = request.POST.get('From', '') or request.GET.get('From', '')
    call_sid = request.POST.get('CallSid', '') or request.GET.get('CallSid', '')

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{protocol}://{host}/media-stream">
            <Parameter name="callerNumber" value="{caller}" />
            <Parameter name="callSid" value="{call_sid}" />
        </Stream>
    </Connect>
</Response>"""

    return HttpResponse(twiml, content_type='text/xml')
