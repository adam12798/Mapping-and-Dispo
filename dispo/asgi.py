"""
ASGI config for dispo project.

Routes WebSocket requests for /media-stream to FastAPI (voice_ws),
and all other HTTP requests to Django.
"""
import os
import threading
import time

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dispo.settings')

# Initialize Django ASGI app first
django_asgi = get_asgi_application()

# Import FastAPI app after Django setup
from voice_ws import app as fastapi_app


def _run_dispo_reminders():
    """Background loop that checks for un-dispositioned appointments every 15 minutes."""
    import logging
    logger = logging.getLogger('dispo_reminders')
    time.sleep(30)
    while True:
        try:
            from django.core.management import call_command
            call_command('check_dispo_reminders')
        except Exception as e:
            logger.error(f'Dispo reminder error: {e}')
        time.sleep(900)


_reminder_thread = threading.Thread(target=_run_dispo_reminders, daemon=True)
_reminder_thread.start()


async def application(scope, receive, send):
    """Route WebSocket to FastAPI, HTTP to Django."""
    if scope['type'] == 'websocket':
        await fastapi_app(scope, receive, send)
    else:
        await django_asgi(scope, receive, send)
