# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mapping and Dispo — a Django app for mapping MA utility providers with SMS integration, auto-assignment, and CRM.

## GitHub

- Username: adam12798
- Repo: https://github.com/adam12798/Mapping-and-Dispo.git

## Tech Stack

- Python / Django
- Twilio for SMS (credentials in `.env`)
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
4. Get Twilio credentials from your Twilio dashboard (do NOT commit them)

## Twilio

- Phone number: +18337990424
- Credentials stored in `.env` (gitignored)

## App Features

- **Map** (`/`) — MA map with lead pins color-coded by appointment type (Solar=yellow, HVAC=red, Both=green, Unknown=pink). Right sidebar shows appointments for selected date with rep assignment dropdowns. Route planner (bottom-left) with auto-assign, confirm/redo flow. Star icon marks rep's home/start.
- **CRM** (`/crm/`) — Inline-editable lead table with search bar, filters (date, product type, meeting type, rep, status, disposition), column sorting, and horizontal scroll with frozen name column. Leads come in via Twilio SMS webhook.
- **Reps** (`/reps/`) — Sales rep management with star ratings, color picker (route lines), specialty, and active/inactive status dropdown.
- **Auto-Assign** — Algorithm distributes appointments to active reps based on geography, travel time, specialty, and workload balance. Target 2-3 appts/day, max 5. Work window 9am-8pm. User-assigned leads are locked (non-negotiable). Same-rated reps get balanced loads.
- **Route API** (`/api/route/?date=YYYY-MM-DD`) — Pre-computed route for a given date, returns ordered stops + rep info.
- **Disposition** — Each lead has a dispo dropdown: Sale (green), No Sale (purple), Credit Fail (pink), Cancel at Door (gray), CPFU (light blue), Rep No Show (black), No Coverage (cherry red).

## Important Rules

- **NEVER change map pin colors** — Solar=yellow (#f1c40f), HVAC=red (#e74c3c), Both=green (#27ae60), Unknown/missing=pink (#ff69b4). These colors are critical for the business.
- **NEVER change city/utility provider colors** — The colors assigned to cities on the map correspond to their utility company. These are business-critical and must not be modified.
- Lead pins use inline styles for color (`.lead-pin` CSS class must stay transparent to avoid double pins).
- **App color scheme**: #293241 (dark navy), #3d5a80 (blue), #98c1d9 (light blue), #e0fbfc (ice blue), #ee6c4d (coral). Use these consistently.
- SMS parser recognizes "Product Type" for appointment type and "Meeting Type" for appointment format.
- Inactive reps are excluded from auto-assign, route fallback, and sidebar dropdowns.
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

- `maps/models.py` — Lead and Rep models
- `maps/views.py` — All API endpoints and views
- `maps/assignment.py` — Auto-assignment algorithm
- `maps/templates/maps/index.html` — Map page with sidebar
- `maps/templates/maps/crm.html` — CRM page
- `maps/templates/maps/reps.html` — Reps page
- `maps/static/maps/style.css` — All styles
- `maps/urls.py` — URL routing
