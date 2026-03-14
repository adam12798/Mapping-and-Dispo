# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Sutton** — a Django app for mapping MA utility providers with SMS integration, auto-assignment, CRM, and AI voice assistant (Alfred).

## GitHub

- Username: adam12798
- Repo: https://github.com/adam12798/Mapping-and-Dispo.git

## Tech Stack

- Python / Django (ASGI via Uvicorn)
- FastAPI for WebSocket handling (voice assistant)
- Twilio for SMS + Voice (credentials in `.env`)
- OpenAI Realtime API for voice AI assistant (Alfred)
- OpenAI GPT-4o-mini for transcript extraction
- Chart.js v4 for dashboard charts (CDN, no build step)
- OSRM for drive time estimates (free, no API key)
- Leaflet.js for maps (Canvas renderer, no tile layer outside MA)
- Nominatim for geocoding (free, no API key)
- aiohttp for async HTTP calls
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

- **Map** (`/`) — MA map with lead pins color-coded by appointment type (Solar=yellow, HVAC=red, Both=green, Unknown=pink). Canvas renderer for flicker-free zoom/pan. Right sidebar overlays the map (doesn't resize it) and shows appointments for selected date with rep assignment dropdowns. Collapsible route planner (bottom-left) with auto-assign, confirm/redo flow. Star icon marks rep's home/start.
- **CRM** (`/crm/`) — Inline-editable lead table with search bar, filters (date, product type, meeting type, rep, status, disposition), column sorting, resizable columns, and horizontal scroll with frozen name column. Includes Follow Up Date, Call Notes, and Transcript columns. Leads come in via Twilio SMS webhook.
- **Daily** (`/daily/`) — Daily appointment view with date picker (defaults to today). Same features as CRM (search, filters, sticky columns, resizable columns, inline editing, bulk delete) but filtered to a single day's appointments sorted by time.
- **Dashboard** (`/dashboard/`) — Analytics page with Chart.js. Four charts: Appointments by Disposition (horizontal bar), by Rep (bar), Conversion Rate by Rep (bar, %), by Product Type (doughnut). Filters: date range, rep, group by (none/rep/product). KPI pills show total, sales, conversion rate. All data fetched via `/api/dashboard/` without page reload.
- **Reps** (`/reps/`) — Sales rep management with star ratings, color picker (route lines), specialty, and active/inactive status dropdown.
- **Auto-Assign** — Algorithm distributes appointments to active reps based on appointment time, specialty, travel distance, and workload balance. Priority 1: maximize coverage, Priority 2: spread evenly across reps, Priority 3: minimize driving. Load penalty of 30 min per existing lead keeps workload balanced. Two-pass system: first pass assigns with lateness preference, second pass assigns remaining leads to any available rep regardless of lateness. Tries all orderings for ≤6 stops. Target 2-3 appts/day, max 5. Work window 8am-10pm. Avg speed: 45 mph (haversine). User-assigned leads are locked (non-negotiable). Leads with no appointment type cannot be assigned. All datetime comparisons use Eastern time (converted from UTC via `to_naive_eastern()`).
- **Time Off** (`/time-off/`) — Reps text time off requests (e.g. "I cant work friday"). Managers get SMS notifications and can reply APPROVE/DENY. Approved time off blocks reps from auto-assign. Time Off page shows pending requests, approved history, and notification manager list.
- **Voice Assistant (Alfred)** — Reps call +18337990424 to speak with Alfred, a 60-year-old British AI scheduling assistant (OpenAI Realtime API, voice: echo). Two-phase session config: generic first, then enriched with real appointment data after caller identification. Features:
  - Greets rep by first name
  - Knows rep's appointments for next 3 days with drive times between stops (OSRM)
  - Can update lead dispositions via function calling (`update_disposition` tool)
  - Follows disposition decision tree: asks "Did you sit?" then "Did you run credit?" to determine correct dispo
  - Asks for follow-up date on follow_up and cpfu outcomes; auto-sets `future_contact` if date >1 month out
  - Writes call_notes (<20 word paraphrase) and saves call_transcript to the lead
  - Sends webhook to Go High Level on disposition update (phone, name, disposition, call_transcript)
  - Reminds reps of next appointment + drive time after debrief
  - Post-call: transcript saved to VoiceCallLog, time off requests auto-extracted via GPT-4o-mini
  - VAD tuned: threshold 0.85, silence 700ms, prefix padding 500ms
  - Debug: `/voice/debug/`, `/voice/logs/`
- **Route API** (`/api/route/?date=YYYY-MM-DD`) — Pre-computed route for a given date, returns ordered stops + rep info.
- **Disposition** — Sale (green #27ae60), No Sale (purple #8e44ad), Follow Up (orange #e67e22), Credit Fail (pink #e91e63), Cancel at Door (gray #95a5a6), CPFU (light blue #00bcd4), Rep No Show (black #2c3e50), No Coverage (cherry red #c0392b), Needs Reschedule (blue #3498db), Incomplete Deal (amber #d4a017, manager-set only), Future Contact (teal #1abc9c, auto-set when follow-up date >1 month out).
- **Go High Level Webhook** — Disposition updates (from Alfred or manual CRM edits) POST to GHL webhook with phone, name, disposition, and call_transcript. Fires from both `voice_ws.py` (async via aiohttp) and `views.py` (sync via urllib).

## Disposition Decision Tree (Alfred)

1. Did the rep sit the appointment? No → Cancel at Door / Needs Reschedule / Rep No Show
2. Did they run credit?
   - Credit passed + all contracts signed → **Sale**
   - Credit passed + contracts NOT completed → **CPFU** (always, never follow_up)
   - Credit failed → **Credit Fail**
3. No credit run? Still life → **Follow Up**, dead → **No Sale**
- Alfred does NOT tell reps the disposition name, just confirms casually
- No Coverage is never rep-reported

## Important Rules

- **NEVER change map pin colors** — Solar=yellow (#f1c40f), HVAC=red (#e74c3c), Both=green (#27ae60), Unknown/missing=pink (#ff69b4). These colors are critical for the business.
- **NEVER change city/utility provider colors** — The colors assigned to cities on the map correspond to their utility company. These are business-critical and must not be modified.
- **Map tile layer** — The CartoDB base tile layer provides road/building detail inside MA. It can be removed to free up bandwidth/space (town polygons + white mask work without it), but keeping it makes it easier to expand to other states in the future.
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

## UI Architecture

- **Base template**: `maps/templates/maps/base.html` — shared `<head>`, nav, Google Fonts, and block structure (`title`, `head_extra`, `content`, `scripts`). All 6 pages extend this.
- **Nav brand**: "Sutton" with "by ICEBERG" subtitle (gold "by", white "ICEBERG")
- **Active tab**: Driven by `active_tab` context variable passed from each view in `views.py`
- **CSS custom properties**: `:root` variables in `style.css` for colors (`--color-navy`, `--color-blue`, etc.), border-radius (`--radius-sm/md/lg`), shadows (`--shadow-sm/md/lg`), transitions (`--transition-fast/base`), focus ring (`--focus-ring`)
- **Fonts**: Montserrat for all UI elements (nav, tables, inputs, buttons, filters). Courier New only for map overlays (legend, city labels, tooltips) and dashboard KPI pills.
- **Delete buttons**: Use inline SVG × icons, not text "X"

## Key Files

- `maps/models.py` — Lead (incl. call_notes, call_transcript, follow_up_date), Rep, TimeOffRequest, Manager, VoiceCallLog models
- `maps/views.py` — All API endpoints, views, SMS webhook, Twilio outbound, daily_view
- `maps/voice.py` — Voice TwiML endpoint + debug endpoint
- `maps/assignment.py` — Auto-assignment algorithm (respects appt times, time off, specialty)
- `voice_ws.py` — FastAPI WebSocket handler: Twilio ↔ OpenAI Realtime API, rep context lookup, disposition function calling, drive time via OSRM
- `dispo/asgi.py` — ASGI router (WebSocket → FastAPI, HTTP → Django)
- `maps/templates/maps/base.html` — Base template (shared nav, head, blocks)
- `maps/templates/maps/index.html` — Map page with sidebar (shows reps off section)
- `maps/templates/maps/crm.html` — CRM page (resizable columns, call notes/transcript)
- `maps/templates/maps/daily.html` — Daily appointments page (date picker, same features as CRM)
- `maps/templates/maps/reps.html` — Reps page
- `maps/templates/maps/dashboard.html` — Dashboard page (Chart.js charts, filters, KPI pills)
- `maps/templates/maps/time_off.html` — Time Off page (requests, approvals, managers)
- `maps/static/maps/style.css` — All styles (CSS custom properties at top)
- `maps/urls.py` — URL routing
- `dispo/settings.py` — Django settings (timezone, Twilio/OpenAI config, database)
- `Procfile` — Railway deployment (uvicorn ASGI server)

## Roadmap

See `ROADMAP.md` for planned future changes.
