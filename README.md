# Sazón Screening Agent

An AI screening agent and recruiter dashboard for **Grupo Sazón**, a (fictional) restaurant chain hiring delivery drivers across 45 locations in Spain and Mexico. Built for the Orbio FDE technical assignment ([brief](docs/requirements.md)).

**The problem:** about 1,000 applications a week, 13 recruiters phoning candidates, 60% of calls unanswered, and 80% of recruiter time spent on people who were never going to qualify.

**The product:** candidates are screened in a chat, in Spanish or English, whenever it suits them, and book their interview there too. Recruiters open a dashboard and only spend time on the qualified candidates and the cases the agent flagged for a human.

## Live demo

https://grupo-sazon-screening.onrender.com, on Render's free tier with Neon Postgres. The first visit after a quiet spell can take about a minute while the server wakes up.

It's invite-only, so a chat or voice call needs a signed link made on the recruiter dashboard. The clock fast-forward is off there, so run that locally. WhatsApp replies are blocked for now: messages reach the app, but Meta refuses delivery because the new business account is locked (error 131031).

## Quick start

```bash
uv sync
uv run python -m app.cli demo
```

The first time, it asks for a dashboard password (Enter generates one), so run it in a real terminal. It then copies `.env.example` to `.env`, adds the login `tester@sazon.test`, puts nine invented candidates on the dashboard and starts the app, printing the links. Run it again to start the app again: steps already done are skipped. `demo --reset` moves the database aside (to `data/sazon-backup-<time>.db`, nothing is deleted) and starts empty, which also clears old chats, reminders and login lockouts; add `--no-seed` for a dashboard with no invented candidates. Stop the app with Ctrl+C.

With [Claude Code](https://claude.com/claude-code), type `/start` in the project instead: it checks `uv`, installs the dependencies and runs `demo --no-prompt` in the background, which generates the password and saves it to `data/demo-login.txt` (git-ignored) instead of asking for one.

It runs without auto-reload, because `--reload` waits for open chat tabs to close and hangs. When changing code, use `uv run uvicorn --factory app.api.main:create_app --reload --timeout-graceful-shutdown 2` instead.

That runs without an LLM, on keyword rules, and the chat says so. For the demo with a real LLM, set `LLM_PROVIDER=openrouter` and `OPENROUTER_API_KEY` in `.env`: it runs on GPT-5.6 Terra, for synthetic data only. If OpenRouter can't be reached, the chat doesn't get stuck: after 8 s (`LLM_TIMEOUT_SECONDS`) and one retry, the keyword mode answers (`LLM_FALLBACK_PROVIDER=fake`). Production uses the same model through OpenAI's own API, with Claude Haiku 5.5 as the fallback: those are the Docker image's defaults, and it won't start without `OPENAI_API_KEY` and `ANTHROPIC_API_KEY`.

- Candidate chat: http://127.0.0.1:8000/chat. The demo bar can start a new chat and fast-forward the clock (+2 h / +24 h) to show reminders.
- Voice call: http://127.0.0.1:8000/voice (Chrome or Edge). Press "Start call", then the button to talk. Replies are spoken by the browser, or by ElevenLabs if `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` are set.
- WhatsApp: candidates message your WhatsApp number and get the same screening, with reply buttons, and reminders on their phone. Setup below.
- Recruiter dashboard: http://127.0.0.1:8000/dashboard. `demo` sets up a login the first time. To add another or change a password, run `uv run python -m app.cli create-user [email]` in a terminal and type a password, or press Enter to get a generated one: it creates `.env` if needed, sets `SESSION_SECRET` and adds the login (`tester@sazon.test` if no email is given; `DEMO_MODE=true` only), then start or restart the app. For a real account, create a password hash with `uv run python -m app.cli hash-password`, then set `RECRUITER_USERS=you@example.com:<hash>` and `SESSION_SECRET` in `.env`.

The invented candidates come from `uv run python -m app.cli seed-demo`, which `demo` runs on an empty database; it can also be run alone, with the app stopped. It plays nine invented chats through the real engine: qualified (one with an interview booked), disqualified, needs review, abandoned and in progress. It's free and offline, whatever `LLM_PROVIDER` says, so their replies are the keyword mode's templates. It only fills an empty database, and refuses unless `DEMO_MODE=true` and `INVITE_ONLY=false`.

The database schema is managed with Alembic migrations, which run automatically at startup. A demo database created before migrations existed is refused with a clear message: delete `data/sazon.db` once.

### WhatsApp (with Meta's test number)

1. Create an app at developers.facebook.com, add the **WhatsApp** product, and use the test number. Add your own phone as a recipient (up to 5).
2. In `.env`, set `WHATSAPP_ACCESS_TOKEN` (the temporary token from the WhatsApp setup page, or a system-user token), `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET` (App settings → Basic) and a `WHATSAPP_VERIFY_TOKEN` of your choice.
3. Expose the app: `ngrok http 8000`. In WhatsApp → Configuration, set the callback URL to `https://<ngrok-host>/webhooks/whatsapp` with the same verify token, and subscribe to the `messages` field.
4. Send "hola" to the test number from your phone.
5. Optional, once replies arrive: set `WHATSAPP_INVITE_NUMBER` to the same number. `/dashboard/invite` then makes `wa.me` links instead of web chat links, each with a signed code already typed in. The candidate presses send, Lucía answers, and the number is tied to that application. A code works once and lasts 7 days. A new number with no valid code gets no conversation and no reply, because replies cost money; a candidate already in a conversation just carries on.

Replies and reminders go out through an outbox (`messages.delivered`), so a reminder created hours later reaches the phone like any reply. `delivered` only means Meta accepted the send. Meta reports what happened next in a webhook, and a failure is logged as `whatsapp_delivery_failed` with its code and title. Since 1 October 2026 [Meta charges per delivered service message](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages), replies included, and needs a payment method on file; its docs don't say how that applies to test numbers. WhatsApp allows free-form messages only within 24 h of the candidate's last message, so on WhatsApp the reminders go out at 2 h and 23 h. Anything later needs an approved template (`WHATSAPP_REMINDER_TEMPLATE`, paid) or is skipped and logged.

### A second client

`CLIENT_ID=norte_express` runs the same app as an invented courier company, to show that a new client is config and no code ([details](docs/architecture.md#adding-a-client)).

### Docker

```bash
docker compose up --build          # web + worker + Postgres at http://localhost:8000
```

The image is multi-stage, runs as a non-root user with a health check, and applies migrations before starting. Settings come from `.env` if present.

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `fake` (Docker image: `openai`) | `fake` (offline keyword rules + templates), `openai` (the production choice), `anthropic`, or `openrouter` (demo and evals, synthetic data only) |
| `LLM_FALLBACK_PROVIDER`, `LLM_FALLBACK_MODEL` | none (`.env.example`: `fake`; Docker image: `anthropic`, `claude-haiku-5-5`) | Used when the primary is down |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | `20` (`.env.example`: `8`), `1` | Seconds per LLM call before it counts as failed, and retries per call before the fallback |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_REASONING_EFFORT` | —, `openai/gpt-5.6-terra`, `low` | The demo and model comparison (synthetic data only); reasoning per call (`none`, `minimal`, `low`…), less is faster |
| `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_REASONING_EFFORT` | —, `gpt-5.6-terra`, `low` | The production choice |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | —, `claude-haiku-5-5` | The fallback, with `LLM_FALLBACK_PROVIDER=anthropic` |
| `ATS_TOKEN` | — | Optional: enables `POST /api/v1/applications`, which returns a chat link |
| `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | — | Optional: the WhatsApp channel (all four needed) |
| `WHATSAPP_INVITE_NUMBER` | — | Optional: turns recruiter invites into WhatsApp links with a code. Leave it empty until replies reach phones |
| `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, `ELEVENLABS_MODEL` | — | Optional: speak voice replies with ElevenLabs instead of the browser |
| `GEOCODER`, `NOMINATIM_USER_AGENT`, `NOMINATIM_URL` | `nominatim` | Look up towns the config doesn't list (free; only the place name is sent) and decide by distance instead of rejecting them. Leave `GEOCODER` empty to turn it off. On a real deployment, set a User-Agent with your own contact |
| `METRICS_TOKEN` | — | Requires a bearer token on `/metrics` |
| `METRICS_PORT` | — | Worker processes: serve metrics on this port, with no token. Keep it on a private network |
| `LLM_CAPTURE_OUTPUTS` | `false` | Keep the LLM's raw replies to debug extraction, read with `python -m app.cli llm-outputs <id>`. They hold personal data, so synthetic data only for now |
| `DATABASE_URL` | `sqlite:///./data/sazon.db` | Postgres works with the same code |
| `DEMO_MODE` | `true` (Docker image: `false`) | Enables the clock fast-forward in the chat, and turns off reminder quiet hours so the fast-forward always shows a reminder. Keep it off in production |
| `CLIENT_IP_HEADER` | — | Behind a proxy, the header it always overwrites with the caller's address, used by rate limits (Render: `True-Client-IP`). Leave it empty with no proxy, or callers can fake their address |
| `INVITE_ONLY` | `false` | Set it on anything public. Only a signed link, a WhatsApp code or the ATS can start a conversation, so `/chat` and `/voice` start nothing on their own, a WhatsApp message from a new number is ignored, and the fast-forward is off |
| `PUBLIC_BASE_URL` | — | Public address of the app, used by invite links and the privacy link. Leave it empty locally: with `DEMO_MODE=true`, invite links use the address you opened, such as `localhost` |
| `RECRUITER_USERS`, `SESSION_SECRET` | — | Dashboard login (no default accounts) |

## What it does

**Conversation** ([process design](docs/process-design.md)): consent and an automated-assistant disclosure first, then the seven screening questions, one per message, with the two knockouts (license, service area) right after the name. Answers can come in any order and several at once. The agent re-asks invalid or unclear answers with examples, and after two failed tries it moves on and flags the field. It follows Spanish ↔ English switches, answers job questions with approved FAQ text (the LLM picks the entry, code sends the words), offers a human when the candidate seems frustrated, and hands off right away when asked. A license "no" in the candidate's own words is read back before it rejects anyone. Sensitive data such as an ID number is redacted. Reminders go out at 2 h and 24 h, waiting for the morning at night, then the candidate is marked abandoned. Qualified candidates book an interview slot in the chat. The same flow runs as a **voice call** (`/voice`): push-to-talk speech recognition in the browser, spoken replies (browser voice or ElevenLabs), wording adapted for speech.

**How a conversation starts:** a recruiter or the ATS sends the candidate a signed link, and the candidate opens it. The agent doesn't message anyone first. With `WHATSAPP_INVITE_NUMBER` set, the link opens WhatsApp with a code ready to send, so the candidate writes first and no paid template is needed to begin.

**Outcomes:** qualified (plus a booked interview), disqualified (kindly, with an offer to keep their details), needs review (with the reason), or abandoned (with the drop-off point). Each comes with a short summary for the recruiter.

**Dashboard** ([spec](docs/recruiter-dashboard.md)): an invite page that turns a CSV of applications into signed chat links, so a pilot needs no ATS; a filterable candidate list (including by channel, and rejected candidates who asked for a human) with counters; a review queue (oldest first, with reasons), a detail page (summary, each answer with the candidate's own words and a confidence score, transcript, timeline), status overrides with a required reason, notes, JSON export, and an analytics page (funnel, drop-off, completion rate, median time, by language and location). Every view and change is in the audit log.

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
- `app/adapters/`: LLM (OpenAI and Anthropic SDKs, OpenRouter, offline keyword rules), SQLAlchemy storage with an append-only event log, calendar, clocks, config loader.
- `app/worker/`: application service (a database lease per conversation, so any number of workers can run; merging message bursts; reminders) and the async runner.
- `app/api/`: FastAPI chat API, chat page, dashboard. `app/config/`: the Grupo Sazón config and ES/EN templates.

## Key design decisions

- **Code decides, the LLM talks.** The LLM only extracts answers into a strict schema and words the messages. Qualification, knockouts and every status change are plain Python, tested and logged. A prompt-injection eval checks that a candidate can't talk their way into "qualified".
- **Slot-filling, not a script.** The next question is the highest-priority field still missing, which is what lets candidates answer in any order.
- **No LLM call when code can read the answer.** A message that is exactly one of the current question's options ("sí", "Moto", "la segunda") skips extraction, and on voice a plain next question uses its template. Anything else goes to the LLM.
- **Rules in config, one engine for many clients.** Service areas, license rules, persona, templates and FAQ live in `app/config/grupo_sazon/` (`client.yaml` and per-language templates), and every table carries `client_id`.
- **Fixed texts for what matters legally:** consent, disqualification, recap and interview confirmation. Every LLM-written message passes an output check (one question, length, and a word list for pay, hiring decisions, being human and protected traits), or the template is used instead.
- **No callback at the end.** Qualified candidates book the interview in the chat, because a callback would bring back the 60% no-answer problem at the last step.
- **Built to scale without a rewrite:** webhooks only store and acknowledge, one conversation is processed at a time, reminders live in the database, and the event log is the source of truth. See the "Demo vs production" and "Deployment and monitoring" sections of the architecture doc.
- **GPT-5.6 Terra, chosen by evals.** It tied for the best accuracy and was faster and cheaper than the model it tied with ([results](docs/architecture.md#where-the-choice-stands)). Claude Haiku 5.5 is the fallback: a different vendor, and about 6x cheaper, though less accurate. Data-protection conditions and one untested step are listed there: the direct OpenAI and Anthropic adapters haven't reached the real APIs yet, because the runs went through OpenRouter. Out of the box `LLM_PROVIDER=fake` needs no key, and the chat says so on screen.

## Quality and evals

([details](docs/quality.md)) ruff, mypy strict on all of `app/`, import-linter for the architecture boundary, 1,100+ tests (property tests included) with a coverage ratchet (≥ 98%), deptry, pip-audit, gitleaks, pre-commit and CI. Claude Code hooks format every edit and run the checks before a task is considered done.

The suite has 99 synthetic scenarios, plus 14 from the held-out run (`evals/holdout.yaml`): cooperative ES/EN, several answers at once, vague location, both knockouts, borderline zones, human request, abuse, ID number, prompt injection, job and bot questions, interview booking, voice calls with speech-to-text-style input, and graded ladders for the four main edge cases (stops responding, invalid answers, ambiguous answers, switches language). `uv run python -m evals` runs the 76 that work offline; the other 23 need a real model to understand the candidate. Each run is scored on outcome, field accuracy, turns and the output check. The offline run is compared against `evals/baseline.json` in CI.

| Run | Pass rate | Field accuracy |
| --- | --- | --- |
| Offline (`fake/keyword/scripted`, in CI) | 76/76 | 100% |
| Live scripted, GPT-5.6 Terra, latest 2 runs (extractor v12, 6 Nov) | 99/99 both times | 100% |
| Live scripted, GPT-5.6 Terra, the 4 runs before them (83/83 on 4 Nov, 81/83 on 5 Nov, 96/97 and 98/99 on 6 Nov) | 358/362 | 99–100% |
| Live scripted, GPT-5.6 Terra, 3 earlier runs on smaller suites (68, 69 and 72 scenarios, 4 Nov) | 209/209 | 100% |
| Live, held-out messy scenarios, GPT-5.6 Terra (run once, before the last prompt fixes) | 13/14 | 93% |
| Live simulated, GPT-5.6 Terra (the model comparison, 4 Nov, on earlier prompts and 57 scenarios) | 34/57 | 96% |

The offline result shows the engine and policy behave as designed. It says little about extraction quality, because the scripted messages were written so simple keyword rules can parse them.

The live scores have limits. The scripted suite isn't held out from the fixes it prompted, so 99/99 is a floor check rather than a prediction, and it's two clean runs in a row on the same prompts. The three runs before them each failed something different. The last two failures are fixed: "el lunes" said on a Monday by a prompt rule, and "tengo 15 años de experiencia" read as an age by a code rule. Expect an occasional miss. The holdout is the fairer number. Its one failure (a neighbourhood lost when the city came next) has since been fixed, so measuring again needs new held-out scenarios. Most simulated failures are the scenario, not the agent: many check a mid-conversation state, and a simulated candidate just carries on to the end. `evals/baseline.json` keeps an older live entry (83/83, 4 Nov) for reference; the history of every run is in `evals/history.jsonl`. Only the offline run fails CI, because live runs cost money and vary.

A live end-to-end run still found two bugs the scripted suite had passed (an invented schedule from "lo mismo que te dije", and a nearby FAQ entry sent for an uncovered question). Both are fixed and now have scenarios, but expect more of this kind with real candidates. That's why the [pilot plan](docs/pilot-plan.md) has a recruiter read every knockout, at least until counsel has answered on GDPR Art. 22.

[`samples/`](samples/README.md) has one transcript for each of the 99 scenarios, from the latest live run on the current prompts.

## Potential improvements

- **Confirm the model on its own SDK:** run the suite through the direct OpenAI adapter, and Haiku through the Anthropic one, then settle OpenAI's EU data residency and the data processing agreement.
- **Real user data over more synthetic data:** after the pilot, turn real failures into new synthetic scenarios ([design](docs/real-data-evals.md)), and tune prompts against a held-out set.
- **WhatsApp in production:** a permanent system-user token, approved reminder templates, a template the ATS can trigger so the agent writes first, delivery and read tracking beyond the failure reports (a failure already sends an open screening to review), and a business-verified number.
- **Production voice:** server-side speech-to-text (browser recognition only works in Chrome and Edge, and Chrome sends audio to Google), streaming replies for lower latency, and a phone or WhatsApp voice entry point.
- **Production hardening:** a separate queue only if polling the database becomes the bottleneck (database leases already make several workers safe, and Postgres and migrations are in place), SSO for recruiters, and retention periods for qualified, needs-review and on-file candidates once counsel sets them. Rate limits, CSRF tokens on dashboard forms, per-browser chat access and the retention job for disqualified and abandoned candidates are already in.
- **Real calendars and ATS:** replace the mock calendar with each location's calendar and build the [ATS integration](docs/ats-integration.md).
- **Smarter re-engagement:** reminder timing tuned beyond the quiet hours, and A/B tests on reminder wording, measured with the drop-off analytics.
- **An admin area so recruiters can iterate quickly:** an editing layer over the client config, not free-text prompts. Recruiters change structured settings directly (FAQ answers, service areas, reminder timing, tone options), while knockout rules and consent or legal text need extra sign-off. Every change goes draft → automatic eval run → review what changed → publish, with rollback, and each conversation records the config version it ran under.

## Docs

[Vision](VISION.md) · [Brief](docs/requirements.md) · [Process design](docs/process-design.md) ([appendix](docs/process-design-appendix.md)) · [Dashboard spec](docs/recruiter-dashboard.md) · [Architecture](docs/architecture.md) · [Quality](docs/quality.md) · [Failure modes](docs/failure-modes.md) · [ATS integration](docs/ats-integration.md) · [Demo script](docs/demo-script.md) · [Pilot plan](docs/pilot-plan.md) · [Real conversations into evals](docs/real-data-evals.md)
