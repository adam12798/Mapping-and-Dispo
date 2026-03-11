import os
import json
import asyncio
import websockets
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt


def voice_debug(request):
    """Debug endpoint to test OpenAI Realtime API connection."""
    api_key = os.environ.get('OPENAI_API_KEY', '')
    results = {
        'api_key_set': bool(api_key),
        'api_key_prefix': api_key[:10] + '...' if api_key else 'NOT SET',
    }

    # Try connecting to OpenAI Realtime API
    async def test_connection():
        try:
            headers = {
                'Authorization': f'Bearer {api_key}',
                'OpenAI-Beta': 'realtime=v1',
            }
            ws = await websockets.connect(
                'wss://api.openai.com/v1/realtime?model=gpt-realtime',
                additional_headers=headers,
            )
            # Wait for session.created
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            data = json.loads(msg)
            results['openai_connection'] = 'SUCCESS'
            results['openai_event'] = data.get('type', 'unknown')
            await ws.close()
        except Exception as e:
            results['openai_connection'] = 'FAILED'
            results['openai_error'] = str(e)

    asyncio.run(test_connection())
    return JsonResponse(results)


@csrf_exempt
def voice_answer(request):
    """Twilio Voice webhook — returns TwiML that connects to our WebSocket media stream."""
    host = request.get_host()
    # Railway runs behind HTTPS proxy, so always use wss://
    forwarded_proto = request.headers.get('X-Forwarded-Proto', '')
    protocol = 'wss' if (request.is_secure() or forwarded_proto == 'https') else 'ws'

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{protocol}://{host}/media-stream" />
    </Connect>
</Response>"""

    return HttpResponse(twiml, content_type='text/xml')
