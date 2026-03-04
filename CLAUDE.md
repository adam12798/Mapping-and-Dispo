# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Mapping and Dispo — a Django app for mapping MA utility providers with SMS integration.

## GitHub

- Username: adam12798
- Repo: https://github.com/adam12798/Mapping-and-Dispo.git

## Tech Stack

- Python / Django
- Twilio for SMS (credentials in `.env`)
- Static maps UI (HTML/CSS in `maps/`)

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

- **Map** (`/`) — MA utility provider map with lead pins color-coded by appointment type (Solar=yellow, HVAC=red, Both=green). Route planner shows rep's daily route with numbered stops.
- **CRM** (`/crm/`) — Inline-editable lead table. Leads come in via Twilio SMS webhook.
- **Reps** (`/reps/`) — Sales rep management with star ratings, color picker (used for route lines), and specialty.
- **Route API** (`/api/route/?date=YYYY-MM-DD`) — Pre-computed route for a given date, returns ordered stops + rep info.
- Lead pins use inline styles for color (`.lead-pin` CSS class must stay transparent to avoid double pins).

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
