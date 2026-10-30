# Demo Script

> For the 1 h live presentation ([structure in the brief](requirements.md#phase-3--presentation-1h-live)) and the 5–10 min video. Run everything live; keep the offline mode ready as a fallback if the network or the LLM is down.

## Before you start

- [ ] `.env` with the chosen provider and key, `RECRUITER_USERS`, `SESSION_SECRET`. Start from a fresh database (`rm data/sazon.db`).
- [ ] Two browser windows: the candidate chat on a phone-sized window (`/chat`), the dashboard on the other (`/dashboard`).
- [ ] Seed a few candidates for the dashboard: run 4–5 chats beforehand (one qualified with an interview, one disqualified, one borderline zone, one who asked for a human, one abandoned with "+24 h" twice).
- [ ] Terminal ready with `uv run python -m evals` and `uv run pytest -q`.
- [ ] Check the regulatory claims in [appendix C](process-design-appendix.md#c-regulatory-and-platform-context) before quoting any of them.

## Product (15 min)

1. **The problem in numbers (2 min).** 1,000 applications a week, 975 calls, 60% unanswered, so ~390 screenings; 80% of recruiter time on unqualified people. The phone call is the wrong tool for seven factual questions.
2. **Live chat, happy path in Spanish (4 min).** Consent and "virtual assistant" disclosure → name → license (show the buttons) → "Madrid" (it asks for the neighbourhood) → answer two things in one message (it skips ahead) → recap → book an interview slot in the chat.
3. **Edge cases (5 min), each in a new chat:**
   - "No tengo carnet": a kind knockout in under a minute, plus the keep-on-file question.
   - Switch to English mid-conversation: it follows.
   - "Quiero hablar con una persona": immediate hand-off.
   - Go silent: press "+2 h", "+24 h", "+24 h". Reminders, then abandoned.
4. **Voice call (2 min).** Open `/voice` in Chrome, press "Start call", and answer by voice: say the postal code out loud, then book the interview by saying "la segunda". Point out that it's the same engine and the same dashboard (filter by channel).
5. **Recruiter side (4 min).** Counters → review queue (reasons in plain words) → detail page (summary, the candidate's own words next to each value, transcript, timeline) → override with a reason (the original decision stays) → analytics funnel and drop-off.

Why this beats the status quo: no missed calls, recruiters only see qualified candidates and real edge cases, structured data ready for the ATS, and the interview is booked without a callback.

## Architecture (15 min)

1. **One turn (3 min):** message → extractor (LLM, strict schema) → Python validators and policy (rules from config) → responder (LLM, output check, template fallback). "Code decides, the LLM talks."
2. **Code tour (5 min):** `app/domain/engine.py` (`turn`), `policy.py` (slot-filling), `validators.py`, `ports.py`; the import-linter contract that keeps the domain pure; `app/config/grupo_sazon.yaml`.
3. **Edge cases in code (3 min):** debounced bursts, per-conversation lock, reminders stored in the database, redaction, double-booking constraint.
4. **LLM and stack choices (4 min):** the provider scorecard, cost at 10K a week, and why the choice is decided by evals and reversible behind a port. Show `uv run python -m evals` and explain the baseline ratchet.

## Discussion (10 min): be ready for

- **"What would you do with more time?"** Live evals and prompt tuning, the WhatsApp adapter, voice, Postgres with migrations, SSO, the real calendar and ATS integration.
- **"10K candidates a week?"** Swap in a real queue and Postgres and run N workers; the engine doesn't change. The limits are LLM rate limits and cost, not infrastructure.
- **"What would you do differently?"** Be honest: the offline keyword extractor makes the offline evals look perfect; only live evals measure extraction. Field editing in the dashboard was cut in favour of overrides plus notes.
- **"How do you know the AI-written code is right?"** The quality gates, the hexagonal boundary check, 180+ tests, and the eval suite. "I can walk you through any file."

## 5–10 min video

1. 0:00 Problem and product in one sentence each.
2. 0:45 Happy path in the chat, ending with the interview booking.
3. 3:00 Two edge cases: no license, and language switch or hand-off. Then 30 seconds of the voice call.
4. 4:30 Dashboard: review queue → detail → override → analytics.
5. 6:30 Architecture in one diagram, plus the evals run in the terminal.
6. 8:00 What's next.
