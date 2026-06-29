# CLAUDE.md — Notification Platform Design Reference

This repo currently holds one artifact: `notification_platform_design.md.pdf` — an
**Engineering Design Proposal** for a multi-channel notification platform (Draft, June 2026,
by Platform Engineering). There is no source code yet. This file is a condensed reference to
that document so I don't have to re-parse the PDF each session.

> Reading the PDF: it has **55 pages** (not 20) and needs `poppler`/PyMuPDF. The system note
> undercounts. Extract text with PyMuPDF (`import fitz`) — it's installed; `pdftotext`/`pdftoppm` are not.

---

## What's being designed

A multi-channel notification platform (email, SMS, push, in-app/WebSocket; WhatsApp + webhooks
later) delivering **transactional, informational, and promotional** notifications to millions of
users. The driving use case is **fintech / trading**: market-data ticks crossing user-defined
alerts (e.g. "margin call must never queue behind a marketing digest").

**Two entry points:**
1. **Sync client API** — services POST a notification (REST primary, gRPC internal).
2. **Event-driven ingestion** — a Live Alert Engine consumes a domain-event stream (market ticks),
   matches `alert_subscriptions`, and auto-generates notifications.

The doc is a synthesis of 9 production write-ups + 1 internal draft. Source tags used throughout:

| Tag | System | Core lesson |
|---|---|---|
| Uber-RAMEN | Real-time push (SSE→gRPC) | Separate trigger/create/deliver; centralized sharding |
| Netflix-RENO | Rapid Event Notification | Priority SQS queues; hybrid push+pull |
| Uber-Feed | Mobile content delivery | Fan-out-on-write vs read; Cassandra by user_id |
| Uber-CCG | ML push timing | Per-user inbox + ILP scheduler + XGBoost |
| Slack-Trace | Notification tracing | notification_id = trace_id, 100% sampled → 30% faster triage |
| Airbnb-OMNI | Promotions/comms | Service decomposition; governance from day one |
| Razorpay | Webhook scaling | Async DB writes via stream; P0/P1/P2; QoS |
| Ref-AM / Ref-MB | Design guides | Capacity math, idempotency, backoff, broker comparison |
| Internal-Draft | Kafka trading-notif doc | Topic taxonomy, tiered retry, Redis scheduler, fail-open/closed |

`[Independent recommendation]` tags mark advice not from sources (e.g. explicit RPO/RTO, blue-green).

---

## Recommended architecture (one paragraph)

Event-driven, **Kafka-backed** pipeline. Thin stateless **Notification API / ingestion** →
**Enrichment** (contact, locale, prefs, category, dedup) → **Router** (fan-out per channel) →
**per-channel stateless worker fleets** → **channel adapters** (SES/SendGrid, Twilio, FCM/APNs,
WebSocket gateway). **PostgreSQL** = canonical relational state. **Redis** = pref cache,
idempotency keys, scheduler ZSET, rate limits. **Cassandra/ScyllaDB** = high-volume delivery log
(added ~10M users). **ClickHouse + Elasticsearch** = analytics/search.

Pipeline lifecycle: receive → validate → idempotency → preference/category check → (schedule) →
template render → publish → route → worker → provider deliver → ack → status/audit → retry/DLQ →
analytics → cleanup.

---

## Five takeaways that shaped the design

1. **Polling doesn't scale** — go event-driven (Uber: 80% of gateway traffic was polling).
2. **Decouple triggering, creation, delivery** — let transports swap without touching logic.
3. **Protect the datastore** — it falls over first (Razorpay's vertical-scaling wall at ~2K TPS).
4. **Priority + isolation are mandatory** — P0/P1/P2 queues + rate limit + QoS.
5. **You can't operate what you can't trace** — notification-as-trace from day one.

---

## Reliability spine (non-negotiable)

- **At-least-once + idempotency keys → effectively-once.** Redis `SETNX delivered:{user}:{key}` EX 7d.
- **Per-user ordering** via Kafka partition key = `user_id`. Global ordering explicitly NOT a requirement.
- **Tiered retry topics** (`retry.5s` / `1m` / `1h`) → **DLQ**, no external scheduler. Poison messages
  (invalid token, bad number, hard bounce) skip retries straight to DLQ; transient failures walk the ladder.
- **Bulkheading** — per-channel/per-provider delivery is best-effort and independent.
- **Circuit breakers** per provider; **QoS** lowers priority of slow consumers (>5 min) instead of blocking.
- **Async DB writes via a stream** — workers emit status to a stream; a throttled DB-writer consumes it.
  Decouples write IOPS from worker count (the single most reusable Razorpay lesson).
- **Fail-open vs fail-closed** (most important reliability rule): on missing prefs / pref-store outage —
  critical/transactional → **send** (fail open); promotional → **don't send** (fail closed);
  informational → send if default-on, honor explicit opt-out. Never block an OTP; never blast marketing on a guess.
- Message-loss prevention: Kafka **RF ≥ 3, acks=all**, schema validation at ingestion, durable compacted audit topic.

---

## Kafka topic taxonomy

| Group | Topic | Retention |
|---|---|---|
| Ingestion | `notifications.ingest` | hours |
| Ingestion | `notifications.enriched` | hours |
| Delivery | `notifications.channel.{email,sms,push,in-app}` | hours–1d |
| Retry | `notifications.retry.{5s,1m,1h}` | short |
| Dead-letter | `notifications.dlq` | weeks |
| Audit | `notifications.audit` (log-compacted) | weeks–months |

All topics partitioned by `user_id`. **Kafka chosen** because fintech needs all of:
per-user ordering + replay + retention + high throughput. (Netflix/Razorpay/Airbnb chose SQS for
managed simplicity — decision rule: replay+ordering+high volume → Kafka, else SQS.)

---

## Data stores

- **PostgreSQL** — `user_contacts`, `notification_preferences` (marketing_opt_in default false =
  fail-closed), `device_tokens`, `alert_subscriptions`, `templates` (versioned), `consent_log`.
- **Redis** — `delivered:{user}:{key}` (dedupe, 7d), `prefs:{user}` (5min), `scheduled` ZSET
  (timer queue), `rate:{user}:{channel}:{window}`.
- **Cassandra** — `notification_log`, partition key `user_id`, compound clustering
  `(created_at DESC, notification_id)` to avoid concurrent-write races (Uber-Feed pattern). 90d hot → S3.
- **S3** — attachments, archived logs. **ClickHouse** — funnel/rollups. **Elasticsearch** — audience/search (allowlisted fields).

Scheduling: Redis ZSET dispatcher (`ZRANGEBYSCORE now`, ~1s tick) for simple timers;
Temporal/Cadence for complex/durable workflows (Phase 3).

---

## Non-functional targets (SLOs)

- Availability **99.99%** (control plane). Delivery success **≥99% transactional, ≥90% all channels**.
- Latency: **p99 <5s** API→provider, **<30s** full pipeline; **<2s** critical path (OTP/margin call).
- Throughput: sustain **17k notif/sec**, absorb bursts to **150k+ events/sec**.
- Capacity model: 50M users × 5 notif/day = 250M/day; 1M/min peak ≈ 17k/s; peak-to-avg ~30×.
- **Bottleneck fall-over order:** (1) relational DB write IOPS → (2) sync provider calls →
  (3) hot partitions → (4) WebSocket connection RAM → (5) Elasticsearch attribute bloat.

---

## Tech stack (final recommendation)

- **Languages:** Go (workers/hot path — avoids Uber's single-threaded Node.js trap), Python (ML/ILP).
- **API:** gRPC internal + REST gateway, versioned `/v1`.
- **Broker:** Apache Kafka (RabbitMQ only if volume genuinely low).
- **Stores:** PostgreSQL + Redis; Cassandra/ScyllaDB at ~10M users; S3; ClickHouse; Elasticsearch.
- **Connection sharding:** ZooKeeper + Helix-class for WS gateway (NOT decentralized gossip — Ringpop failed).
- **Infra:** Kubernetes + HPA/KEDA (aggressive scale-up, conservative scale-down), Docker.
- **Observability:** OpenTelemetry (notification-as-trace), Prometheus + Grafana, ELK/Loki (structured JSON).
- **Providers:** SES/SendGrid (email), Twilio (SMS — dominant variable cost), FCM/APNs (push).
- **ML:** XGBoost + ILP solver (later).

---

## API surface

Key endpoints (all `/v1`): `POST /notifications`, `POST /notifications:batch`,
`GET /notifications/{id}`, `GET /users/{id}/notifications`, `POST /notifications/{id}:read`,
`GET/PUT /users/{id}/preferences`, `POST /users/{id}/devices`, `GET/POST /templates`,
`POST /campaigns`, `POST /dlq/{id}:replay`, `POST /webhooks/subscriptions`.

- AuthN: OAuth2 client-credentials / mTLS + per-tenant API keys. AuthZ: RBAC scopes
  (`notify:send`, `notify:read`, `admin:campaign`).
- Idempotency: `Idempotency-Key` header → Redis SETNX 7d. Re-send returns original, never re-delivers.
- Errors: 400 VALIDATION_ERROR, 409 IDEMPOTENCY_CONFLICT, 422 PREFERENCE_BLOCKED,
  429 RATE_LIMITED, 503 DOWNSTREAM_UNAVAILABLE.
- `notification_id` minted at API = `trace_id` (Slack model).

---

## Channels

| Channel | Cost | Reliability lever | Primary use |
|---|---|---|---|
| Push (FCM/APNs) | very low | token hygiene, bulkhead | engagement, alerts |
| In-app/WS | low | hybrid push+pull, persist | live updates, unread |
| Email (SES/SendGrid) | low | SPF/DKIM/DMARC | receipts, digests, fallback |
| SMS (Twilio) | **high** | throttle, channel-shift | OTP, security, margin call |
| WhatsApp (BSP) | medium | template approval, sessions | opportunistic (P3) |
| Webhook | low | QoS, HMAC signing, rate limit | B2B (P2) |

WebSocket gateway: bidirectional/binary/instant-acks (WS or gRPC streams), centralized sharding,
hybrid push (online via registry) + pull (offline persist). Critical-push failure escalates to SMS.

---

## Implementation roadmap

- **MVP:** core reliable pipeline. F1–F3, F6–F7, F9–F10, F12–F13, F15–F16, F22, F26–F29, F35.
  Kafka + Postgres + Redis; tiered retry→DLQ; idempotency; fail-open/closed; basic metrics.
- **Phase 2:** WebSocket (F4), read state, quiet hours, history, async DB writes + Cassandra,
  notification tracing, priority queues + rate limiter + QoS, analytics dashboards.
- **Phase 3:** marketing platform — Audience service (ES), campaign UI + approval, multi-tenancy,
  ML send-time optimization + frequency capping (ILP + XGBoost).
- **Phase 4:** global scale + DR — multi-region active, Kafka MirrorMaker, channel-shift cost engine, chaos.

Highest-risk items (both Phase 2, each needs its own design review): **WebSocket connection-sharding**
and the **async-write + wide-column migration**.

---

## 15 documented mistakes to avoid

Polling-not-events · decentralized gossip sharding · single-threaded hot path · unidirectional/text
transport · sync DB writes on hot path · vertical DB scaling · one delivery model for all devices ·
no priority isolation · no staleness filter · governance as afterthought · unbounded indexed
attributes · fragmented per-system logging · blocking critical messages on a prefs outage ·
no idempotency · letting failures poison the pipeline.
