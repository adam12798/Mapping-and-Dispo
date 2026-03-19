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

## Authentication & Roles

- **Two roles**: Manager (full access) and Rep (read-only own leads)
- Django's built-in auth with `UserProfile` model linking User → role + optional Rep record
- Login at `/login/`, logout at `/logout/`
- `@login_required` on all views, `@manager_required` on admin-only views
- `get_user_rep(user)` helper returns the linked Rep for rep users (used for data filtering)
- `is_manager` context variable injected into all templates via `maps/context_processors.py`
- **Manager access**: All pages (Map, CRM, Daily, Reps, Time Off, Dashboard, Users) + full CRUD
- **Rep access**: Map, CRM, Daily only — filtered to their assigned leads, all inputs disabled (read-only), can post lead updates (chatter)
- Nav bar is role-conditional — reps only see Map/CRM/Daily tabs
- User management at `/users/` (manager-only) — create/edit/delete accounts, assign roles, link to Rep records
- Twilio webhooks (`sms_webhook`, `voice_answer`) and FastAPI WebSocket remain unauthenticated
- First manager account must be created via a one-time `/setup-admin/` route (add temporarily, remove after use)

## App Features

- **Map** (`/`) — MA map with lead pins color-coded by appointment type (Solar=yellow, HVAC=red, Both=green, Unknown=pink). Canvas renderer for flicker-free zoom/pan. Right sidebar overlays the map (doesn't resize it) and shows appointments for selected date with rep assignment dropdowns. Collapsible route planner (bottom-left) with auto-assign, confirm/redo flow. Star icon marks rep's home/start. Route planner and rep dropdowns hidden for rep users.
- **CRM** (`/crm/`) — Inline-editable lead table with search bar, filters (date, product type, meeting type, rep, status, disposition), column sorting, resizable columns, and horizontal scroll with frozen name column. Includes Follow Up Date, Call Notes, Transcript, and Updates (chatter) columns. Leads come in via Twilio SMS webhook. Supports bulk editing (shift-click selection, change field on selected rows) and bulk delete. Read-only for rep users.
- **Daily** (`/daily/`) — Daily appointment view with date picker (defaults to today). Same features as CRM (search, filters, sticky columns, resizable columns, inline editing, bulk edit, bulk delete) but filtered to a single day's appointments sorted by time. Read-only for rep users.
- **Dashboard** (`/dashboard/`) — Analytics page with Chart.js. Four charts: Appointments by Disposition (horizontal bar), by Rep (bar), Conversion Rate by Rep (bar, %), by Product Type (doughnut). Filters: date range, rep, group by (none/rep/product). KPI pills show total, sales, conversion rate. All data fetched via `/api/dashboard/` without page reload. Manager-only.
- **Reps** (`/reps/`) — Sales rep management with star ratings, color picker (route lines), specialty, and active/inactive status dropdown. Manager-only.
- **Users** (`/users/`) — User account management. Create/edit/delete users, assign role (Manager/Rep), link rep users to Rep records, toggle active status, reset passwords. Manager-only.
- **Auto-Assign** — Algorithm distributes appointments to active reps based on appointment time, specialty, travel distance, and workload balance. Priority 1: maximize coverage, Priority 2: spread evenly across reps, Priority 3: minimize driving. Load penalty of 30 min per existing lead keeps workload balanced. Two-pass system: first pass assigns with lateness preference, second pass assigns remaining leads to any available rep regardless of lateness. Tries all orderings for ≤6 stops. Target 2-3 appts/day, max 5. Work window 8am-10pm. Avg speed: 45 mph (haversine). User-assigned leads are locked (non-negotiable). Leads with no appointment type cannot be assigned. All datetime comparisons use Eastern time (converted from UTC via `to_naive_eastern()`).
- **Time Off** (`/time-off/`) — Reps text time off requests (e.g. "I cant work friday"). Managers get SMS notifications and can reply APPROVE/DENY. Approved time off blocks reps from auto-assign. Time Off page shows pending requests, approved history, and notification manager list. Manager-only.
- **Lead Update Chatter** — Monday.com-style comment thread on each lead. Chat bubble button on CRM/Daily rows opens a modal with update history + text input. Both managers and reps can view and post updates. API at `/api/leads/<id>/updates/`. Reps can only access updates on their own assigned leads.
- **Bulk Editing** — CRM and Daily support shift-click multi-selection and bulk field updates. Select 2+ rows, change a bulk-editable field (rep, disposition, sat, appointment_type, appointment_format, follow_up_date) → confirm dialog → single POST to `/api/leads/bulk-update/`. Green toast + row flash on success. GHL webhook fires per lead on bulk disposition changes.
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
  - Debug: `/voice/debug/`, `/voice/logs/` (manager-only)
- **Route API** (`/api/route/?date=YYYY-MM-DD`) — Pre-computed route for a given date, returns ordered stops + rep info.
- **Disposition** — Sale (green #27ae60), No Sale (purple #8e44ad), Follow Up (orange #e67e22), Credit Fail (pink #e91e63), Cancel at Door (gray #95a5a6), CPFU (light blue #00bcd4), Rep No Show (black #2c3e50), No Coverage (cherry red #c0392b), Needs Reschedule (blue #3498db), Incomplete Deal (amber #d4a017, manager-set only), Future Contact (teal #1abc9c, auto-set when follow-up date >1 month out).
- **Go High Level Webhook** — Disposition updates (from Alfred, manual CRM edits, or bulk updates) POST to GHL webhook with phone, name, disposition, and call_transcript. Fires from `voice_ws.py` (async via aiohttp), `views.py` lead_update (sync via urllib), and `views.py` leads_bulk_update (sync via urllib).

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
- **NEVER change city/utility provider colors** — The colors assigned to cities on the map correspond to their utility company. These are business-critical and must not be modified. Only move cities between providers when the user explicitly requests it.
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

- **Base template**: `maps/templates/maps/base.html` — shared `<head>`, nav, Google Fonts, and block structure (`title`, `head_extra`, `content`, `scripts`). All pages extend this (except login.html which is standalone).
- **Nav brand**: "Sutton" with "by ICEBERG" subtitle (gold "by", white "ICEBERG")
- **Nav right side**: Username + Logout link (Montserrat font)
- **Active tab**: Driven by `active_tab` context variable passed from each view in `views.py`
- **Role-conditional nav**: Reps only see Map, CRM, Daily tabs. Managers see all tabs including Users.
- **CSS custom properties**: `:root` variables in `style.css` for colors (`--color-navy`, `--color-blue`, etc.), border-radius (`--radius-sm/md/lg`), shadows (`--shadow-sm/md/lg`), transitions (`--transition-fast/base`), focus ring (`--focus-ring`)
- **Fonts**: Montserrat for all UI elements (nav, tables, inputs, buttons, filters). Courier New only for map overlays (legend, city labels, tooltips) and dashboard KPI pills.
- **Delete buttons**: Use inline SVG × icons, not text "X"
- **Login page**: Standalone (no base.html), gradient Sutton branding, animated background, coral Sign In button

## Key Files

- `maps/models.py` — Lead, Rep, TimeOffRequest, Manager, VoiceCallLog, UserProfile, LeadUpdate models
- `maps/views.py` — All API endpoints, views, SMS webhook, Twilio outbound, auth views (login/logout), user management, lead updates API. Auth decorators: `manager_required`, `get_user_rep()`
- `maps/voice.py` — Voice TwiML endpoint + debug endpoint (manager-only)
- `maps/assignment.py` — Auto-assignment algorithm (respects appt times, time off, specialty)
- `maps/context_processors.py` — Injects `is_manager` into all template contexts
- `voice_ws.py` — FastAPI WebSocket handler: Twilio ↔ OpenAI Realtime API, rep context lookup, disposition function calling, drive time via OSRM
- `dispo/asgi.py` — ASGI router (WebSocket → FastAPI, HTTP → Django)
- `dispo/settings.py` — Django settings (timezone, Twilio/OpenAI config, database, LOGIN_URL, context processors)
- `maps/templates/maps/base.html` — Base template (shared nav with role-conditional tabs, username/logout)
- `maps/templates/maps/login.html` — Standalone login page (gradient branding, animated bg)
- `maps/templates/maps/index.html` — Map page with sidebar (route planner hidden for reps)
- `maps/templates/maps/crm.html` — CRM page (resizable columns, call notes/transcript, chatter modal, bulk edit, read-only for reps)
- `maps/templates/maps/daily.html` — Daily appointments page (same features as CRM)
- `maps/templates/maps/reps.html` — Reps page (manager-only)
- `maps/templates/maps/dashboard.html` — Dashboard page (Chart.js charts, filters, KPI pills, manager-only)
- `maps/templates/maps/time_off.html` — Time Off page (requests, approvals, managers, manager-only)
- `maps/templates/maps/users.html` — User management page (manager-only)
- `maps/static/maps/style.css` — All styles (CSS custom properties at top)
- `maps/urls.py` — URL routing (includes login/logout, users, lead updates)
- `Procfile` — Railway deployment (uvicorn ASGI server)

## Roadmap

See `ROADMAP.md` for planned future changes.
