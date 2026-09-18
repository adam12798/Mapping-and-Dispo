# INTEGRATION.md — Sutton ↔ Wednesday (hub) contract

**Status:** Stage C discovery spike. Read-only audit of the code as of commit `255c25b` (2026-08-30), cross-checked against live production data. No code was changed to produce this document. **The Sutton half was built on branch `hub-integration` (2026-09-18) — see [§6](#6-what-hub-integration-shipped-2026-09-18); where §6 and earlier sections disagree, §6 is current.**

**Scope:** everything below is scoped to the **Ventana** org (`slug: ventana`, org id 1). Team Sunshine (`slug: team-sunshine`, org id 2, the default inbound org) must be unaffected by any integration built from this document — see [§0 Isolation guarantee](#0-isolation-guarantee).

**Verified against production** (read-only queries, 2026-08-30):
- Ventana has **zero** `WebhookConfig` rows today. Team Sunshine has two (both active, both pointing at their GHL account).
- Ventana already has one API key: tenant **"Marketing Canvas"**, `organization = ventana`, last used 2026-08-27.
- Last config-driven outbound webhook delivery: **2026-07-13** (17 config-sourced deliveries all-time, all HTTP 200). The path is dormant because the triggers haven't occurred, not because it is broken.

---

## 0. Isolation guarantee

How "Team Sunshine is unaffected" is actually enforced, and where that guarantee stops.

**What is org-scoped and therefore safe:**

| Mechanism | Enforcement |
|---|---|
| Webhook config rows | `_do_fire_webhooks` filters `WebhookConfig.objects.filter(trigger=…, is_active=True, organization_id=org_id)` where `org_id` comes from `lead.organization_id`. A Ventana lead can only ever fire Ventana's configs. |
| Webhook firing thread | `fire_webhooks` captures the lead's org **before** spawning the timer thread and re-activates it inside `_do_fire_webhooks`, because thread stacks don't inherit the contextvar. |
| API key → org | `api_key_required` resolves `APITenant.organization_id` and wraps the whole view in `org_context(org_id)`. |
| All v1 reads/writes | Every v1 view uses the scoped default manager (`Lead.objects`, `Rep.objects`), which filters on the contextvar and **returns nothing when no org is active** (fail-closed). `get_object_or_404(Lead, pk=…)` is scoped too, so a Ventana key asking for a Team Sunshine lead id gets a 404, not a row. |

**⚠️ The one real cross-org risk — an API key with no organization.** If `APITenant.organization` is `NULL`, `api_key_required` falls back to `Organization.default_inbound_id()`, **which is Team Sunshine**, and logs a warning. A hub key created without an org set would silently read and write Team Sunshine's data. **Any key minted for Wednesday must have `organization = ventana` set at creation and verified after.**

**⚠️ Shared-code risk.** Config *rows* are per-org, but `_do_fire_webhooks` is *shared code*. Adding Ventana webhook configs is zero-risk to Team Sunshine. Changing the payload builder, the debounce, or the delivery mechanism changes Team Sunshine's live webhooks too. **Rule for Stage C: config additions are free; engine changes must be strictly additive and backward-compatible.** In particular, do not "fix" the string-typing described in §1.4 — Team Sunshine's GHL receiver depends on the current format.

**Edge case:** an orphan lead (`organization_id IS NULL`) resolves `org_id = None`, and the config query then matches only configs with a NULL org. None exist today, so orphan leads fire nothing.

**Correction to CLAUDE.md:** it states that GHL webhooks fire from `views.py::lead_update` and `voice_ws.py` via hardcoded URLs. That is no longer true. `_send_ghl_dispo_webhook`, `_send_ghl_appt_webhook` (views.py) and `_send_ghl_dispo_webhook_async` (voice_ws.py) are **defined but never called** — dead code. The hardcoded `GHL_WEBHOOK_URL` constants survive only as preset URLs in the `/ghl-debug/` builder UI. **Every outbound webhook today flows through the org-scoped `WebhookConfig` system**, which is good news for isolation: there is no un-scoped global sender left in the delivery path.

---

## 1. Events OUT (Sutton → hub)

### 1.1 Trigger inventory

Seven trigger types exist (`WebhookConfig.TRIGGER_CHOICES`). What matters is not the type but **which code paths actually call `fire_webhooks`** — several obvious paths call nothing.

| Trigger | Fires from | Does **not** fire from |
|---|---|---|
| `disposition_changed` | CRM inline edit (`lead_update`, when `disposition` in payload); bulk update (`leads_bulk_update`); manager SMS update (`apply_manager_sms_update`, on `cancel`/`disposition`); Alfred `update_disposition` and manager `update_lead` | v1 API `PUT /api/v1/leads/<id>/`; `POST /api/v1/ghl/disposition/` |
| `appointment_changed` | CRM inline edit (only when the datetime actually changed); Alfred `update_lead` | **`ghl_appointment`**, **`ghl_reschedule`**; bulk update; v1 PUT |
| `lead_created` | **only** `POST /api/v1/leads/create/` | **`ghl_appointment`** (GHL bookings), **inbound SMS** lead creation |
| `lead_cancelled` | CRM inline edit (when `cancelled` set truthy) | `ghl_cancel`; `ghl_appointment` with status=cancelled; SMS cancel path |
| `rep_assigned` | CRM inline edit (whenever `rep_id` is in the payload — including clearing it) | bulk update; auto-assign; `confirm_assignments_api`; TextBlast claim |
| `sat_changed` | CRM inline edit | bulk update |
| `follow_up_set` | CRM inline edit | bulk update; Alfred's follow-up date writes |

Two structural observations:

- **Bulk edits emit only `disposition_changed`**, even though rep, sat, appointment type/format and follow-up date are all bulk-editable. A manager bulk-assigning 20 leads to a rep emits nothing.
- **Every inbound GHL path is silent.** `ghl_appointment` creates and updates leads, `ghl_reschedule` moves appointments, `ghl_cancel` cancels them — none call `fire_webhooks`. If Ventana's bookings arrive through GHL, Sutton emits nothing today.

### 1.2 The two events the hub asked for

**"Appointment created / booked" → needs a code change (new call site; the trigger type already exists).**

`lead_created` fires from exactly one place: `POST /api/v1/leads/create/`. If Wednesday creates leads through that endpoint, the event works today. If Ventana's bookings arrive via GHL (`ghl_appointment`) or inbound SMS — the two paths that create real leads in production — **no event fires**. Fixing this is two `fire_webhooks('lead_created', lead)` calls, not a new trigger type.

"Booked" on an *existing* lead (an appointment datetime being set or moved) maps to `appointment_changed`, which likewise doesn't fire from the GHL paths.

**"Disposition set" → works via config today.**

`disposition_changed` fires from every path a human or Alfred actually uses to set a disposition: CRM inline edit, bulk edit, manager SMS, and both Alfred tools. This needs zero code — only a `WebhookConfig` row for Ventana.

### 1.3 Can the payload carry what a hub needs?

Payload fields are chosen per config (`WebhookConfig.fields`, a JSON list of keys). The builder resolves them as: `rep_name` → `lead.rep.name`; `appointment_datetime` → GHL-formatted string; `disposition` → title-cased (`no_sale` → `No_Sale`); **anything else → `getattr(lead, key, '')`**.

Because the fallback is a raw `getattr` with no validation, **any Lead attribute works as a field key**, including ones the UI can't offer:

| Hub need | Field key | Status |
|---|---|---|
| Lead id | `id` | ✅ works — but **not selectable in the `/ghl-debug/` builder UI**; must be set via `POST /api/webhook-configs/` (manager auth) or directly in the DB |
| Homeowner name | `homeowner_name` | ✅ in UI |
| Phone | `phone_number` | ✅ in UI |
| Address | `address`, `city`, `state` | ✅ in UI |
| Appointment time | `appointment_datetime` | ✅ in UI (formatted string, see below) |
| Rep | `rep_name` ✅ in UI · `rep_id` ✅ works, not in UI | |
| Source | `source` | ✅ in UI |

**Verdict: works via config today**, with the caveat that `id` and `rep_id` must be configured through the API rather than the UI. Adding them to the UI picker is a two-line template change (`ALL_FIELDS` in `maps/templates/maps/ghl_debug.html`).

### 1.4 Payload gotchas the hub must handle

- **Every value is a string.** `payload[key] = str(val)`. Booleans arrive as Python's `"True"` / `"False"` (capitalized, not JSON `true`/`false`). `None` becomes `""`. So `cancelled` → `"True"`, `sat` → `"True"` / `"False"` / `""`, `latitude` → `"42.3601"`, `id` → `"1234"`.
- **`disposition` is re-formatted**, not raw: `no_sale` → `No_Sale`, `cpfu` → `Cpfu`. The hub should map from these, not from the raw DB values in §3.
- **`appointment_datetime` is a formatted local string**, not ISO 8601 (`_format_appt_dt_for_ghl`). The v1 API returns proper ISO — the two surfaces disagree.
- **No event metadata.** The payload carries no event type, no timestamp, no delivery id. The hub must infer the event from which URL received it (use one URL per trigger), or add a static custom header per config.
- **No signature.** Outbound webhooks are unsigned. Custom headers are configurable per config, so a static bearer token in a header is the available authentication mechanism.

### 1.5 Delivery guarantees — read this before designing the hub receiver

`fire_webhooks` schedules a **60-second debounced `threading.Timer`** (`WEBHOOK_DELAY = 60`), keyed on `(trigger, lead_id)`. Repeated calls **cancel and reset** the timer.

| Question | Answer |
|---|---|
| Retries? | **None.** One attempt, 10-second timeout. |
| Receiver down / 500s? | Attempt is made, failure is recorded, **event is dropped**. Nothing re-sends it. |
| Where logged? | A `GHLWebhookLog` row per attempt: `organization_id` stamped, `direction='outbound'` (model default), `webhook_type=<trigger>`, `source='config:<config name>'`, plus URL, payload, `response_status`, `response_body` (2 KB), `success`, `error_message`. |
| Visible where? | The `/ghl-debug/` page (manager UI) and the database. **Not** via `GET /api/v1/ghl/logs/` — that endpoint hard-filters `direction='inbound'`, so a hub cannot self-serve its own delivery failures. |
| Process restart? | **Events in the 60s debounce window are lost silently, with no log row at all** — nothing is persisted until the delivery attempt runs. A Railway deploy, crash, or restart during that window drops them. The timer thread is a daemon, so it is killed at exit without running. |
| Rapid edits? | Coalesced. Five disposition changes in 60 seconds produce **one** delivery carrying the final state; intermediate states are never emitted. |
| Ordering? | Not guaranteed. Each event is an independent timer; two triggers on the same lead can arrive out of order. |

**Design consequence — SETTLED 2026-08-30: "hint + reconcile".** This is **at-most-once** delivery with a silent-loss window. The hub treats Sutton events as *fast hints*, never as a ledger, and reconciles by polling on a sweep. Do not build the receiver assuming a trustworthy stream. Note that the reconciliation sweep needs a change feed that today's `?since=` does not provide — see §4.2.

---

## 2. API IN (hub → Sutton)

Base: `https://sutton-soda.com/api/v1/`. Auth: `Authorization: Bearer <api_key>`, or `X-API-Key: <key>`, or `?api_key=…`. The key is `APITenant.api_key` (a UUID).

| Hub need | Endpoint | Status |
|---|---|---|
| Read a lead | `GET /api/v1/leads/<id>/` | ✅ **exists** — full record incl. `call_transcript` |
| List / poll leads | `GET /api/v1/leads/` | ⚠️ **exists, but not a change feed** — filters `date`, `start`, `end`, `rep_id`, `disposition`, `since`; paginated (`page`, `per_page`, max 100). **`since` filters `created_at`, so it misses edits to existing leads** — see §4.2 |
| Update status (disposition) | `PUT /api/v1/leads/<id>/` | ✅ **exists** |
| Update follow-up date/time | `PUT` — `follow_up_date`, `follow_up_time` | ✅ **exists** |
| Update notes | `PUT` — `call_notes`, `appt_notes`, `post_appt_notes` | ✅ **exists** |
| Update owner (rep) | `PUT` — `rep_id` | ✅ **exists** |
| Create a lead | `POST /api/v1/leads/create/` | ✅ **exists** — requires `address`; fires `lead_created` |
| List reps | `GET /api/v1/reps/` | ⚠️ **exists, missing fields** — active reps only, no way to include inactive; returns id, name, phone, home_address, city, lat/lng, specialty, rating, color, is_active. Omits `textblast_eligible`, `sms_consent` (needed before any hub-triggered SMS) |
| Trigger an SMS to a rep | — | ❌ **absent** — no v1 endpoint sends SMS. `POST /api/textblast/send/` exists but is session/manager-authenticated, not API-key, and is TextBlast-specific |
| Stats | `GET /api/v1/stats/` | ✅ exists |
| Time off | `GET /api/v1/time-off/?date=` | ✅ exists (read-only) |
| Delivery-log read-back | `GET /api/v1/ghl/logs/` | ⚠️ inbound-only (see §1.5) |

**Behavioural notes on `PUT /api/v1/leads/<id>/` that the hub design depends on:**

- **It fires no webhooks.** A hub-driven disposition change does not emit `disposition_changed`. This conveniently prevents echo loops, but it also means Sutton's other consumers (Team Sunshine's GHL receiver is *not* one of them — different org) never learn about hub-driven changes. Document it as intentional or change it deliberately; don't discover it in production.
- **It writes no chatter entry.** CRM edits create a `LeadUpdate` row ("Disposition: Follow Up → Sale"); the v1 PUT does not. Hub-driven changes are invisible in the lead's Updates thread — no audit trail of who changed what.
- Writable fields: `homeowner_name`, `phone_number`, `address`, `city`, `state`, `source`, `tags`, `appointment_type`, `appointment_format`, `appointment_datetime`, `disposition`, `sat`, `follow_up_date`, `follow_up_time`, `call_notes`, `appt_notes`, `call_transcript`, `cancelled`, `monthly_cost`, `total_cost`, `adders`, `post_appt_notes`, and `rep_id`. Not writable: geo (auto-derived on address/city change), all internal stamps.
- `DELETE` is supported and is a hard delete. Consider not granting the hub a key path to it.

**Auth → org mapping (confirmed):** `api_key_required` looks up the tenant, rejects unknown/inactive keys with 401, stamps `last_used_at`, then runs the entire view inside `org_context(tenant.organization_id)`. Cross-org access is impossible **provided the tenant row has an organization** — see the NULL-org warning in §0. `rate_limit` (default 1000/hr) exists on the model but **is not enforced anywhere**. CORS on `/api/v1/` allows any origin (`CORS_ALLOWED_ORIGIN_REGEXES = ['.*']`); the API key is the only gate.

---

## 3. The Lead field map

Every field on `maps.models.Lead`, classified for a hub-side customer record. **SHARED** = the hub would mirror it. **SUTTON-ONLY** = routing, geo, voice, or internal scheduling state that should not leave Sutton.

### SHARED — identity

| Field | Type | Notes |
|---|---|---|
| `id` | AutoField (PK) | The join key. Stable, per-instance, not exposed in the builder UI by default (§1.3) |
| `homeowner_name` | char(200), blank | Free text; blank on SMS-created leads until parsed |
| `phone_number` | char(20), blank | Not normalized on write; format varies (`+1…`, `(978) …`). Matching uses last-10-digit `icontains` (§4) |
| `address` | char(500), **required** | Only truly required field on the model |
| `city` | char(200), blank | |
| `state` | char(50), blank | Defaults to `MA` on GHL/v1 creation |
| `source` | char(200), blank | Lead source; also drives provider-portal filtering |
| `tags` | char(200), blank | Product-type text from GHL; `appointment_type` is auto-computed from it |

### SHARED — appointment

| Field | Type | Notes |
|---|---|---|
| `appointment_datetime` | datetime, null | Stored UTC, rendered Eastern. **CRM is the source of truth for appointment times** |
| `appointment_type` | choice: `solar`/`hvac`/`both`, blank | Blank = unassignable by auto-assign |
| `appointment_format` | choice: `in_person`/`virtual`, blank | |
| `rep` | FK → Rep, null | The assigned owner. Serialized as `rep_id` + `rep_name` |
| `cancelled` | bool, default False | Soft-cancel; cancelled leads stay in the table |

### SHARED — outcome / follow-up

| Field | Type | Notes |
|---|---|---|
| `disposition` | choice, blank | 13 values: `sale`, `no_sale`, `follow_up`, `credit_fail`, `cancel_door`, `cpfu`, `rep_no_show`, `no_coverage`, `needs_reschedule`, `incomplete_deal`, `future_contact`, `dq`, `no_show`. Blank = not yet dispositioned (drives reminders) |
| `sat` | bool, **nullable** | Tri-state: True / False / unknown |
| `follow_up_date` | date, null | |
| `follow_up_time` | time, null | |
| `call_notes` | char(**200**), blank | Alfred's <20-word paraphrase. Short field — hub must truncate |
| `appt_notes` | text, blank | Pre-appointment notes (from GHL `Notes`) |
| `post_appt_notes` | text, blank | |
| `monthly_cost` | char(100), blank | Free text, not numeric (`"$150/mo"`) |
| `total_cost` | char(100), blank | Free text, not numeric |
| `adders` | text, blank | |

### SUTTON-ONLY — do not mirror

| Field | Type | Why it stays |
|---|---|---|
| `organization` | FK → Organization | Tenancy. Never expose; the hub key implies the org |
| `latitude` / `longitude` | float, null | Derived by geocoding; recomputed whenever address/city changes |
| `from_number` | char(20), blank | Twilio sender of the originating SMS — telephony plumbing |
| `raw_message` | text, blank | Raw inbound SMS body. **Do not repurpose as a spare field** (§4) |
| `call_transcript` | text, blank | Full Alfred call transcript. Readable via v1 detail; treat as sensitive — it is a verbatim recording of a rep conversation |
| `created_at` | datetime, auto | Sutton row-creation time, ≠ booking time |
| `dispo_reminder_sent_at` | datetime, null | Reminder-worker state |
| `dispo_call_made_at` | datetime, null | Reminder-worker state (Alfred callback) |
| `follow_up_reminder_sent_at` | datetime, null | Reminder-worker state |
| `textblast_sent_at` | datetime, null | TextBlast dedupe state |

**Related tables** (not fields, but part of the record a hub might expect): `LeadMessage` (SMS thread, reverse `lead.messages`) and `LeadUpdate` (chatter thread, reverse `lead.updates`). Both are org-scoped, both are readable only through session-authenticated endpoints — **there is no v1 API for either**.

---

## 4. Identity linkage

**Lead identity.** `Lead.id` (integer PK) is the only stable unique identifier. There is no natural key: `phone_number` is not unique or normalized, `homeowner_name` is free text and frequently blank, `address` is the only required field but is not unique (duplicates exist across re-bookings).

**Rep identity.** `Rep.id` is the join key and is what `PUT …/leads/<id>/ {"rep_id": N}` expects. Note that Sutton's own SMS paths match reps by `name__icontains` and by last-10-digit phone — the hub should always use `rep_id`, never name.

**⚠️ Phone dedupe behaviour on the GHL appointment endpoint.** `_ghl_match_lead(name, phone, address)` tries three matches in order, taking the **most recently created** match at each step:

1. `homeowner_name__iexact` **AND** `phone_number__icontains(last 10 digits)`
2. `homeowner_name__iexact` **AND** `address__icontains`
3. **`phone_number__icontains(last 10 digits)` alone** — name ignored entirely

Consequences the hub must design around:

- **One phone number = one lead, forever.** A repeat customer booking a second appointment **updates the existing lead in place** (new datetime overwrites the old) rather than creating a second row. `POST /api/v1/ghl/appointment/` is documented as "never creates duplicates" — that is the mechanism. The hub must not assume one booking = one lead.
- Step 3 ignores the name, so two different people sharing a phone (spouses, a shared household line, an office number) collapse onto one lead.
- Matching is `icontains` on the last 10 digits, so it is substring-based, not exact-equality — a stored number containing those 10 digits anywhere matches.
- `POST /api/v1/leads/create/` does **not** dedupe. It creates unconditionally. The two creation endpoints therefore behave oppositely.

**Where a hub-side customer id would live.** There is **no spare field**. `tags` carries product type, `source` carries lead source and drives provider filtering, `raw_message` holds the raw SMS body and is semantically wrong to overload (and would silently corrupt SMS-origin leads). Overloading any of them is not recommended.

> ### ⚠️ The one schema change Stage C asks of Sutton
>
> Add two nullable/defaulted columns to `Lead` in a single migration:
>
> ```python
> hub_customer_id = models.CharField(max_length=100, blank=True, db_index=True)
> updated_at = models.DateTimeField(auto_now=True, db_index=True)
> ```
>
> Both are additive and safe for Team Sunshine's rows (`hub_customer_id` stays blank; `updated_at` backfills to the migration timestamp). Shipping `hub_customer_id` also means exposing it in the v1 serializers (read + writable via PUT) and ideally as a `?hub_customer_id=` filter on the list endpoint, so the hub resolves its own id → Sutton lead without maintaining a mapping table.
>
> `updated_at` is what makes reconciliation work — see §4.2 for why it is required and the trap that will silently defeat it.
>
> Note for whoever ships it: Railway runs `manage.py migrate` on deploy, so this migration applies automatically at merge — unlike the hardening branch, this change set will **not** be migration-free.

### 4.2 Why `updated_at` ships with `hub_customer_id` (decided 2026-08-30)

The settled reconciliation model (§1.5) polls Sutton on a sweep to backstop lost webhook events. Today's `?since=` filter targets **`created_at`**, so a sweep sees *newly created* leads and nothing else. The single most important case it misses is **a disposition set on an older lead** — precisely the event reconciliation exists to catch. Without a modified timestamp there is no change feed, only a creation feed.

**⚠️ The trap: `auto_now` fires only on `Model.save()`.** Queryset-level `.update()` writes straight to SQL and silently skips it. Sutton has five such paths on `Lead` today, and one of them is the highest-value event in the whole integration:

| Path | What it writes | Why it matters |
|---|---|---|
| `voice_ws.py:649` — Alfred `update_disposition` | `disposition`, `call_notes`, costs, notes | **Alfred is a primary way dispositions get set.** Left unhandled, the flagship event is invisible to the change feed |
| `views.py:681` — `leads_bulk_update` | disposition, rep, sat, appt type/format, follow-up | Bulk disposition edits also vanish from the feed |
| `views.py:1224` — `confirm_assignments_api` | `rep_id` | Owner changes from route confirmation |
| `views.py:1119` — `clear_assignments_api` | `rep=None` | Bulk un-assignment |
| `views.py:1208` — `send_textblast` | `textblast_sent_at` | Internal stamp; arguably *should not* bump `updated_at` |

**Correction (2026-09-18): this table is incomplete.** `save(update_fields=[...])` also skips `auto_now` unless `updated_at` is in the list, which adds `ghl_cancel`, `ghl_disposition`, the SMS cancel path, the TextBlast claim, auto-assign (`assignment.py`) and the three reminder stamps. With the orphan backfill command that makes fourteen bypassing writes, not five. This is why the trigger was chosen (§6).

Handle these explicitly (add `updated_at=timezone.now()` to each `.update()` call), or use a database trigger, which cannot be bypassed and needs no discipline from future code. **A plain `auto_now` field alone reproduces the same silent-loss hole in a new place** — the change feed would look healthy while quietly missing Alfred's dispositions.

Decide deliberately whether internal stamps (`textblast_sent_at`, the reminder stamps) should count as "updated". Bumping on them makes the hub re-poll leads whose customer-visible state did not change; not bumping keeps the feed meaningful.

**API change must be additive.** Do not silently repoint the existing `since` parameter at `updated_at` — an existing consumer relying on creation semantics would break. Add a new parameter (`updated_since=`) or an explicit mode flag, and leave `since` as it is.

---

## 5. Gaps, ranked by effort (smallest first)

| # | Gap | Effort | Notes |
|---|---|---|---|
| 1 | **No Ventana webhook configs exist** | Config only, no deploy | Create `WebhookConfig` rows for `ventana` via `/ghl-debug/` or `POST /api/webhook-configs/`. Zero risk to Team Sunshine |
| 2 | `id` / `rep_id` not selectable in the builder UI | ~2 lines | Add to `ALL_FIELDS` in `ghl_debug.html`. They already work when set via API |
| 3 | **`lead_created` doesn't fire for real bookings** | 2 call sites | Add `fire_webhooks('lead_created', lead)` to `ghl_appointment` (new-lead branch) and the SMS creation path. Trigger type already exists |
| 4 | `appointment_changed` doesn't fire from GHL paths | 2 call sites | `ghl_appointment` (datetime-changed branch) and `ghl_reschedule` |
| 5 | Bulk edits emit only `disposition_changed` | ~5 lines | Fire `rep_assigned` / `sat_changed` / `follow_up_set` from `leads_bulk_update` for consistency with single edits |
| 6 | Outbound delivery failures invisible to the hub | 1 filter param | `GET /api/v1/ghl/logs/` hard-codes `direction='inbound'`; add `?direction=` |
| 7 | v1 PUT writes no chatter entry | ~5 lines | Create a `LeadUpdate` on hub-driven changes so they appear in the audit thread |
| 8 | `GET /api/v1/reps/` omits SMS-eligibility fields | ~3 lines | Add `textblast_eligible`, `sms_consent`; decide whether inactive reps should be listable |
| 9 | **`hub_customer_id` + `updated_at` columns** | **1 migration** + serializer edits + 5 `.update()` call sites | The flagged schema change (§4). `updated_at` is required for reconciliation to work at all; the five queryset `.update()` paths must be handled or the change feed silently misses Alfred's dispositions. Add `updated_since=` as a **new** param — don't repoint `since` |
| 10 | **At-most-once delivery with a silent-loss window** | Medium | In-process `threading.Timer` loses queued events on restart with no log row. **Decided 2026-08-30: accept it and reconcile by polling** (§1.5) rather than building a persisted outbox — which is what makes gap #9 load-bearing. Note the same class of fragility already bit the reminder thread — see the DB-connection bug in the project notes |
| 11 | **No hub-triggered SMS to a rep** | Medium | New API-key endpoint required. Must respect `sms_consent`, and must send from the per-org number resolved by `maps/sms_numbers.py`. **Superseded 2026-09-18:** that is the 833 for every org now, because replies to the 978 never reach Sutton (§6.4) |
| 12 | `APITenant.rate_limit` not enforced | Medium | Field exists, no enforcement anywhere. A hub bug could hammer the app |
| 13 | No v1 access to `LeadMessage` / `LeadUpdate` threads | Medium | Only session-authenticated endpoints exist today |
| 14 | Payload typing (all strings, `"True"`/`"False"`, non-ISO datetimes) | **Do not fix** | Changing `_do_fire_webhooks` changes Team Sunshine's live payloads. Handle the coercion hub-side (§1.4) |

**Recommended Stage C slice:** gaps 1–4 deliver both requested events (`appointment created` and `disposition set`) for Ventana with roughly a dozen lines of code plus config, and touch no shared behaviour Team Sunshine depends on. Gap 9 is the migration to schedule deliberately. Gap 10 is the one that decides whether the hub can trust the event stream or must reconcile by polling — answer it before building the receiver, not after.

**Status 2026-09-18:** gaps 2, 3, 4 and 9 are built on `hub-integration`. Gap 4 uses a new trigger, not `appointment_changed` (§6). Gap 1 is deliberately not done: there is no receiver URL yet, so the branch ships inert.

---

## 6. What `hub-integration` shipped (2026-09-18)

### 6.1 Schema: migration `0042` (the only one)

- `Lead.hub_customer_id` — `varchar(100)`, nullable, indexed, not unique. NULL means not linked; the v1 API stores a blank value as NULL.
- `Lead.updated_at` — `timestamptz NOT NULL`, indexed. Existing rows are backfilled to the migration's run time, so the hub's first sweep sees every lead once. That is by design: a change feed may over-report, never under-report.
- **`updated_at` is owned by a PostgreSQL trigger** (`maps_lead_updated_at` → `maps_lead_set_updated_at()`), not by `auto_now`, because fourteen write paths bypass `auto_now` (§4.2 correction). The trigger's rules:
  - It bumps the value (to `clock_timestamp()`) when **any column changes except** `dispo_reminder_sent_at`, `dispo_call_made_at`, `follow_up_reminder_sent_at` and `textblast_sent_at`. Those are worker and TextBlast bookkeeping, so a reminder or blast does not make the hub re-poll the lead. A TextBlast *claim* still bumps it, because it assigns a rep.
  - A write that changes nothing keeps the old value. A no-op `save()` or bulk edit is not a change.
  - The value cannot be set or backdated by hand.
  - It also fills the column on INSERT. That makes a code-only rollback safe: pre-0042 code can still create leads against the migrated schema (verified).
  - SQLite dev databases have no trigger; there `auto_now` covers `save()` only.
- `WebhookConfig.trigger` gains the choice `appt_rescheduled`. This is choices-only and emits no SQL.

### 6.2 Events

| Trigger | New call sites |
|---|---|
| `lead_created` | `ghl_appointment` (new-lead branch); inbound SMS: setter-format new lead, and GHL-format `NEW APPOINTMENT`/`SCHEDULED` new lead. **Not** the placeholder row an unmatched `APPOINTMENT CANCELLED` text creates |
| `appt_rescheduled` (new) | `ghl_reschedule` and `ghl_appointment` (existing lead), when the time actually moved or a cancelled lead came back. A resend of the same time fires nothing |

- **Why a new trigger instead of `appointment_changed`:** Team Sunshine's live config on `appointment_changed` posts to their GHL. Their GHL sent Sutton 304 bookings and 175 cancels through `ghl_appointment` between 2026-08-01 and 2026-09-18, so reusing it would echo GHL's own traffic back to it. `ghl_disposition` already refuses to echo for the same reason. Team Sunshine has no config on either trigger that fires from the new call sites, so they receive nothing new (tested end to end).
- **Trigger names must be ≤ 20 characters.** Every delivery is logged with `GHLWebhookLog.webhook_type = <trigger>`, a `varchar(20)`. A longer name makes that INSERT fail inside the timer thread after the POST has already gone out, so the audit row is lost. `appointment_rescheduled` did exactly this in local verification before it was renamed. A test now enforces the limit.
- The engine (`fire_webhooks` / `_do_fire_webhooks`) is **unchanged**. Team Sunshine's two live configs are pinned by a test to the exact bytes the pre-branch engine produced.
- The builder's field picker has a new **IDs** group: `id`, `rep_id`, `hub_customer_id`.
- **These paths are still silent** (the change feed covers them): SMS-text reschedules, manager-SMS reschedules, `ghl_cancel`, `ghl_update`, non-disposition bulk edits, auto-assign, v1 `PUT`.

### 6.3 v1 API (additive)

- `GET /api/v1/leads/` and `GET /api/v1/leads/<id>/` add `updated_at` (ISO 8601 with offset, microseconds kept) and `hub_customer_id`.
- `PUT /api/v1/leads/<id>/` and `POST /api/v1/leads/create/` accept `hub_customer_id` (≤ 100 characters, else 400; blank clears it).
- `GET /api/v1/leads/?hub_customer_id=X` does an exact match, scoped to the key's org.
- `GET /api/v1/leads/?updated_since=T[&after_id=N]` is the change feed. `?since=` is untouched and still filters `created_at`.
  - With `updated_since`, results are ordered by `(updated_at, id)` ascending. Without `after_id` the filter is `updated_at >= T`; with it, the filter is `updated_at > T OR (updated_at = T AND id > N)`.
  - `T` must carry a UTC offset. A naive or unparseable value is a **400**, not silently ignored, because reading UTC as Eastern would move the cursor by hours. An unencoded `+` that arrives as a space is repaired.

**The polling protocol the hub should use:**

1. Keep a cursor `(T, N)`. Request `?updated_since=T&after_id=N&per_page=100` and **stay on page 1**. Set the cursor to the last row's `(updated_at, id)` and repeat until fewer than 100 rows come back. Do not walk `page=2,3…`: rows that change mid-walk shift the pages and get skipped.
2. `after_id` is required for correctness. One bulk edit can give many rows the same instant, and a timestamp-only cursor would loop or skip there.
3. Re-sweep with overlap, for example from `T − 5 minutes` every so often, and dedupe on `(id, updated_at)`. A write's timestamp is taken before it commits, so a row can become visible slightly after a poller has moved past its timestamp.
4. The first sweep after deploy returns every lead (§6.1 backfill).

### 6.4 SMS numbers: the 978 is reply-routed to MarketingCanvas

**Found 2026-09-18 in the Twilio console:** the 978's inbound goes through its Messaging Service to **MarketingCanvas's** `/api/webhooks/twilio`. The 833's inbound points at Sutton's `/sms/`. So a reply to anything Sutton sends from the 978 never reaches Sutton.

**Rule: the 978 must never be the From on anything that expects an SMS reply into Sutton.** That covers every text Sutton sends: TextBlast (claims are replies), manager time-off notices (APPROVE/DENY), manager-update answers, dispo and follow-up reminders, and consent confirmations. `maps/sms_numbers.py` now sends **every org from the 833**, and `A2P_SMS_ORG_SLUGS` is empty.

History:
- **Before 2026-08-29:** TextBlast alone sent from the 978, for both orgs. That is where Team Sunshine's 2026-06-23 blast claims would have gone.
- **2026-08-29 23:30Z (deploy of `649e720` + `64808b2`) until this change:** all of Ventana's SMS went out from the 978.
- **What was lost in that window: nothing found.** Sutton sent no reply-inviting text from the 978: no reminders, blasts, time-off traffic or consent confirmations are stamped for either org after the switch. MarketingCanvas's logs, continuous across all its deployments since 2026-08-27, record one unmatched inbound from a Sutton rep/manager number (2026-09-14, the owner's own phone, 2 characters). Its data export holds no `sms_in` note, suppression or consent from any of the 35 Sutton rep/manager numbers.

What MarketingCanvas does with a 978 inbound (`server/sms-routes.js`, deployed `aa3f083`):
- STOP/START/HELP keywords are settled first. They go to MC's own `sms_suppressions` and `sms_consents`, keyed by phone, never to Sutton.
- A sender matching an MC lead is written to that lead's `lead_notes` as `sms_in`.
- A sender matching no lead (every Sutton rep) is **not stored in any table**. It is `console.warn`ed to MC's Railway log (sender and the first 120 characters). It is also forwarded by SMS to MC's `NOTIFY_PHONE` (first 800 characters) when Twilio is configured and the sender isn't that phone.
- The full message is always in **Twilio's own Messaging log** for the 978, which is the authoritative place to recover one.

**The cost of this change:** the 833 is described in this codebase as carrier-filtered (sends accepted, delivery unreliable). That is why Team Sunshine's TextBlast is off. Ventana's texts now ride that number too.

**Durable fix (future work, not built):** MarketingCanvas relays 978 inbounds whose sender matches a Sutton rep or manager on to Sutton's `/sms/` (or a signed Sutton endpoint). Then Ventana can send from its A2P-registered 978 again and still receive replies. Only then should `A2P_SMS_ORG_SLUGS` include `ventana` again.

Related: the public SMS consent page (`sms_consent.html`, part of the 978's A2P filing) tells reps to text START to the 978. That text is handled by MarketingCanvas, not Sutton. Sutton records rep consent only through the manager's checkbox.
