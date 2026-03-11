from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt


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
