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
  → Extractor (LLM, strict output schema):
      fields found (value, quote, confidence), language, intent, sentiment, confused
  → validators (Python, rules from client config)
  → policy picks the next action:
      ask next missing field | re-ask | disqualify | hand off | recap | offer slots
  → Responder (LLM) writes the message in the persona and candidate's language
      — consent, disqualification and recap use fixed templates
  → output check (one question, length, no promises) → else fallback template
```

The flow is **slot-filling**, not a rigid state machine: the next question is the highest-priority field still missing, so answers can arrive in any order.

The extractor schema includes `language`, `intent`, `sentiment` and `confused` **from day one**, even before the policy uses all of them. That keeps the bonus features cheap.

### 3. Webhooks only store and acknowledge

The API normalizes input to `InboundMessage`, drops duplicates by channel message ID, persists, enqueues and returns 200 in milliseconds. All LLM work happens in workers.

### 4. One conversation at a time

A per-conversation lock (`SELECT … FOR UPDATE`, or a queue grouped by conversation ID), plus a ~2 s debounce that merges a burst of messages into one turn. That fixes ordering and saves LLM calls.

Storing an inbound message doesn't wait for that lock: a webhook must be acknowledged even while a turn is running. So message numbers come from a counter on the conversation row (`last_seq`), reserved with one atomic `UPDATE … RETURNING` that holds the row lock until commit. Concurrent writers to one conversation queue up instead of colliding, which reading `max(seq) + 1` didn't guarantee.

### 5. Append-only event log as source of truth

Events (`message_received`, `field_extracted`, `field_rejected`, `status_changed`, `reminder_sent`, `override`) are never edited. The `candidates` table is a projection of them. One mechanism gives the audit trail, analytics, ATS sync (outbox) and replay for debugging.

### 6. Reminders live in the database

A `next_reminder_at` column, polled by a scheduler that enqueues reminder turns. Nothing is lost on restart. The clock is a port, so the demo and tests can fast-forward time.

### 7. Clients are configuration

A client config holds locations and service areas, license rules per country, fields and their priority, persona, templates (by language and channel) and FAQ. Grupo Sazón is the first config, not hard-coded logic. `client_id` is a column on every table from the start.

### 8. LLM calls are hardened

Timeouts and retries with backoff; a templated fallback message so a failed call never looks like a crash; prompt caching for the system prompt and config; token usage and cost logged per turn. A smaller model can take over extraction if the evals show it's accurate enough.

## Hexagonal, but lightweight

| Port | Adapters (now → later) |
| --- | --- |
| `Channel` | web chat → WhatsApp, voice |
| `Extractor`, `Responder` | LLM → fake for tests, cheaper model |
| `Repository`, `EventLog` | SQLite → Postgres |
| `CalendarProvider` | mock slots → Google Calendar |
| `KnowledgeBase` | FAQ in prompt → embeddings |
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
    models.py        # Candidate, ConversationState, fields, statuses, actions
    policy.py        # next action, knockouts, outcomes
    validators.py    # field rules, read from config
    engine.py        # turn(state, msg, config, ports) -> (state, actions)
    ports.py         # Protocols: Extractor, Responder, Repository, EventLog,
                     #            CalendarProvider, KnowledgeBase, Clock, Channel
  adapters/
    llm/             # extractor + responder (real, fake); prompts/ as versioned files
    channels/        # web chat, (whatsapp, voice)
    storage/         # SQLAlchemy repository, event log, projections
    calendar/ kb/ clock/
  api/               # FastAPI: webhooks, chat endpoint, dashboard routes
  worker/            # turn processor, reminder scheduler
  config/            # client configs (grupo_sazon.yaml), templates/<lang>/<channel>/
  bootstrap.py       # wires adapters to ports
tests/               # unit, contract, recorded, smoke
evals/               # scenarios, simulated candidates, baseline.json
```

## Demo vs production

Same code, different implementations behind the ports.

| Part | Demo (one process) | 10K/week and beyond |
| --- | --- | --- |
| Queue | asyncio queue in the FastAPI process | Redis/SQS, N worker containers |
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
- **Services:** one image (`Dockerfile`; `docker-compose.yml` runs it locally with Postgres) deployed as three container services: `api` (webhooks, chat, dashboard), `worker` (turn processing, N replicas), `scheduler` (reminders, outbox dispatch). Stateless; scale `worker` on queue depth.
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
| Outbox backlog | ATS sync stuck | undelivered > 1 h |

Logs are structured and contain IDs, never message text or names (already true in the code). Traces follow one turn across API → queue → worker → LLM. A nightly job runs the live eval suite against production config and posts the scores.

**10K candidates a week:** ~15K messages a day, a few per second at peak. Two to four worker replicas cover LLM latency comfortably; Postgres load is trivial. The real limits are LLM rate limits and cost, so the provider choice and prompt caching matter more than infrastructure.

## How the bonus features plug in

| Bonus | Where it plugs in | Effort |
| --- | --- | --- |
| Analytics | SQL over the event log (funnel, drop-off, duration, outcomes) | Low |
| Re-engagement | Done: scheduler + reminder templates; on WhatsApp the outbox delivers reminders to the phone at 2 h and 23 h (inside the 24 h window) | Low |
| Multi-language | Extractor returns `language`; templates per language; stored values language-neutral, so code-switching is handled | Low |
| Sentiment | `sentiment` / `confused` from the extractor; the policy rephrases with options or offers a human. Never used for qualification | Low |
| Tests and evals | Pure engine + fake adapters; simulated candidates against the real engine | Low–Med |
| Guardrails | Input: `intent` drives the policy. Output: a Python check on every reply, with a template fallback. Data: personal data redacted before logging | Low–Med |
| RAG | `KnowledgeBase` port; a small FAQ goes in the cached prompt, embeddings only if it grows. Answers come only from the FAQ | Low–Med |
| Interview scheduling | New stage after the recap; `CalendarProvider` port; `OfferSlots` / `InterviewBooked` actions; a database constraint prevents double-booking. Booking is idempotent per candidate, so a turn that booked but failed to save can be retried (an external calendar can't join our transaction anyway) | Med |
| ATS integration design | Done: [`ats-integration.md`](ats-integration.md) (outbox → signed webhooks, REST API sketch, field mapping) | Low |
| Deployment design | Done: "Deployment and monitoring" above | Low |
| Voice | Done: a `/voice` page on the same engine. Push-to-talk in the browser (Web Speech API, Chrome/Edge) → text → `engine.turn()` → reply spoken by ElevenLabs when `ELEVENLABS_API_KEY` is set, else by the browser. Voice templates (no emoji or "·" lists, options as sentences, spoken dates), a speech-aware prompt note, spoken-number parsing, no debounce. See "Voice channel" below | Done |

## WhatsApp channel

The production channel, built on the WhatsApp Cloud API (Meta). It uses the same engine; only the edges differ.

- **Inbound:** `GET /webhooks/whatsapp` answers Meta's verification check. `POST /webhooks/whatsapp` checks the `X-Hub-Signature-256` HMAC with the app secret, stores the message (deduplicated by WhatsApp message id, since Meta retries for up to 36 h), acknowledges, and hands the conversation to the normal debounce. A new number starts a conversation; "hola" after a finished one starts a new one.
- **Outbound, through an outbox:** agent replies on external channels are stored as undelivered messages, and a dispatcher sends them in order every second. Up to 3 options become reply buttons, longer lists a list message. Errors are retried (429 and 5xx) and given up after 5 attempts. Because reminders are just outbound messages, re-engagement now reaches the candidate's phone.
- **The 24-hour rule:** free-form messages are only allowed within 24 h of the candidate's last message. The client config schedules WhatsApp reminders at 2 h and 23 h; later messages need an approved (paid) template or are skipped and logged.
- **Demo:** Meta's free test number (up to 5 recipients) and an ngrok tunnel for the webhook. Code: `app/adapters/channels/whatsapp.py`, `app/worker/whatsapp.py`, `app/api/whatsapp.py`.

## Voice channel

Voice is a channel, not a second agent: the conversation has `channel: voice`, and everything else (engine, validators, policy, storage, dashboard, evals) is shared.

- **Listening:** the browser's speech recognition turns one push-to-talk utterance into text, which goes through the normal message API. Voice messages skip the debounce, since each one is already a complete utterance.
- **Speaking:** replies are read by ElevenLabs (`POST /api/tts`, server-side, only the agent's text) when configured, otherwise by the browser's `speechSynthesis`.
- **Wording:** `app/config/templates/<lang>/voice.yaml` overrides only what doesn't work aloud. The responder is told its message will be read aloud, and the output check rejects emoji and "·" lists for voice.
- **Understanding:** the extractor is told the text is speech-to-text output (no punctuation, spelled-out numbers, misheard names). The offline extractor converts spoken numbers ("veintiocho cero uno cero" → 28010).

Limits and the production path: browser speech recognition only works in Chrome and Edge, and Chrome sends the audio to Google for recognition. That's acceptable for a demo but not for candidates' data under GDPR, so production would use server-side speech-to-text (e.g. ElevenLabs or another provider under the client's data-processing agreement) and the WhatsApp or phone channel instead of a web page. The engine wouldn't change.

## LLM provider

> **Status: provisional.** The final choice is made by the eval suite (accuracy → p50/p95 latency → cost), not by reputation. Prices and capabilities below change often: verify against each provider's official pricing page before quoting them.

### Approach

- **One provider in production, through its own SDK**, behind the `Extractor` / `Responder` ports. The app relies on provider-specific features (prompt caching, per-turn system instructions, strict structured outputs), and it handles candidates' personal data, so fewer intermediaries is better.
- **The port is provider-neutral.** The domain only sees our Pydantic types (`ExtractionResult`, `Reply`), never SDK types, so a second provider is just a second adapter.
- **No routing library (e.g. LiteLLM) in the product.** It's another layer to explain, and it tends to hide provider-specific features.
- **OpenRouter only for model comparison in evals.** One `OpenRouterLLM` adapter runs the same simulated candidates against several providers. It adds roughly a ~5% fee on credits and tens of milliseconds per call, which is negligible for evals. Confirm the winner through its own SDK before locking the baseline, since features differ through the proxy.
- **Offline mode for CI and demos without a key.** `LLM_PROVIDER=fake` uses a rule-based `KeywordExtractor` (Spanish/English keywords, read in the context of the question just asked) and template replies. It is not a production path.
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
| Anthropic | `claude-opus-5-5` (top) | 4.00 / 0.20 / 20.00 | ~1,100 | Current default |
| Anthropic | `claude-sonnet-5-5` (mid) | 2.00 / 0.20 / 10.00 | ~570 | |
| Anthropic | `claude-haiku-5-5` (small) | 0.10 / ? / 0.50 | ~30–50 | Cache-read price not confirmed |
| Google | `gemini-4-argon` (top) | 2.00 / 0.10 / 10.00 | ~540 | Doubles on 1 Dec 2026 → ~1,090 |
| Google | `gemini-3.8-flash` (mid) | 0.75 / 0.075 / 3.75 | ~210 | Doubles on 1 Jan 2027 → ~430 |
| Google | `gemini-3.5-flash-lite` (small) | 0.30 / 0.03 / 2.50 | ~125 | |
| Google | `gemini-3.1-flash-lite` (small) | 0.25 / 0.025 / 1.50 | ~80 | |
| OpenAI | GPT-5.6 Sol (high) | 5.00 / 0.50 / 30.00 | ~1,620 | Third-party source; verify |
| OpenAI | GPT-5.6 Terra (mid) | 2.00 / 0.20 / 12.00 | ~650 | Third-party source; verify |
| OpenAI | GPT-5.6 Luna (small) | 0.20 / 0.02 / 1.20 | ~65 | Third-party source; verify |

Every option is small next to 13 recruiters' time. Two details matter more than list prices:

- **Caching vs slow conversations.** Candidates reply over hours. Caches with an hourly storage fee (e.g. Gemini explicit caching) may cost more than they save; prefer implicit/automatic caching and check the cache-hit rate.
- **Output dominates cost.** Keep reasoning effort low for this task and measure real output tokens per turn.

Sources: Anthropic model table (Claude API reference bundled with Claude Code, cached 2026-10-06; verify on Anthropic's pricing page) · [Gemini API pricing (official, updated 2026-10-28)](https://ai.google.dev/gemini-api/docs/pricing) · [CloudZero: OpenAI API pricing 2026 (updated 2026-10-21)](https://www.cloudzero.com/blog/openai-pricing/). OpenAI's own pricing page could not be fetched, and third-party sources disagree on model names and prices.

### Latency and voice

- No latency figures are quoted here on purpose: they depend on region, load and reasoning effort. The eval run measures **p50/p95 time-to-first-token from an EU region with our real prompts**.
- Small and mid tiers are much faster than flagships, and they suit one short question per message.
- **Voice:** Google publishes per-minute prices for its live audio models (`gemini-3.8-live`: ~$0.005/min audio in, ~$0.018/min audio out, so ~$0.05 for a 4-minute screening). OpenAI's Realtime pricing was not confirmed. Both run their own conversation loop, so our engine would plug in as tools; the preferred voice design (push-to-talk → STT → `engine.turn()` → TTS) keeps the engine in control.

### Selection process

1. Build on the default (`claude-opus-5-5`, low effort, structured outputs via `client.messages.parse()`, prompt caching, server-side refusal fallbacks).
2. Through the OpenRouter eval adapter, run the same suite on one mid tier per shortlisted provider (`claude-sonnet-5-5`, `gemini-3.8-flash`, GPT-5.6 Terra), plus one small model each for extraction only (`claude-haiku-5-5`, Flash-Lite, Luna).
3. Rank by extraction and knockout accuracy, then p50/p95 latency, then cost (using post-increase prices).
4. Confirm the winner through its own SDK, record the results in `evals/baseline.json`, and present them as the justification for the choice.
