# Notser — Multi-Channel Notification Platform

An asynchronous, multi-channel notification service (email, SMS, push, in-app) built with
Django + DRF + PostgreSQL. A client `POST`s a notification; it's fanned out into per-recipient,
per-channel **deliveries** that a background worker sends, with **retry → dead-letter**,
**priority** ordering, **scheduling**, **rate limiting**, **idempotency**, and end-to-end
**observability**.

> The send path is fully decoupled from delivery: the API accepts and queues (returns `202`),
> and a separate worker process does the actual sending — so a slow provider never blocks the API.

---

## Features

- **Multi-channel fan-out** — one notification → many deliveries (recipient × channel), each tracked independently.
- **Async delivery** via a DB-backed queue + a worker (`manage.py run_worker`).
- **Retry ladder → DLQ** — transient failures retry on a backoff ladder; permanent (poison) failures and exhausted retries go to a dead-letter state with `replay`.
- **Priority** — `HIGH` deliveries are claimed before `MEDIUM`/`LOW` across the whole queue.
- **Scheduling** — `send_at` parks a notification until its time.
- **Rate limiting** — per-recipient, per-channel caps; over-limit deliveries are *deferred*, not dropped.
- **Idempotency** — an `Idempotency-Key`-style key prevents duplicate sends.
- **Templates** — reusable, versioned, per-channel/locale message templates with `{placeholder}` rendering.
- **Observability** — a `/stats/` roll-up endpoint + a per-notification trace log.
- **Pluggable providers** — Email (Django/SMTP), SMS (Twilio), Push (FCM) implementations included, gated behind config (stubbed by default).

---

## Tech stack

Python 3.10 · Django 5.2 · Django REST Framework · PostgreSQL (psycopg 3) · python-dotenv ·
Twilio SDK · firebase-admin. Background delivery is a DB-backed worker (no external broker);
the producer/consumer seam is structured so Kafka/Celery can drop in later.

---

## Architecture

```
                 ┌─────────────────────────── API process ───────────────────────────┐
  POST /notifications ─► validate ─► idempotency ─► create Notification ─► enqueue_notification()
                 └──────────────────────────────────────────────────────┬────────────┘
                                                                         │ fan out (no send)
                                                                         ▼
                                            Delivery rows (PENDING)  [recipient × channel]
                                                                         │
                 ┌────────────────────── worker process(es) ────────────┼────────────┐
   run_worker ─► claim due deliveries (priority, then time; SKIP LOCKED) │            │
                 ─► rate-limit check ─► sender.dispatch() ──────────────►│  Email/SMS/Push/In-app
                 ─► record outcome: SENT | RETRYING | THROTTLED | DEAD_LETTER         │
                 └─────────────────────────────────────────────────────────────────────┘
```

- **`Notification`** = the intent (channels, recipients, priority, category, template/content, schedule).
- **`Delivery`** = one attempt to one recipient on one channel (status, attempts, error, `next_attempt_at`). This is the delivery log.
- The worker claims due deliveries with `SELECT … FOR UPDATE SKIP LOCKED`, so multiple workers split the work safely with no double-sends.
- `next_attempt_at` is the shared clock for **scheduling**, **retries**, and **throttle deferral**; priority breaks ties among what's due.

**Models:** `Recipient`, `Template`, `Notification`, `Delivery`.

**Delivery statuses:** `PENDING → SENT` | `RETRYING` (transient) | `THROTTLED` (rate-limited, deferred) | `DEAD_LETTER` (permanent / exhausted).
**Notification statuses:** `SCHEDULED`, `PENDING`, `SENT`, `PARTIAL`, `FAILED`.

---

## Prerequisites

- Python 3.10+
- PostgreSQL 14+ running locally

---

## Setup

```bash
# 1. Create a virtualenv and install deps
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. Create the database + a role (adjust to taste)
psql -d postgres -c "CREATE ROLE notser_user WITH LOGIN PASSWORD 'changeme' CREATEDB;"
psql -d postgres -c "CREATE DATABASE notser OWNER notser_user;"

# 3. Configure environment
cp .env.example .env
#   then edit .env — at minimum set SECRET_KEY and DB_PASSWORD

# 4. Apply migrations
./venv/bin/python manage.py migrate
```

`.env` keys (see `.env.example`): `SECRET_KEY`, `DEBUG`, `DB_*` (required); `EMAIL_*`,
`TWILIO_*`, `FCM_CREDENTIALS_FILE` (optional — enable real providers). Timestamps are stored
in UTC; the admin displays them in `Asia/Kolkata` (configurable in `settings.py`).

---

## Running

Two processes, in separate terminals:

```bash
# API
./venv/bin/python manage.py runserver        # http://127.0.0.1:8000

# Background delivery worker
./venv/bin/python manage.py run_worker        # loops; --once to drain and exit
```

`run_worker` flags: `--once`, `--interval <seconds>` (default 1.0), `--batch <n>` (default 50),
`--label <name>` (shown in output; run several workers with different labels to split load).

---

## API

Base path: `/api/v1/`

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/notifications/` | Send (accepts + queues, `202`) |
| `GET` | `/notifications/` · `/notifications/{id}/` | List / fetch one + its deliveries |
| `GET/POST/PUT/DELETE` | `/recipients/[ {id}/ ]` | Manage recipients |
| `GET/POST/PUT/DELETE` | `/templates/[ {id}/ ]` | Manage templates |
| `GET` | `/deliveries/?status=&notification=` | Inspect deliveries (e.g. the DLQ) |
| `POST` | `/deliveries/{id}/replay/` | Re-queue a dead-lettered delivery |
| `GET` | `/stats/` | Pipeline health roll-up |

### Send — direct content

```http
POST /api/v1/notifications/
{
  "recipients": [1, 2],
  "channels": ["EMAIL", "SMS"],
  "priority": "HIGH",
  "category": "TRANSACTIONAL",
  "subject": "Order filled",
  "content": "Your INFY x10 order filled at 1543.20",
  "idempotency_key": "order-789",
  "send_at": null
}
```

### Send — via template

```http
POST /api/v1/notifications/
{
  "recipients": [1],
  "channels": ["EMAIL"],
  "template": "order_filled",
  "data": { "symbol": "INFY", "qty": 10, "price": 1543.20 }
}
```
A template must exist for **every** channel in the list (create via `POST /templates/`).
Missing template / missing `{placeholder}` → `400`.

### Response (`202 Accepted`)

```json
{
  "id": "ntf_…",                       // also the trace id
  "status": "PENDING",                 // or SCHEDULED if send_at is set
  "channels": ["EMAIL", "SMS"],
  "deliveries": [
    { "recipient": 1, "channel": "EMAIL", "status": "PENDING", "priority": "HIGH", ... },
    { "recipient": 2, "channel": "SMS",   "status": "DEAD_LETTER", "error": "recipient has no SMS address", ... }
  ]
}
```

### Conventions

- **Recipients** are referenced by **id** (create them first); the system resolves the address per channel (email→`email`, sms→`phone`, push→`device_token`, in-app→recipient id).
- **Idempotency:** re-sending with a seen `idempotency_key` returns the original (`200`) and does not re-send.
- **`send_at`:** ISO 8601, must be in the future, else `400`.

---

## Channels & providers

All four senders are **stubs by default** (they log, mark `SENT`) so the system runs end-to-end
with no external accounts. Real implementations are included and gated behind config:

| Channel | Provider | Enable |
|---|---|---|
| Email | Django email (console → SMTP) | uncomment `EmailSender` body in `notifications/senders.py`; set `EMAIL_*` in `.env` |
| SMS | Twilio | uncomment `SmsSender` body; set `TWILIO_*` in `.env` |
| Push | FCM (firebase-admin) | uncomment `PushSender` body; set `FCM_CREDENTIALS_FILE` in `.env` |
| In-app | — | stub only (needs a WebSocket gateway) |

When enabled, provider errors are classified: permanent (bad address/token) → `DEAD_LETTER`;
transient (5xx/timeout/quota) → retry ladder. Re-enable the matching test class (remove its
`@skip`) to test the real path.

---

## Configuration

Rate limits (per recipient, per channel) live in `settings.py`:

```python
NOTIFS_RATE_LIMITS = {
    "SMS":   (5, 3600),    # max 5 SMS per recipient per hour
    "EMAIL": (20, 3600),
}
```
Channels not listed are unlimited. Over-limit deliveries are deferred (status `THROTTLED`) until
a slot frees, then re-sent.

Retry backoff ladder is `RETRY_DELAYS` in `notifications/services.py` (seconds between attempts;
exhausting it dead-letters the delivery).

---

## Observability

- **Stats:** `GET /api/v1/stats/` → delivery & notification counts by status, queue depth
  (due-now vs parked), DLQ size, and per-channel success rate.
- **Trace log:** every pipeline stage logs to `logs/notifications.log` (rotating) and the console,
  tagged with `notif=<id>` (the notification id is the trace id). To follow one notification:
  ```bash
  grep "notif=<id>" logs/notifications.log
  ```
  Levels: `INFO` = normal flow, `WARNING` = retries / throttles / dead-letters.

---

## Testing

```bash
./venv/bin/python manage.py test notifications
```
The worker, retry/DLQ, priority, scheduling, rate limiting, templates, and stats are covered.
Real-provider sender tests are skipped while the senders are stubbed (each re-enables with its
sender).

---

## Project layout

```
notser/                 # Django project (settings, urls, logging, provider config)
notifications/
  models.py             # Recipient, Template, Notification, Delivery + enums
  serializers.py        # DRF serializers (incl. the send/validate serializer)
  views.py              # API viewsets + /stats/
  services.py           # enqueue (fan-out) + worker (claim/send/retry/DLQ) + stats
  senders.py            # per-channel senders (stubs; real impls gated/commented)
  management/commands/run_worker.py
  tests.py
manage.py · requirements.txt · .env.example
```

---

## Not built yet (intentionally)

These depend on requirements not yet defined, and are left as clean extension points:

- **API authentication / authz** — the API is currently open.
- **Recipient preferences & opt-out** (per-channel/category consent, fail-open/closed) + quiet hours.
- **Event-driven ingestion** (auto-generating notifications from a domain-event stream).
- **In-app / WebSocket** real-time delivery.
- **Priority isolation** (dedicated worker lanes) — current strict-ordering is sufficient at this scale.
- Swapping the DB-backed queue for **Kafka/Celery** at higher volume.
