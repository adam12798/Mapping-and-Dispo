"""
ASGI config for dispo project.

Routes WebSocket requests for /media-stream to FastAPI (voice_ws),
and all other HTTP requests to Django.
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')

# Initialize Django ASGI app first
django_asgi = get_asgi_application()

# Import FastAPI app after Django setup
from voice_ws import app as fastapi_app


async def application(scope, receive, send):
    """Route WebSocket to FastAPI, HTTP to Django."""
    if scope['type'] == 'websocket':
        await fastapi_app(scope, receive, send)
    else:
        await django_asgi(scope, receive, send)
