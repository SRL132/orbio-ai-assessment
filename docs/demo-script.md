# Demo Script

> For the 1 h live presentation ([structure in the brief](requirements.md#phase-3--presentation-1h-live)) and the 5–10 min video. Run everything live; keep the offline mode ready as a fallback if the network or the LLM is down.

## Before you start

- [ ] `.env` with `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL=openai/gpt-5.6-terra` (synthetic data, as the demo is), `LLM_FALLBACK_PROVIDER=openrouter`, `LLM_FALLBACK_MODEL=anthropic/claude-haiku-5.5`, `RECRUITER_USERS`, `SESSION_SECRET`. Start from a fresh database (`rm data/sazon.db`).
- [ ] WhatsApp isn't demoed: Meta has locked the new business account, so replies don't reach phones (error 131031). Save a screenshot of a `whatsapp_delivery_failed` line from the deployed app's logs, in case they ask for proof.
- [ ] Two browser windows: the candidate chat on a phone-sized window, the dashboard on the other (`/dashboard`). The deployed site is invite-only, so make one link per chat at `/dashboard/invite` and open each in a private window.
- [ ] Seed a few candidates for the dashboard: run 4–5 chats beforehand (one qualified with an interview, one disqualified, one borderline zone, one who asked for a human, one abandoned with "+24 h" twice). The time buttons only exist locally, so the abandoned one can't be seeded on the deployed site.
- [ ] Terminal ready with `uv run python -m evals` and `uv run pytest -q`.
- [ ] Check the regulatory claims in [appendix C](process-design-appendix.md#c-regulatory-and-platform-context) before quoting any of them.

## Product (15 min)

1. **The problem in numbers (1 min).** 1,000 applications a week, 975 calls, 60% unanswered, so ~390 screenings; 80% of recruiter time on unqualified people. If the agent removes half of that waste, about 208 recruiter hours a week for about $15 of LLM cost ([assumptions](pilot-plan.md#the-business-case)).
2. **Live chat, happy path in Spanish (4 min).** Consent and "virtual assistant" disclosure → name → license (show the buttons) → "Madrid" (it asks for the neighbourhood) → answer two things in one message (it skips ahead) → switch to English → recap → book an interview slot in the chat.
3. **Edge cases (3 min), each in a new chat:**
   - "No tengo carnet": a kind knockout in under a minute, plus the keep-on-file question.
   - "Quiero hablar con una persona": immediate hand-off.
   - Go silent, on localhost: press "+2 h", "+24 h", "+24 h". Reminders, then abandoned.
4. **Recruiter side (5 min).** Invite page (CSV in, signed links out: no ATS needed) → counters and the "asked for a human" filter → review queue (reasons in plain words) → detail page (summary, the candidate's own words next to each value, how to reach them, transcript, timeline) → override with a reason (the original decision stays) → the "unfinished applications" download → analytics funnel and drop-off.
5. **Voice (1 min).** Open a voice link in Chrome (tick "Make voice links" at `/dashboard/invite`) and answer one or two questions by voice. Call it what it is: a channel proof, push-to-talk with browser speech recognition, on the same engine and dashboard. It isn't a phone call: the measured latency is LLM time only, and there's no streaming or interruption handling. Be ready to say what a real-time call would change.
6. **WhatsApp (30 s, no demo).** Say it plainly: the channel is built on the same engine, with reply buttons, reminders and an outbox. Messages reach the app, but Meta refuses delivery because the new business account is locked (error 131031), so it can't be shown end to end. The block itself shows why the logging exists: Meta can accept a send and fail it later, and the app logs that failure with its code.

Why this beats the status quo: no missed calls, recruiters only see qualified candidates and real edge cases, structured data ready for the ATS, and the interview is booked without a callback.

## Architecture (15 min)

1. **One turn (3 min):** message → extractor (code for an exact closed answer, otherwise LLM with a strict schema) → Python validators and policy (rules from config) → responder (LLM, output check, template fallback). "Code decides, the LLM talks."
2. **Code tour (5 min):** `app/domain/engine.py` (`turn` and its stage handlers), `render.py`, `policy.py` (slot-filling), `validators.py`, `ports.py`; the import-linter contract that keeps the domain pure; `app/config/grupo_sazon/client.yaml`.
3. **Edge cases in code (3 min):** debounced bursts, per-conversation lock, reminders stored in the database, redaction, double-booking constraint.
4. **LLM and stack choices (4 min):** why GPT-5.6 Terra won (the results table), Haiku as fallback, about $153 a week at 10K, and why it's reversible behind a port. The demo reaches Terra through OpenRouter; production calls OpenAI directly. Tell the schema story: only Gemini could pass until it was fixed. Show `uv run python -m evals` and explain the baseline ratchet.

## Discussion (10 min): be ready for

- **"What would you do with more time?"** Run the pilot ([plan](pilot-plan.md)) and learn from real failures, confirm the direct SDK path and OpenAI's EU terms, then WhatsApp and voice in production, SSO, the real calendar and ATS integration.
  - **An admin area so recruiters can iterate quickly.** The rules already live in client config, so this is an editing layer over it, not a rewrite. Recruiters change structured settings directly (FAQ answers, service areas, reminder timing, tone options); knockout rules and consent or legal text need extra sign-off. Every change goes draft → automatic eval run → review what changed → publish, with rollback, and each conversation records the config version it ran under. No free-text prompt box: that would change the agent's behaviour without passing the evals, and could undermine "code decides, the LLM talks". Not built now because a half-finished version would weaken traceability, and one bad edit would reach 1,000 applications a week.
- **"10K candidates a week?"** Postgres and N workers, which find waiting messages in the database; the engine doesn't change. A separate queue only if polling becomes the bottleneck. The limits are LLM rate limits and cost, not infrastructure.
- **"What would you do differently?"** Be honest: the offline keyword extractor makes the offline evals look perfect; only live evals measure extraction. Field editing in the dashboard was cut in favour of overrides plus notes.
- **"Which model would you ship tomorrow?"** Terra, on the numbers in the architecture doc, with two conditions you can name: the direct SDK run and OpenAI's EU data terms.
- **"What's your accuracy on messy input?"** 13/14 on held-out scenarios written before seeing results, before the last fixes, and say what the failure was (now fixed, so that scenario no longer counts as held out). Then say what it doesn't tell you: they're still synthetic. Simulated runs score 34/57, mostly because the scenarios don't fit a candidate who carries on, so you'd want real data.
- **"You built Prometheus and mutation testing before choosing a model. Why that order?"** Have your honest answer ready. It was the wrong order, and it hid a real bug: only Gemini could pass the schema, which nobody saw until the comparison ran on every vendor.
- **"Why do the commits labelled days 0–5 share a date?"** Have your real answer ready.
- **"You built a lot. What would you cut, and what are you sure of?"** Pick three stories you can tell without notes, and be ready to walk through any file in `app/domain` and `app/worker`. Candidates: the schema bug that only showed when every vendor ran; the clean-clone crash that all the gates missed, because nothing loaded `.env.example`; and the promise to write back that recruiters couldn't keep until a review caught it. Cuts to name: Prometheus, mutation testing and the geocoder could all have waited for the first client.
- **"How do you know the AI-written code is right?"** The quality gates, the hexagonal boundary check, 860+ tests, and the eval suite. "I can walk you through any file."

## 5–10 min video

1. 0:00 Problem and product in one sentence each.
2. 0:45 Happy path in the chat, ending with the interview booking.
3. 3:00 Two edge cases: no license, and language switch or hand-off. Then 30 seconds of the voice call.
4. 4:30 Dashboard: review queue → detail → override → analytics.
5. 6:30 Architecture in one diagram, plus the evals run in the terminal.
6. 8:00 What's next.
