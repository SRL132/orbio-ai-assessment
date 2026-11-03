# Sazón Screening Agent

An AI screening agent and recruiter dashboard for **Grupo Sazón**, a (fictional) restaurant chain hiring delivery drivers across 45 locations in Spain and Mexico. Built for the Orbio FDE technical assignment ([brief](docs/requirements.md)).

**The problem:** about 1,000 applications a week, 13 recruiters phoning candidates, 60% of calls unanswered, and 80% of recruiter time spent on people who were never going to qualify.

**The product:** candidates are screened in a chat, in Spanish or English, whenever it suits them, and book their interview there too. Recruiters open a dashboard and only spend time on the qualified candidates and the cases the agent flagged for a human.

## Quick start

```bash
uv sync
cp .env.example .env                                     # LLM_PROVIDER=fake: no API key needed
uv run uvicorn --factory app.api.main:create_app --reload
```

- Candidate chat: http://127.0.0.1:8000/chat. The demo bar can start a new chat and fast-forward the clock (+2 h / +24 h) to show reminders.
- Voice call: http://127.0.0.1:8000/voice (Chrome or Edge). Press "Start call", then the button to talk. Replies are spoken by the browser, or by ElevenLabs if `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` are set.
- WhatsApp: candidates message your WhatsApp number and get the same screening, with reply buttons, and reminders on their phone. Setup below.
- Recruiter dashboard: http://127.0.0.1:8000/dashboard. Create a password hash with `uv run python -m app.cli hash-password`, then set `RECRUITER_USERS=you@example.com:<hash>` and `SESSION_SECRET` in `.env`.

The database schema is managed with Alembic migrations, which run automatically at startup. A demo database created before migrations existed is refused with a clear message: delete `data/sazon.db` once.

### WhatsApp (with Meta's test number)

1. Create an app at developers.facebook.com, add the **WhatsApp** product, and use the test number. Add your own phone as a recipient (up to 5).
2. In `.env`, set `WHATSAPP_ACCESS_TOKEN` (the temporary token from the WhatsApp setup page, or a system-user token), `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` (App settings → Basic) and a `WHATSAPP_VERIFY_TOKEN` of your choice.
3. Expose the app: `ngrok http 8000`. In WhatsApp → Configuration, set the callback URL to `https://<ngrok-host>/webhooks/whatsapp` with the same verify token, and subscribe to the `messages` field.
4. Send "hola" to the test number from your phone.

Replies and reminders go out through an outbox (`messages.delivered`), so a reminder created hours later reaches the phone like any reply. Since 1 October 2026 [Meta charges per delivered service message](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages), replies included, and needs a payment method on file; its docs don't say how that applies to test numbers. WhatsApp allows free-form messages only within 24 h of the candidate's last message, so on WhatsApp the reminders go out at 2 h and 23 h. Anything later needs an approved template (`WHATSAPP_REMINDER_TEMPLATE`, paid) or is skipped and logged.

### Docker

```bash
docker compose up --build          # web + worker + Postgres at http://localhost:8000
```

The image is multi-stage, runs as a non-root user with a health check, and applies migrations before starting. Settings come from `.env` if present.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `fake` | `fake` (offline keyword rules + templates), `openrouter`, or `anthropic` |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_REASONING_EFFORT` | —, —, `low` | Development and model comparison (synthetic data only); reasoning per call (`none`, `minimal`, `low`…), less is faster |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | `claude-opus-5-5` | The intended production path |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | — | Optional: the WhatsApp channel (all four needed) |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | — | Optional: speak voice replies with ElevenLabs instead of the browser |
| `GEOCODER`, `NOMINATIM_USER_AGENT`, `NOMINATIM_URL` | — | Optional: look up towns the config doesn't list (`nominatim`, free; needs a User-Agent with a contact) and decide by distance instead of rejecting them |
| `DATABASE_URL` | `sqlite:///./data/sazon.db` | Postgres works with the same code |
| `DEMO_MODE` | `true` | Enables the clock fast-forward in the chat |
| `PUBLIC_BASE_URL` | — | Public address of the app; makes the privacy link absolute (needed on WhatsApp) |
| `RECRUITER_USERS`, `SESSION_SECRET` | — | Dashboard login (no default accounts) |

## What it does

**Conversation** ([process design](docs/process-design.md)): consent and an automated-assistant disclosure first, then the seven screening questions, one per message, with the two knockouts (license, service area) right after the name. Answers can come in any order and several at once. The agent re-asks invalid or unclear answers with examples, and after two failed tries it moves on and flags the field. It follows Spanish ↔ English switches, answers job questions with approved FAQ text (the LLM picks the entry, code sends the words), offers a human when the candidate seems frustrated, and hands off right away when asked. Sensitive data such as an ID number is redacted. Reminders go out at 2 h and 24 h, then the candidate is marked abandoned. Qualified candidates book an interview slot in the chat. The same flow runs as a **voice call** (`/voice`): push-to-talk speech recognition in the browser, spoken replies (browser voice or ElevenLabs), wording adapted for speech.

**Outcomes:** qualified (plus a booked interview), disqualified (kindly, with an offer to keep their details), needs review (with the reason), or abandoned (with the drop-off point). Each comes with a short summary for the recruiter.

**Dashboard** ([spec](docs/recruiter-dashboard.md)): a filterable candidate list (including by channel: web chat, voice or WhatsApp) with counters, a review queue (oldest first, with reasons), a detail page (summary, each answer with the candidate's own words and a confidence score, transcript, timeline), status overrides with a required reason, notes, JSON export, and an analytics page (funnel, drop-off, completion rate, median time, by language and location). Every view and change is in the audit log.

## Architecture

A modular monolith with a lightweight hexagonal core ([architecture](docs/architecture.md)):

```
chat / webhooks ──▶ API ──▶ queue (debounced) ──▶ worker ──▶ ENGINE (pure) ──▶ state + events
                                                     │            │
                                         LLM, calendar, clock    policy + validators (Python)
                                         behind ports              rules from client config
dashboard ──▶ read model over the projected state; writes go back through the engine
```

- `app/domain/`: models, validators, policy, engine, summary, ports. No I/O and no SDKs; import-linter enforces this.
- `app/adapters/`: LLM (Anthropic SDK, OpenRouter, offline keyword rules), SQLAlchemy storage with an append-only event log, calendar, clocks, config loader.
- `app/worker/`: application service (a database lease per conversation, so any number of workers can run; merging message bursts; reminders) and the async runner.
- `app/api/`: FastAPI chat API, chat page, dashboard. `app/config/`: the Grupo Sazón config and ES/EN templates.

## Key design decisions

- **Code decides, the LLM talks.** The LLM only extracts answers into a strict schema and words the messages. Qualification, knockouts and every status change are plain Python, tested and logged. A prompt-injection eval checks that a candidate can't talk their way into "qualified".
- **Slot-filling, not a script.** The next question is the highest-priority field still missing, which is what lets candidates answer in any order.
- **No LLM call when code can read the answer.** A message that is exactly one of the current question's options ("sí", "Moto", "la segunda") skips extraction, and on voice a plain next question uses its template. Anything else goes to the LLM.
- **Rules in config, one engine for many clients.** Service areas, license rules, persona, templates and FAQ live in `app/config/grupo_sazon/` (`client.yaml` and per-language templates), and every table carries `client_id`.
- **Fixed texts for what matters legally:** consent, disqualification, recap and interview confirmation. Every LLM-written message passes an output check (one question, length), or the template is used instead.
- **No callback at the end.** Qualified candidates book the interview in the chat, because a callback would bring back the 60% no-answer problem at the last step.
- **Built to scale without a rewrite:** webhooks only store and acknowledge, one conversation is processed at a time, reminders live in the database, and the event log is the source of truth. See the "Demo vs production" and "Deployment and monitoring" sections of the architecture doc.
- **LLM choice is decided by evals, and the production model is still open.** Out of the box `LLM_PROVIDER=fake` needs no key. The best live run so far is `google/gemini-3.5-flash-lite` through OpenRouter, which is for synthetic data only. For real candidates, the Anthropic adapter (`claude-opus-5-5` at low effort, with structured outputs, prompt caching and refusal fallbacks) is built but hasn't been through the evals yet. The [provider comparison](docs/architecture.md#llm-provider) covers Anthropic, OpenAI, Google, Mistral, Grok and DeepSeek on quality, data protection, cost and latency, and [where the choice stands](docs/architecture.md#where-the-choice-stands) has the measured results.

## Quality and evals

([details](docs/quality.md)) ruff, mypy strict on all of `app/`, import-linter for the architecture boundary, 500+ tests (property tests included) with a coverage ratchet (≥ 97.9%), deptry, pip-audit, gitleaks, pre-commit and CI. Claude Code hooks format every edit and run the checks before a task is considered done.

The suite has 64 synthetic scenarios: cooperative ES/EN, several answers at once, vague location, both knockouts, borderline zones, human request, abuse, ID number, prompt injection, job and bot questions, interview booking, voice calls with speech-to-text-style input, and graded ladders for the four main edge cases (stops responding, invalid answers, ambiguous answers, switches language). `uv run python -m evals` runs the 55 that work offline; the other 9 need a real model to understand the candidate. Each run is scored on outcome, field accuracy, turns and the output check. The offline run is compared against `evals/baseline.json` in CI.

| Run | Pass rate | Field accuracy |
| --- | --- | --- |
| Offline (`fake/keyword/scripted`, in CI) | 55/55 | 100% |
| Live scripted, `google/gemini-3.5-flash-lite` (2026-11-03, 2 runs at `low` reasoning) | 127/128 (99%) | 100% |
| Live simulated, `qwen/qwen3.7-plus` (2026-10-30, older 21-scenario suite) | 13/20 | 99% |

The offline result shows the engine and policy behave as designed. It says little about extraction quality, because the scripted messages were written so simple keyword rules can parse them. The live runs are the real measure, with limits: results vary between runs (64 and 63 of 64 now; 62, 62 and 63 of 64 before closed answers skipped the LLM), and two runs are a small sample; the scripted messages use more closed answers than real candidates may, which flatters the speed and cost figures; the suite isn't held out from the fixes it prompted; and live scores aren't in the baseline, so nothing fails automatically if they drop.

[`samples/`](samples/README.md) has one transcript per scenario.

## Potential improvements

- **Finish the model choice:** run the current suite several times on each shortlisted model, including Claude through its own SDK, skipping models whose median turn is over 5 s. Pick the provider on accuracy, then latency, then cost, and record live baselines. Then tune the prompts against failures, with the LLM-simulated candidates as a harder test set (the last simulated runs used the older 21-scenario suite).
- **WhatsApp in production:** a permanent system-user token, approved reminder templates, a message-status webhook to track delivery and read receipts, and a business-verified number.
- **Production voice:** server-side speech-to-text (browser recognition only works in Chrome and Edge, and Chrome sends audio to Google), streaming replies for lower latency, and a phone or WhatsApp voice entry point.
- **Production hardening:** a real queue (database leases already make several workers safe, and Postgres and migrations are in place), SSO for recruiters, and retention jobs. Rate limits, CSRF tokens on dashboard forms and per-browser chat access are already in.
- **Real calendars and ATS:** replace the mock calendar with each location's calendar and build the [ATS integration](docs/ats-integration.md).
- **Smarter re-engagement:** reminder timing by time of day and channel, and A/B tests on reminder wording, measured with the drop-off analytics.
- **An admin area so recruiters can iterate quickly:** an editing layer over the client config, not free-text prompts. Recruiters change structured settings directly (FAQ answers, service areas, reminder timing, tone options), while knockout rules and consent or legal text need extra sign-off. Every change goes draft → automatic eval run → review what changed → publish, with rollback, and each conversation records the config version it ran under.

## Docs

[Vision](VISION.md) · [Brief](docs/requirements.md) · [Process design](docs/process-design.md) ([appendix](docs/process-design-appendix.md)) · [Dashboard spec](docs/recruiter-dashboard.md) · [Architecture](docs/architecture.md) · [Quality](docs/quality.md) · [ATS integration](docs/ats-integration.md) · [Demo script](docs/demo-script.md) · [Pilot plan](docs/pilot-plan.md)
