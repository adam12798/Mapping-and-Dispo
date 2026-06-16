web: python manage.py migrate && uvicorn dispo.asgi:application --host 0.0.0.0 --port $PORT
worker: while true; do python manage.py check_dispo_reminders 2>&1; sleep 900; done
