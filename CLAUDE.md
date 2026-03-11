# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mapping and Dispo — a Django app for mapping MA utility providers with SMS integration, auto-assignment, and CRM.

## GitHub

- Username: adam12798
- Repo: https://github.com/adam12798/Mapping-and-Dispo.git

## Tech Stack

- Python / Django (ASGI via Uvicorn)
- FastAPI for WebSocket handling (voice assistant)
- Twilio for SMS + Voice (credentials in `.env`)
- OpenAI Realtime API for voice AI assistant
- OpenAI GPT-4o-mini for transcript extraction
- Leaflet.js for maps (OpenStreetMap tiles)
- Nominatim for geocoding (free, no API key)
- Static maps UI (HTML/CSS/JS in `maps/`)

## Environment Setup (for new machines)

1. Clone the repo: `git clone https://github.com/adam12798/Mapping-and-Dispo.git`
2. Install dependencies (Python/Django)
3. Create a `.env` file with the following keys:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_PHONE_NUMBER`
   - `OPENAI_API_KEY`
4. Get Twilio credentials from your Twilio dashboard (do NOT commit them)
5. Get OpenAI API key from https://platform.openai.com/api-keys

## Twilio

- Phone number: +18337990424
- Credentials stored in `.env` (gitignored) and as env vars on Railway
- **SMS Inbound**: Webhook receives leads (from setters), time off requests (from reps, matched by phone number), and manager APPROVE/DENY replies
- **SMS Outbound**: Notifies managers of time off requests, confirms approve/deny to reps
- **Voice Inbound**: Webhook at `/voice/answer/` returns TwiML with `<Connect><Stream>` to bridge to OpenAI Realtime API via WebSocket at `/media-stream`
- Timezone: America/New_York (EST/EDT)

## App Features

- **Map** (`/`) — MA map with lead pins color-coded by appointment type (Solar=yellow, HVAC=red, Both=green, Unknown=pink). Right sidebar shows appointments for selected date with rep assignment dropdowns. Route planner (bottom-left) with auto-assign, confirm/redo flow. Star icon marks rep's home/start.
- **CRM** (`/crm/`) — Inline-editable lead table with search bar, filters (date, product type, meeting type, rep, status, disposition), column sorting, and horizontal scroll with frozen name column. Leads come in via Twilio SMS webhook.
- **Reps** (`/reps/`) — Sales rep management with star ratings, color picker (route lines), specialty, and active/inactive status dropdown.
- **Auto-Assign** — Algorithm distributes appointments to active reps based on appointment time, specialty, travel distance, and workload balance. Priority 1: maximize coverage, Priority 2: minimize driving. Reps arrive at appointment time or up to 30 min late (stretch to 60 min). Tries all orderings for ≤6 stops. Target 2-3 appts/day, max 5. Work window 9am-10pm. User-assigned leads are locked (non-negotiable). Leads with no appointment type cannot be assigned.
- **Time Off** (`/time-off/`) — Reps text time off requests (e.g. "I cant work friday"). Managers get SMS notifications and can reply APPROVE/DENY. Approved time off blocks reps from auto-assign. Time Off page shows pending requests, approved history, and notification manager list.
- **Voice Assistant** — Reps call +18337990424 to speak with an AI scheduling assistant (OpenAI Realtime API, model `gpt-realtime`). Audio bridged via WebSocket: Twilio ↔ FastAPI ↔ OpenAI (g711_ulaw, no transcoding). Caller number passed as TwiML Stream Parameter for rep matching. Post-call: transcript saved to VoiceCallLog, time off requests auto-extracted via GPT-4o-mini and created as pending. Debug: `/voice/debug/` (tests OpenAI connection), `/voice/logs/` (recent call logs).
- **Route API** (`/api/route/?date=YYYY-MM-DD`) — Pre-computed route for a given date, returns ordered stops + rep info.
- **Disposition** — Each lead has a dispo dropdown: Sale (green), No Sale (purple), Follow Up (orange), Credit Fail (pink), Cancel at Door (gray), CPFU (light blue), Rep No Show (black), No Coverage (cherry red).

## Important Rules

- **NEVER change map pin colors** — Solar=yellow (#f1c40f), HVAC=red (#e74c3c), Both=green (#27ae60), Unknown/missing=pink (#ff69b4). These colors are critical for the business.
- **NEVER change city/utility provider colors** — The colors assigned to cities on the map correspond to their utility company. These are business-critical and must not be modified.
- Lead pins use inline styles for color (`.lead-pin` CSS class must stay transparent to avoid double pins).
- **App color scheme**: #293241 (dark navy), #3d5a80 (blue), #98c1d9 (light blue), #e0fbfc (ice blue), #ee6c4d (coral). Use these consistently.
- SMS parser recognizes "Product Type" for appointment type and "Meeting Type" for appointment format.
- Inactive reps are excluded from auto-assign, route fallback, and sidebar dropdowns.
- **Geocoding validates MA bounds** (lat 41-43, lng -73.6 to -69.8). Out-of-state results are rejected and retried with city fallback.
- **CRM is the source of truth** for appointment times — map sidebar always shows CRM time, not computed arrival.
- Local database is empty — all real data lives in Railway's PostgreSQL. Use the live API to check data.
- Cannot access Railway dashboard directly — can only push code to GitHub to trigger deploys.

## Deployment

- Railway auto-deploys from main branch
- App URL: https://lavish-reflection-production-1e5f.up.railway.app
- Always push after committing so Railway picks it up
- Use `python3` (not `python`) for commands

## Multi-Machine Workflow

- Adam works across personal Mac and work computer
- Use `git pull` / `git push` to stay in sync
- `.env` must be recreated manually on each machine
- Cursor settings sync enabled across machines

## Key Files

- `maps/models.py` — Lead, Rep, TimeOffRequest, Manager, VoiceCallLog models
- `maps/views.py` — All API endpoints, views, SMS webhook, Twilio outbound
- `maps/voice.py` — Voice TwiML endpoint + debug endpoint
- `maps/assignment.py` — Auto-assignment algorithm (respects appt times, time off, specialty)
- `voice_ws.py` — FastAPI WebSocket handler bridging Twilio ↔ OpenAI Realtime API
- `dispo/asgi.py` — ASGI router (WebSocket → FastAPI, HTTP → Django)
- `maps/templates/maps/index.html` — Map page with sidebar (shows reps off section)
- `maps/templates/maps/crm.html` — CRM page
- `maps/templates/maps/reps.html` — Reps page
- `maps/templates/maps/time_off.html` — Time Off page (requests, approvals, managers)
- `maps/static/maps/style.css` — All styles
- `maps/urls.py` — URL routing
- `dispo/settings.py` — Django settings (timezone, Twilio/OpenAI config, database)
- `Procfile` — Railway deployment (uvicorn ASGI server)
