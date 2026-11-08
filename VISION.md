# Vision — Sazón Screening Agent

> Brief: [`docs/requirements.md`](docs/requirements.md) · Process design (Phase 1): [`docs/process-design.md`](docs/process-design.md) and [appendix](docs/process-design-appendix.md) · Recruiter tool: [`docs/recruiter-dashboard.md`](docs/recruiter-dashboard.md) · Architecture: [`docs/architecture.md`](docs/architecture.md) · Quality: [`docs/quality.md`](docs/quality.md)
>
> Where this vision and the process design disagree on details, the process design wins.

## The problem, in numbers

Grupo Sazón gets **~1,000 delivery-driver applications a week** across 45 locations in Spain and Mexico. Screening is done by **13 recruiters making ~15 phone calls a day each**: about 975 call attempts a week, which barely covers the volume on paper. In practice:

- **60% of candidates don't pick up**, so only ~390 screenings actually happen each week.
- **80% of recruiter time goes to people who were never going to qualify** (no license, outside the service area, wrong availability).

The bottleneck isn't recruiters' skill. A synchronous phone call is the wrong way to ask seven factual questions, and a human is the wrong person to ask them.

## The vision

**Every applicant is screened within minutes of applying, on their phone, whenever it suits them. Recruiters only talk to people who already qualify.**

We build the two things the brief expects, as one product recruiters can start using:

1. **A conversational screening agent.** It runs in a chat (WhatsApp-style), speaks Spanish and English, and collects and validates the seven screening fields through a short, friendly conversation. Candidates answer asynchronously, so a missed call no longer means a lost candidate.
2. **A recruiter dashboard.** A candidate list, a review queue for the cases the agent couldn't decide, and a detail view with the summary, each field with its source quote, and the transcript. Spec: [`docs/recruiter-dashboard.md`](docs/recruiter-dashboard.md).

## Bets and how we'll measure them

These are hypotheses, not results. The analytics view exists to test them.

| Today | Bet | Metric |
| --- | --- | --- |
| ~40% of candidates reached | Async chat reaches more candidates than calls do | Share of applicants screened within 24 h of applying; completion rate (started → final status) |
| Screening takes days, depends on recruiter capacity | Screening starts immediately and takes about 3–5 minutes | Median time to complete |
| 80% of recruiter time on unqualified candidates | Recruiters only see qualified candidates and real edge cases | Share of candidates that reach a recruiter (qualified + needs review) |
| Data lives in recruiters' notes | Every candidate has structured, checkable data | Extraction accuracy on the eval set |
| Unknown where candidates give up | We can see and fix the drop-off points | Drop-off per stage |

## Principles

- **Messaging, not email.** Short messages, one question at a time, a warm and casual tone. No walls of text.
- **Disqualify early and kindly.** Only two knockouts (no license, clearly outside the service area), asked first, always with a respectful message.
- **Code decides, the LLM talks.** The LLM handles dialogue and extraction into a strict schema. Qualification rules are deterministic Python, so they are auditable and testable, and the LLM never decides who is rejected.
- **Robust to real people.** Ambiguity gets a clarifying question, invalid answers a gentle re-ask, language switches are followed, silence gets a timed follow-up. Unexpected input never crashes the agent.
- **Recruiter trust.** Every decision can be traced to the transcript and the field that drove it, and recruiters can override it.

## Approach

A **modular monolith with a lightweight hexagonal core**. Full detail: [`docs/architecture.md`](docs/architecture.md).

- **A pure engine.** `turn(state, message, config) → (new state, actions)` holds all screening logic. The LLM, channels, database, calendar and clock sit behind ports, so tests and evals run on fakes and "code decides, the LLM talks" is enforced by structure.
- **Slot-filling, not a rigid script.** The next question is the highest-priority field still missing, so answers can arrive in any order.
- **Built to scale without a rewrite.** Webhooks only store and acknowledge. Workers process one conversation at a time. An append-only event log is the source of truth, and reminders are stored in the database. Clients are configuration (`client_id` everywhere). The demo is one process (FastAPI + SQLite, with the worker loop polling the database in-process); production swaps in Postgres and N workers, with a separate queue only if polling the database becomes the bottleneck.
- **Quality gates for AI-written code.** ruff, mypy strict on all of `app/`, import-linter for the architecture boundary, pytest, and an eval baseline that can only go up. See [`docs/quality.md`](docs/quality.md).

## Scope

**Core (must ship)**

- Phase 1 process design ([`docs/process-design.md`](docs/process-design.md))
- Python agent: full conversation history, structured JSON extraction, validation and re-ask, graceful error handling
- Persistent storage of conversations and extracted data
- Post-conversation summary
- Recruiter dashboard: list, review queue, detail view ([`docs/recruiter-dashboard.md`](docs/recruiter-dashboard.md))

**Build order** (technical build is 40% of the grade, bonus features 10%):

1. Core loop and validators, with unit tests
2. Recruiter dashboard
3. **Tests and evals:** simulated candidates (cooperative, vague, language-switcher, goes silent, abusive) scored on correct extraction, correct knockouts and turns to complete. These are the test suite, the demo material and the answer to "how do you know it works?"
4. **Multi-language (ES/EN) with code-switching.** Nearly free with this design.
5. **Re-engagement:** reminders when a candidate goes silent, aimed at the 60% no-answer problem.
6. **Guardrails:** on-script behaviour, inappropriate input, minimal personal data. Mostly comes from the intent classification in the core loop.
7. **Interview scheduling** with mocked slots. The qualified path in the process design books an interview in the chat.
8. **Analytics:** the metrics in the table above.
9. **Voice agent.** Done: `/voice` runs the same engine with browser speech recognition and spoken replies (browser voice or ElevenLabs). See [architecture → Voice channel](docs/architecture.md#voice-channel).

*Design only, if time remains:* ATS integration spec, deployment design for 10K candidates/week.

## Open decisions

- **LLM choice.** Decided by evals: GPT-5.6 Terra, with Claude Haiku 5.5 as the fallback. Still to confirm: the direct SDK path and OpenAI's EU data terms. See [where the choice stands](docs/architecture.md#where-the-choice-stands).
- **Service areas.** The brief doesn't list the 45 locations, so we invent a plausible set in config.
- **License rules.** One question in the conversation; detailed per-country rules in config ([appendix A](docs/process-design-appendix.md#a-license-rules-config-not-conversation)).
- **FAQ knowledge base.** The RAG bonus mentions a "provided FAQ", but none was attached. We wrote 14 demo entries, sent word for word; the client must approve them.
- **Regulatory claims.** Checked on 2026-11-03, each marked primary or secondary, and GDPR Art. 22 needs counsel ([appendix C](docs/process-design-appendix.md#c-regulatory-and-platform-context)).

## Deliverables checklist

- [x] Git repository with source code
- [x] Process design document (Phase 1): [`docs/process-design.md`](docs/process-design.md)
- [x] README: setup, architecture, key design decisions, potential improvements
- [x] Sample conversations ([`samples/`](samples/README.md), from the live model)
- [ ] Demo video (5–10 min): script in [`docs/demo-script.md`](docs/demo-script.md)
- [ ] 1h live presentation (product → architecture → discussion)
