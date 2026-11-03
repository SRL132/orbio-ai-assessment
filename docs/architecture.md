# Architecture

> How the screening agent is built so it works as a one-process demo today and scales to 10K+ candidates/week and more clients without a rewrite. Conversation design: [`process-design.md`](process-design.md). Quality gates: [`quality.md`](quality.md).

## Summary

A **modular monolith with a hexagonal core**. A pure conversation engine holds all screening logic. Everything that touches the outside world (LLM, channels, database, calendar, clock) is an adapter behind an interface (a "port"). The demo runs as one process; scaling means swapping adapter implementations and running more workers. The engine, rules and evals don't change.

## What "scale" means here

10K candidates/week ≈ 1,500/day ≈ 15K messages/day, with peaks of a few messages per second. Raw throughput is not the problem. These are:

1. **LLM latency, cost and rate limits.** Every turn takes seconds and costs money.
2. **Webhook reliability.** Channels like WhatsApp retry if they don't get a fast 200, which can cause duplicate deliveries and double replies.
3. **Ordering within a conversation.** Candidates send bursts ("hola" / "soy Ana" / "tengo moto"); processing them in parallel corrupts state.
4. **Long-running timers.** Thousands of 2 h / 24 h reminders must survive restarts.
5. **More clients.** For Orbio, the next client matters more than the next 10K candidates.

## System overview

```
 Channels                 API (stateless)                 Workers (stateless, N copies)
┌──────────┐   webhook   ┌──────────────────┐  enqueue   ┌──────────────────────────────┐
│ Web chat │────────────▶│ Channel adapters │───────────▶│ Turn processor               │
│ WhatsApp │◀────────────│  normalize, dedup│  (by conv) │  lock conversation           │
│ Voice    │    send     │  persist, ack    │            │  load state → ENGINE → save  │
└──────────┘             └──────────────────┘            │  send replies via adapter    │
┌──────────┐             ┌──────────────────┐            └──────────────┬───────────────┘
│Recruiters│────────────▶│ Dashboard (reads │                           │
└──────────┘             │  projections)    │            ┌──────────────▼───────────────┐
                         └────────┬─────────┘            │ Engine (pure, no I/O)        │
                                  │                      │  state + msg + config →      │
                         ┌────────▼──────────┐           │  new state + actions         │
                         │ Database          │◀──────────│  LLM behind ports            │
                         │  events (append)  │           └──────────────────────────────┘
                         │  candidates (view)│      Scheduler: polls `next_reminder_at`
                         │  messages, config │      → enqueues reminder turns
                         └───────────────────┘
```

## Key decisions

### 1. The engine is pure

```python
def turn(
    state: ConversationState, msg: InboundMessage, config: ClientConfig, ports: Ports
) -> tuple[ConversationState, list[Action]]: ...
```

Actions are data: `AskField("start_date")`, `Disqualify("no_license")`, `ScheduleReminder(at=...)`, `HandOff("human_requested")`, `OfferSlots(...)`. The engine performs no database or network calls of its own; the LLM is reached only through the `Extractor` and `Responder` ports.

This gives us fast, deterministic tests and evals; "code decides, the LLM talks" enforced by structure; and the same engine for chat, WhatsApp and voice.

### 2. One turn

```
candidate message
  → Extractor (LLM, strict output schema, with the last 6 messages as context):
      fields found (value, quote, confidence), language, intent, sentiment, confused, faq_id
  → validators (Python, rules from client config)
  → policy picks the next action:
      ask next missing field | re-ask | disqualify | hand off | recap | offer slots
  → Responder (LLM) writes the message in the persona and candidate's language
      — consent, disqualification and recap use fixed templates
      — a job question gets the approved FAQ text for `faq_id`, word for word, or "a recruiter will confirm"
  → output check (one question, length) → else fallback template
```

The flow is **slot-filling**, not a rigid state machine: the next question is the highest-priority field still missing, so answers can arrive in any order.

**Context, not memory.** The structured state carries what the conversation has established. The LLM also gets the last 6 stored messages (up to 300 characters each) to understand a follow-up ("¿y los turnos?") and to avoid repeating itself. They're read from storage each turn, so redacted text stays redacted, and they're never saved with the state. Values still have to quote the current message: the context can't fill a field.

**FAQ answers aren't written by the LLM.** It picks an entry id and code sends that entry's approved text. An unknown id gets the fixed reply, so an invented salary can't reach a candidate. The cost is less natural wording, and one question per message gets one entry.

The extractor schema includes `language`, `intent`, `sentiment` and `confused` **from day one**, even before the policy uses all of them. That keeps the bonus features cheap.

### 3. Webhooks only store and acknowledge

The API normalizes input to `InboundMessage`, drops duplicates by channel message ID, persists, enqueues and returns 200 in milliseconds. All LLM work happens in workers.

Inbound is idempotent per message: before doing anything, the webhook claims the channel's message ID in `channel_deliveries` (a primary-key insert, so of two concurrent copies exactly one wins). A retry is dropped whatever it is: a first message that opens a conversation, a photo that gets the "text only" notice, or a normal answer. If handling fails, the claim is given back, the webhook answers 500, and Meta's retry is handled instead of lost. IDs are pruned after 7 days; Meta stops retrying after 36 h. Opening a conversation for a number is serialized too: when no conversation exists yet, the message takes a short lock (a job lease named by a hash of the number, deleted when released) and looks again. So two different first messages arriving together ("hola" / "soy Ana") open one conversation, and the second joins it. A message that waits more than 5 s for the lock fails, and Meta retries it.

Outbound is at-least-once: the dispatcher sends, then marks the message delivered, so a crash between the two re-sends that one message. Making it exactly-once would need an idempotency key that WhatsApp's send API doesn't offer. There is one dispatcher at a time (a job lease), but it sends to up to `WHATSAPP_DISPATCH_CONCURRENCY` conversations in parallel; each conversation's messages go one at a time, in order, and stop at its first failure. A slow or failing recipient holds up only their own conversation, and the dispatcher stops starting sends at half its lease so the lease is still its own while it sends.

### 4. One conversation at a time

A lease on the conversation row, plus a ~2 s debounce that merges a burst of messages into one turn. That fixes ordering and saves LLM calls.

- **Taking it:** one atomic `UPDATE conversations SET lease_owner, lease_until WHERE id = … AND lease is free or expired`. Of any number of workers, in any number of processes, exactly one matches. It works the same on SQLite and Postgres, and holds no database connection while the LLM is thinking.
- **Expiry:** a lease lasts 3 minutes, longer than any turn (two LLM calls, each with a retry and the fallback provider). A worker that dies holding one blocks its conversation for at most that long. Leases are timed by the system clock, not the demo's fast-forwardable one.
- **Fencing:** `save_turn` checks the caller still owns the lease, so a worker whose lease expired mid-turn gets `LeaseLost` instead of overwriting the new holder's state.
- **No lost messages:** a worker that finds the lease taken returns at once; the holder checks for messages that arrived meanwhile after releasing it (messages are stored before anyone asks for the lease).
- **Who waits:** reminders skip a busy conversation until the next tick; a recruiter override waits up to 10 s, then gets "try again" (409).

Storing an inbound message doesn't wait for that lock: a webhook must be acknowledged even while a turn is running. So message numbers come from a counter on the conversation row (`last_seq`), reserved with one atomic `UPDATE … RETURNING` that holds the row lock until commit. Concurrent writers to one conversation queue up instead of colliding, which reading `max(seq) + 1` didn't guarantee.

### 5. Append-only event log as source of truth

Events (`message_received`, `field_extracted`, `field_rejected`, `status_changed`, `reminder_sent`, `override`) are never edited. The `candidates` table is a projection of them. One mechanism gives the audit trail, analytics, ATS sync (outbox) and replay for debugging.

### 6. Reminders live in the database

A `next_reminder_at` column, polled by a scheduler that enqueues reminder turns. Nothing is lost on restart. The clock is a port, so the demo and tests can fast-forward time.

### 7. Clients are configuration

A client config holds locations and service areas, license rules per country, fields and their priority, persona, templates (by language and channel) and FAQ. Grupo Sazón is the first config (`app/config/grupo_sazon/`), not hard-coded logic. `client_id` is a column on every table from the start, and it is enforced, not just stored: the repository is built for one client and filters every read on it (and refuses to write another client's state), and unique constraints that could collide across clients (interview slots, WhatsApp contacts) include `client_id`.

### 8. LLM calls are hardened

Fewer calls first. `FastPathExtractor` (`app/adapters/llm/fast_path.py`) sits in front of the real extractor: a message that is exactly one of the current question's options ("sí", "Moto", "la segunda") is read by code, and anything else, however close ("¿sí?", "sí, pero solo los lunes"), goes to the LLM. Validators and policy still decide what the answer means. On the channels in `template_questions_on` (voice), a plain next question uses its template; re-asks and questions after confusion still use the LLM. On the voice scenarios (3 repeats, sequential, LLM time only: no speech recognition or text-to-speech) this took p50 from 1.83 s to 0.01 s, p95 from 2.62 s to 1.92 s and LLM cost by 61%, with all 15 runs passing before and after. Scripted answers are more often closed than real ones may be, so the real gain will be smaller.

Prompt caching for the system prompt and config; token usage and cost logged per call and added up per conversation. A smaller model can take over extraction if the evals show it's accurate enough. For a provider that is slow, rate-limited or down (`app/adapters/llm/resilient.py`, wired in `bootstrap.make_resilient_llm`):

- **Retry with backoff.** Each call is retried `LLM_MAX_RETRIES` times on rate limits, server errors and failed connections, with full-jitter exponential backoff (so workers that failed together don't retry together) and the provider's `Retry-After` when it sends one. A read timeout isn't retried on the same provider: it already cost the whole timeout.
- **Fallback provider behind the same interface.** `LLM_FALLBACK_PROVIDER` / `LLM_FALLBACK_MODEL` (e.g. another vendor's model through OpenRouter). When the primary fails, the same call goes to the fallback, and a circuit breaker sends calls straight to the fallback for 30 s before trying the primary again. The engine sees one `Extractor` and one `Responder` either way. Only synthetic data has been evaluated on a fallback model; pick one that passes the same evals. The chosen fallback is `openrouter` / `qwen/qwen3.7-plus`, second in the [provider comparison](#where-the-choice-stands) (19/21 scripted, 97% field accuracy, p95 8.5 s/turn, on the older 21-scenario suite). The top run splits extraction and replies across two models, which a single fallback setting can't express. It sits behind a different vendor from both Gemini, the best live run so far, and Anthropic, the intended production path, so one outage doesn't take out both.
- **Concurrency cap.** At most `LLM_MAX_CONCURRENCY` calls in flight per process, so a peak queues in our process instead of turning into a burst of 429s. Fleet-wide the cap is replicas × that number: size it to the provider's rate limit. A call that can't get a slot in time counts as unavailable.
- **Hold, don't drop.** If no provider answers, the adapter raises `LLMUnavailable` and the engine keeps the candidate's message instead of consuming it: one holding message ("give me a moment…", template `technical_hold`), then retries after 20 s, 1, 3 and 10 minutes (`llm_retry_seconds` in the client config). Messages the candidate sends meanwhile join the retry. When the retries run out, the candidate is told there's a technical problem and asked to write again (`technical_unavailable`), and the usual reminders follow, so a conversation is never dropped silently. Every attempt is an `extractor_failed` event with its attempt number.
- **A bad answer isn't an outage.** Unparseable output or a refusal is not retried later (it would likely fail the same way): the candidate is asked again at once (`technical_retry`), as before. A failed reply (the responder) falls back to the fixed template for that action.

## Hexagonal, but lightweight

| Port | Adapters (now → later) |
| --- | --- |
| `Channel` | web chat → WhatsApp, voice |
| `Extractor`, `Responder` | LLM → fake for tests, cheaper model |
| `Repository`, `EventLog` | SQLite → Postgres |
| `CalendarProvider` | mock slots → Google Calendar |
| `Geocoder` | fixed list (fake) → Nominatim (OpenStreetMap) → self-hosted Nominatim |
| `KnowledgeBase` | FAQ id list in prompt → embeddings |
| `Clock` | system → fake |

What we deliberately **don't** do:

- **No separate models per layer.** Pydantic models are the domain; the repository adapter maps them to SQLAlchemy once.
- **No port without a second implementation** (real + fake counts).
- **No DI framework.** Ports are `typing.Protocol`; wiring is by hand in `bootstrap.py`.
- **No full DDD vocabulary.** A candidate record, a conversation state and a policy are enough.
- **The dashboard skips the hexagon for reads.** It queries projections directly (CQRS-lite). Writes such as overrides go through the core so they are validated and logged.

**The one rule that matters:** `app/domain/` imports nothing from `adapters/`, `api/`, `worker/` or any SDK. Enforced by import-linter (see [`quality.md`](quality.md)).

## Code layout

```
app/
  domain/            # pure: no I/O, no SDKs
    models.py        # Candidate, ConversationState, fields, statuses, actions,
                     #   event types and reasons (enums, not free strings)
    policy.py        # next action, knockouts, outcomes
    validators.py    # field rules, read from config
    engine.py        # turn(state, msg, config, ports) -> (state, actions): one handler per stage
    render.py        # actions -> replies (templates + responder); checks a config's templates
    summary.py       # recruiter summary
    ports.py         # Protocols: Extractor, Responder, Repository, EventLog,
                     #            CalendarProvider, Geocoder, KnowledgeBase, Clock, Channel
  adapters/
    llm/             # extractor + responder (real, fake); prompts/ as versioned files
    channels/        # web chat, (whatsapp, voice)
    storage/         # SQLAlchemy repository, event log, projections
    calendar/ geo/ kb/ clock/
  api/               # FastAPI: webhooks, chat endpoint, dashboard routes
  worker/            # turn processor, reminder scheduler
  config/            # one directory per client: client.yaml, templates/<lang>/<channel>.yaml
  bootstrap.py       # wires adapters to ports
tests/               # unit, contract, recorded, smoke
evals/               # scenarios, simulated candidates, baseline.json
```

## Demo vs production

Same code, different implementations behind the ports.

| Part | Demo (one process) | 10K/week and beyond |
| --- | --- | --- |
| Queue | `ROLE=all`: debounce timers in the FastAPI process | `ROLE=web` + N `python -m app.worker` processes that find waiting messages in the database (leases keep them apart); Redis/SQS only if polling the database stops being enough |
| Database | SQLite via SQLAlchemy | Postgres (same code) |
| Scheduler | background task | cron / worker job |
| Channels | web chat (+ voice) | + WhatsApp adapter |
| Deploy | `uvicorn` locally | API and workers as separate container services |
| Monitoring | logs | turn latency, LLM errors and cost, completion rate, eval scores in CI |

Explicitly **not** now: microservices, Kafka, Kubernetes, a separate vector database, multi-agent orchestration.

## Deployment and monitoring

Design only; the demo runs as one process.

**Deploy**

- **Region:** EU (for Spanish candidates' data under GDPR). A second region for Mexico only if the client requires local residency.
- **Services:** one image (`Dockerfile`) run in two roles, as `docker-compose.yml` does locally with Postgres: `web` (`ROLE=web`: webhooks, chat, dashboard; no background work) and `worker` (`python -m app.worker`: turns, reminders, the WhatsApp outbox, cleanup and retention). Both are stateless: all state is in the database, so any worker can take any conversation. Each worker runs `WORKER_CONCURRENCY` turns at once; scale `worker` on `sazon_pending_messages_oldest_seconds`. Singleton jobs (the outbox) hold a job lease, so they need no separate scheduler service.
- **Chat delivery:** replies are pushed to the browser over server-sent events (`/api/conversations/{id}/stream`), with polling as the fallback. Each open stream checks the database every 0.5 s; past a few thousand concurrent chats, Postgres `LISTEN/NOTIFY` (or Redis pub/sub) would replace that check.
- **Data:** managed Postgres (point-in-time recovery, encrypted at rest) and a managed queue (SQS or Redis Streams). Schema changes go through Alembic migrations (`migrations/`), run by the container before the server starts; a CI test fails if the models change without a migration, and the suite also runs against Postgres in CI.
- **Secrets:** a secrets manager injects LLM keys, `SESSION_SECRET` and recruiter credentials (or SSO). Nothing in images or the repo; gitleaks runs in CI.
- **Release:** CI gates (lint, types, boundaries, tests, offline evals) → image → staging with live evals → production. Prompt and config changes ship per client behind a version number, so one client can be rolled back without the others.

**Monitor**

| Signal | Why | Alert when |
| --- | --- | --- |
| Turn latency p50 / p95 | Candidates wait on every message | p95 > 8 s for 10 min |
| LLM errors, refusals, fallback-template rate | The template fallback hides failures from candidates, so it must be visible to us | fallback rate > 5 % |
| Queue depth and age | Messages waiting to be processed | oldest message > 60 s |
| Completion rate, drop-off by stage | The product's core bet (see VISION) | completion drops > 20 % week on week |
| Review-queue size and age | Recruiters falling behind | oldest item > 2 working days |
| Cost per conversation (tokens × price) | Budget at 10K/week | > 2× the baseline |
| Provider outage: fallback calls, held turns | The primary is down or rate-limited; candidates are waiting on a retry | any `held` for 5 min, or any `gave_up` |
| Outbox backlog | ATS sync stuck | undelivered > 1 h |

Built: `GET /metrics` (Prometheus; set `METRICS_TOKEN` to require a bearer token). Each signal above maps to a metric:

| Signal | Metric |
| --- | --- |
| Turn latency | `sazon_turn_seconds` (histogram, by channel) |
| LLM errors and fallbacks | `sazon_events_total{type="extractor_failed" \| "responder_failed" \| "reply_rejected"}`, against `sazon_llm_calls_total` |
| LLM latency, tokens, cost | `sazon_llm_call_seconds`, `sazon_llm_tokens_total`, `sazon_llm_cost_usd_total` (by provider) |
| Queue depth and age | `sazon_pending_messages`, `sazon_pending_messages_oldest_seconds` (read from the database at scrape time) |
| Outcomes | `sazon_outcomes_total{status}` (completion and drop-off in detail: the analytics page) |
| Drop-off by stage | `sazon_drop_offs_total{stage}` |
| Cost and turns per conversation | `sazon_conversation_cost_usd`, `sazon_conversation_turns` (histograms by outcome, observed when a conversation first reaches one) |
| Provider outage | `sazon_llm_failures_total{which="primary" \| "fallback" \| "busy"}`, `sazon_llm_fallbacks_total`, `sazon_turns_held_total{outcome="held" \| "gave_up"}` |
| Review-queue size and age | `sazon_review_queue`, `sazon_review_queue_oldest_seconds` |
| Outbox backlog | `sazon_whatsapp_outbox`, `sazon_whatsapp_outbox_oldest_seconds`, `sazon_whatsapp_messages_total{result}` |
| Abuse | `sazon_rate_limited_total{endpoint}` |

Labels are enums and provider names only, never ids or personal data.

Tracing (OpenTelemetry): FastAPI's request spans, tagged with a `request.id`, contain a `turn` span per candidate turn, which contains an `llm.<kind>` span per LLM call (tokens, cost, provider). Set `OTEL_EXPORTER_OTLP_ENDPOINT` to send them to any OTLP collector; unset, tracing is a no-op. Every log line carries `req=<id>` and responses return `X-Request-ID`. A turn processed by a worker process starts its own trace, linked by `conversation.id`. Not built yet: the nightly live eval job.

Logs are structured and contain IDs, never message text or names (already true in the code). Traces follow one turn across API → queue → worker → LLM. A nightly job runs the live eval suite against production config and posts the scores.

**10K candidates a week:** ~12 turns each is 120K turns a week, ~0.2 a second on average and ~2 a second at a 10× peak. A turn is mostly waiting for two LLM calls (~4–6 s), so by Little's law the peak needs ~10–12 turns in flight. Each worker process runs up to `WORKER_CONCURRENCY` turns at once in a thread pool (`app/worker/pool.py`, default 16; the conversation lease keeps them apart), so two worker replicas cover the peak with room for one to fail; scale on `sazon_pending_messages_oldest_seconds`. At peak that's ~4 LLM requests a second, which most provider tiers allow; input tokens per minute matter more than requests, so prompt caching matters. Postgres load is trivial. The real limits are the provider's rate limits and cost, and WhatsApp's rules: business-initiated (template) messages are capped per day by the number's messaging tier, which starts low and rises with volume and quality, so warm up the number and keep reminders inside the 24 h window where possible (free-form, no template). Check Meta's current limits before launch.

## How the bonus features plug in

| Bonus | Where it plugs in | Effort |
| --- | --- | --- |
| Analytics | Done: SQL aggregates over projected columns of `conversations` (`progress`, `dropped_at`, `duration_minutes`, `interview_at`), so the page costs a few small queries however many candidates there are. The event log stays the record for replay and for metrics the projection doesn't hold | Low |
| Re-engagement | Done: scheduler + reminder templates; on WhatsApp the outbox delivers reminders to the phone at 2 h and 23 h (inside the 24 h window) | Low |
| Multi-language | Extractor returns `language`; templates per language; stored values language-neutral, so code-switching is handled | Low |
| Sentiment | `sentiment` / `confused` from the extractor; the policy rephrases with options or offers a human. Never used for qualification | Low |
| Tests and evals | Pure engine + fake adapters; simulated candidates against the real engine | Low–Med |
| Guardrails | Input: `intent` drives the policy. Output: a Python check on every reply, with a template fallback. Data: personal data redacted before logging | Low–Med |
| RAG | `KnowledgeBase` port; the extractor picks from a small FAQ id list in the cached prompt, embeddings only if it grows. Code sends the approved text | Low–Med |
| Interview scheduling | New stage after the recap; `CalendarProvider` port; `OfferSlots` / `InterviewBooked` actions; a database constraint prevents double-booking. Booking is idempotent per candidate, so a turn that booked but failed to save can be retried (an external calendar can't join our transaction anyway). A periodic job releases bookings the conversation never recorded, after a 10-minute grace period | Med |
| Location lookup | Done: a `Geocoder` port, called by the engine only for places the config doesn't list (only the place name is sent). The validator decides by distance to the nearest served city (`geo_review_radius_km`), and the candidate confirms the place first. Nominatim adapter: in-memory cache, one request per second and an identifying User-Agent (its usage policy); at volume, self-host it or swap the adapter. A failed lookup sends the candidate to review, never to a knockout | Low–Med |
| ATS integration design | Done: [`ats-integration.md`](ats-integration.md) (outbox → signed webhooks, REST API sketch, field mapping) | Low |
| Deployment design | Done: "Deployment and monitoring" above | Low |
| Voice | Done: a `/voice` page on the same engine. Push-to-talk in the browser (Web Speech API, Chrome/Edge) → text → `engine.turn()` → reply spoken by ElevenLabs when `ELEVENLABS_API_KEY` is set, else by the browser. Voice templates (no emoji or "·" lists, options as sentences, spoken dates), a speech-aware prompt note, spoken-number parsing, no debounce. See "Voice channel" below | Done |

## WhatsApp channel

The production channel, built on the WhatsApp Cloud API (Meta). It uses the same engine; only the edges differ.

- **Inbound:** `GET /webhooks/whatsapp` answers Meta's verification check. `POST /webhooks/whatsapp` checks the `X-Hub-Signature-256` HMAC with the app secret, stores the message (deduplicated by WhatsApp message id, since Meta retries for up to 36 h), acknowledges, and hands the conversation to the normal debounce. A new number starts a conversation; "hola" after a finished one starts a new one.
- **Outbound, through an outbox:** agent replies on external channels are stored as undelivered messages, and a dispatcher sends them in order every second. Only one process dispatches at a time (a renewable job lease in `job_leases`; another process takes over 30 s after the holder stops renewing), so nothing is sent twice. Up to 3 options become reply buttons, longer lists a list message. Errors are retried (429 and 5xx) and given up after 5 attempts. Because reminders are just outbound messages, re-engagement now reaches the candidate's phone.
- **The 24-hour rule:** free-form messages are only allowed within 24 h of the candidate's last message. The client config schedules WhatsApp reminders at 2 h and 23 h; later messages need an approved (paid) template or are skipped and logged.
- **Cost:** since 1 October 2026 Meta charges per delivered service message, so replies inside the 24-hour window are no longer free, and it stops delivering them without a payment method. Add this to the LLM costs below.
- **Demo:** Meta's test number (up to 5 recipients) and an ngrok tunnel for the webhook. Code: `app/adapters/channels/whatsapp.py`, `app/worker/whatsapp.py`, `app/api/whatsapp.py`.

## Voice channel

Voice is a channel, not a second agent: the conversation has `channel: voice`, and everything else (engine, validators, policy, storage, dashboard, evals) is shared.

- **Listening:** the browser's speech recognition turns one push-to-talk utterance into text, which goes through the normal message API. Voice messages skip the debounce, since each one is already a complete utterance.
- **Speaking:** replies are read by ElevenLabs (`POST /api/tts`, server-side, only the agent's text) when configured, otherwise by the browser's `speechSynthesis`.
- **Wording:** `app/config/<client>/templates/<lang>/voice.yaml` overrides only what doesn't work aloud. The responder is told its message will be read aloud, and the output check rejects emoji and "·" lists for voice.
- **Understanding:** the extractor is told the text is speech-to-text output (no punctuation, spelled-out numbers, misheard names). The offline extractor converts spoken numbers ("veintiocho cero uno cero" → 28010).

Limits and the production path: browser speech recognition only works in Chrome and Edge, and Chrome sends the audio to Google for recognition. That's acceptable for a demo but not for candidates' data under GDPR, so production would use server-side speech-to-text (e.g. ElevenLabs or another provider under the client's data-processing agreement) and the WhatsApp or phone channel instead of a web page. The engine wouldn't change.

## LLM provider

> **Status: provisional.** The final choice is made by the eval suite (accuracy → p50/p95 latency → cost), not by reputation, and it hasn't been made yet: see [where the choice stands](#where-the-choice-stands). Prices and capabilities below change often: verify against each provider's official pricing page before quoting them.

### Approach

- **One provider in production, through its own SDK**, behind the `Extractor` / `Responder` ports. The app relies on provider-specific features (prompt caching, per-turn system instructions, strict structured outputs), and it handles candidates' personal data, so fewer intermediaries is better.
- **The port is provider-neutral.** The domain only sees our Pydantic types (`ExtractionResult`, `Reply`), never SDK types, so a second provider is just a second adapter.
- **No routing library (e.g. LiteLLM) in the product.** It's another layer to explain, and it tends to hide provider-specific features.
- **OpenRouter only for model comparison in evals.** One `OpenRouterLLM` adapter runs the same simulated candidates against several providers. It adds roughly a ~5% fee on credits and tens of milliseconds per call, which is negligible for evals. Confirm the winner through its own SDK before locking the baseline, since features differ through the proxy.
- **Offline mode for CI and demos without a key.** `LLM_PROVIDER=fake` uses a rule-based `KeywordExtractor` (Spanish/English keywords, read in the context of the question just asked) and template replies. As a full extractor it is not a production path; in production it only reads whole-message closed answers, through `FastPathExtractor`.
- **Evals use synthetic candidates only.** No real personal data is ever sent to a provider that isn't the approved production one.

### Provider scorecard

✅ strong · ⚠️ check or partial · ❌ blocker for this project. Assessment as of 2026-10-30; to verify.

| Provider | Spanish extraction | Structured output | Native voice | EU data / GDPR | Reputational risk | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| **Anthropic (Claude)** | ✅ | ✅ | ⚠️ STT/TTS pipeline | ⚠️ check DPA, EU options | ✅ | Shortlist |
| **OpenAI (GPT)** | ✅ | ✅ | ✅ Realtime API | ⚠️ check DPA, EU residency | ✅ | Shortlist |
| **Google (Gemini)** | ✅ | ✅ | ✅ Gemini Live | ⚠️ check Vertex EU regions | ✅ | Shortlist |
| **Mistral** | ✅ | ✅ | ⚠️ limited | ✅ EU-based | ✅ | Shortlist if EU residency matters |
| **xAI (Grok)** | ⚠️ verify with evals | ⚠️ check strict schemas | ⚠️ check | ⚠️ unclear, regulatory scrutiny | ❌ 2025 offensive-output incidents | Evals only (synthetic data) |
| **DeepSeek (hosted API)** | ⚠️ verify with evals | ⚠️ check strict schemas | ❌ | ❌ data processed in China | ⚠️ | Evals only (synthetic data) |
| **Self-hosted open weights** (Llama, Mistral, Qwen, DeepSeek) | ⚠️ varies by model | ⚠️ needs constrained decoding | ❌ separate STT/TTS | ✅ full control | ✅ | Option at 10×+ scale |

Grok and DeepSeek are ruled out for production on **data-protection and reputational grounds, not model quality**: this is a candidate-facing product run on a client's behalf.

### Cost at 10K conversations/week

Assumptions (estimates, to replace with measured `usage`): 10 turns per conversation, ~3K input and ~400 output tokens per turn (reasoning counts as output), 80% of input cached. That is 60M fresh input, 240M cached input and 40M output tokens a week.

| Provider | Model (tier) | Input / cached / output ($ per 1M) | ≈ $ / week | Notes |
| --- | --- | --- | --- | --- |
| Anthropic | `claude-opus-5-5` (top) | 4.00 / 0.20 / 20.00 | ~1,100 | Anthropic adapter's default; not evaluated yet |
| Anthropic | `claude-sonnet-5-5` (mid) | 2.00 / 0.20 / 10.00 | ~570 | |
| Anthropic | `claude-haiku-5-5` (small) | 0.10 / ? / 0.50 | ~30–50 | Cache-read price not confirmed |
| Google | `gemini-4-argon` (top) | 2.00 / 0.10 / 10.00 | ~540 | Doubles on 1 Dec 2026 → ~1,090 |
| Google | `gemini-3.8-flash` (mid) | 0.75 / 0.075 / 3.75 | ~210 | Doubles on 1 Jan 2027 → ~430 |
| Google | `gemini-3.5-flash-lite` (small) | 0.30 / 0.03 / 2.50 | ~125 | Best live run so far |
| Google | `gemini-3.1-flash-lite` (small) | 0.25 / 0.025 / 1.50 | ~80 | |
| OpenAI | GPT-5.6 Sol (high) | 5.00 / 0.50 / 30.00 | ~1,620 | Third-party source; verify |
| OpenAI | GPT-5.6 Terra (mid) | 2.00 / 0.20 / 12.00 | ~650 | Third-party source; verify |
| OpenAI | GPT-5.6 Luna (small) | 0.20 / 0.02 / 1.20 | ~65 | Third-party source; verify |

Every option is small next to 13 recruiters' time. Two details matter more than list prices:

- **Caching vs slow conversations.** Candidates reply over hours. Caches with an hourly storage fee (e.g. Gemini explicit caching) may cost more than they save; prefer implicit/automatic caching and check the cache-hit rate.
- **Output dominates cost.** Keep reasoning effort low for this task and measure real output tokens per turn.

Sources: Anthropic model table (Claude API reference bundled with Claude Code, cached 2026-10-06; verify on Anthropic's pricing page) · [Gemini API pricing (official, updated 2026-10-28)](https://ai.google.dev/gemini-api/docs/pricing) · [CloudZero: OpenAI API pricing 2026 (updated 2026-10-21)](https://www.cloudzero.com/blog/openai-pricing/). OpenAI's own pricing page could not be fetched, and third-party sources disagree on model names and prices.

### Latency and voice

- No published latency figures are quoted here on purpose: they depend on region, load and reasoning effort. The eval run measures **p50/p95 seconds per candidate turn with our real prompts**, including both LLM calls; results are under [where the choice stands](#where-the-choice-stands).
- Small and mid tiers are much faster than flagships, and they suit one short question per message.
- **Voice:** Google publishes per-minute prices for its live audio models (`gemini-3.8-live`: ~$0.005/min audio in, ~$0.018/min audio out, so ~$0.05 for a 4-minute screening). OpenAI's Realtime pricing was not confirmed. Both run their own conversation loop, so our engine would plug in as tools; the preferred voice design (push-to-talk → STT → `engine.turn()` → TTS) keeps the engine in control.

### Selection process

1. Build on the default (`claude-opus-5-5`, low effort, structured outputs via `client.messages.parse()`, prompt caching, server-side refusal fallbacks).
2. Through the OpenRouter eval adapter, run the same suite on one mid tier per shortlisted provider (`claude-sonnet-5-5`, `gemini-3.8-flash`, GPT-5.6 Terra), plus one small model each for extraction only (`claude-haiku-5-5`, Flash-Lite, Luna).
3. Rank by extraction and knockout accuracy, then p50/p95 latency, then cost (using post-increase prices).
4. Confirm the winner through its own SDK, record the results in `evals/baseline.json`, and present them as the justification for the choice.

### Where the choice stands

Steps 2 to 4 aren't finished, so no production model has been chosen. As of 2026-11-03:

- **No settings:** `LLM_PROVIDER=fake`, the offline mode.
- **Best live run:** `google/gemini-3.5-flash-lite` through OpenRouter. OpenRouter is for synthetic data only, and there's no direct Gemini adapter yet.
- **Intended path for real candidates:** the Anthropic adapter (default `claude-opus-5-5`). It's built, but no Claude model has been through the evals, and neither has any OpenAI model.

Results, all with synthetic candidates. Run files are in `evals/results/`, which is gitignored, so the numbers are copied here.

| Run | Date | Suite | Mode | Pass rate | Field accuracy | p50 / p95 s per turn | $ per conversation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Gemini 3.5 Flash-Lite, `low` (2 runs, closed answers skip the LLM) | 11-03 | 64 | scripted | 127/128 (64, 63 of 64) | 100% | 1.21–1.33 / 2.63–3.08 | 0.0078 |
| Gemini 3.5 Flash-Lite, `minimal` (2 runs, closed answers skip the LLM) | 11-03 | 64 | scripted | 125/128 (64, 61 of 64) | 99% | 1.34–1.36 / 3.06–3.09 | 0.0079 |
| Gemini 3.5 Flash-Lite, `low` (3 runs, before closed answers skipped the LLM) | 11-03 | 64 | scripted | 187/192 (62, 62, 63 of 64) | 99% | 2.02–2.19 / 4.03–4.22 | 0.0136 |
| Qwen 3.8 Flash extract, Qwen 3.7 Plus replies | 10-30 | 21 | scripted | 20/21 | 99% | 3.75 / 8.36 | 0.0395 |
| Qwen 3.7 Plus | 10-30 | 21 | scripted | 19/21 | 97% | 5.95 / 8.49 | 0.0570 |
| Mistral Large 4 | 10-30 | 21 | scripted | 8/21 | 68% | 13.0 / 19.53 | 0.0948 |
| Mistral Small extract, Mistral Large 4 replies | 10-30 | 21 | scripted | 6/21 | 67% | 4.25 / 8.49 | 0.0434 |
| DeepSeek V4.1 Flash extract, DeepSeek V4 Pro replies | 10-30 | 21 | scripted | 4/21 | 57% | 5.16 / 9.45 | 0.0512 |
| DeepSeek V4 Pro | 10-30 | 21 | scripted | 2/21 | 61% | 16.41 / 45.53 | 0.1286 |
| Qwen 3.7 Plus | 10-30 | 20 | simulated | 13/20 | 99% | 5.86 / 7.35 | 0.0840 |
| Mistral Large 4 | 10-30 | 20 | simulated | 9/20 | 83% | 12.87 / 20.43 | 0.1394 |
| DeepSeek V4 Pro | 10-30 | 20 | simulated | 3/20 | 71% | 20.16 / 45.81 | 0.1764 |

Don't read this as a ranking across dates. The Gemini runs used a bigger suite and a later version of the engine. The same model and code don't always give the same result: five different scenarios failed across the three earlier runs, and an earlier single run reached 64/64. The earlier three ran side by side and the later four in pairs, so the speed gain is approximate. `minimal` was neither faster nor as reliable as `low`, so `low` stays. The other rows are single runs. The 10-30 comparison only covered open-weight models, because the OpenRouter account was restricted from closed-weight ones at the time. A fair comparison means re-running every candidate, Claude included, on the current suite several times.

Models with a median turn over 5 s are out of consideration, so they won't be re-run. That rules out Qwen 3.7 Plus (5.95 s), the fallback named under resilience above, so a replacement still has to be chosen and pass the same evals.
