---
description: Set up and start the app locally (free, offline), then give the links and the login
---

Start this project for the person, from a fresh clone or an existing setup. It's free: the default
`.env.example` uses `LLM_PROVIDER=fake` (keyword rules, no API key, no network for the chat). Don't
change `.env` or switch to a paid LLM provider unless the person asks.

1. **uv:** run `uv --version`. If it's missing, stop and tell the person to install it from
   https://docs.astral.sh/uv/getting-started/installation/ (on macOS: `brew install uv`), then run
   `/start` again. Don't install it yourself.
2. **Already running?** If `curl -s http://127.0.0.1:8000/health` returns `{"status":"ok"}`, the app
   is up: skip to step 5. If something else holds port 8000, say so and ask before stopping it.
3. **Dependencies:** `uv sync` (it fetches Python 3.13 if needed).
4. **Start:** run `uv run python -m app.cli demo --no-prompt` **in the background**. On the first
   run it copies `.env.example` to `.env`, creates the login `tester@sazon.test` with a generated
   password saved in `data/demo-login.txt`, adds nine invented candidates and starts the server
   (no auto-reload). Wait until its output shows `Uvicorn running`, or report the error it printed.
5. **Hand over** these links:
   - Candidate chat: http://127.0.0.1:8000/chat (the demo bar starts a new chat and fast-forwards
     the clock to show reminders)
   - Voice call: http://127.0.0.1:8000/voice (Chrome or Edge)
   - Recruiter dashboard: http://127.0.0.1:8000/dashboard

   For the login, give the email and tell them the password is in `data/demo-login.txt` (they can
   run `! cat data/demo-login.txt`). Don't read the password out in the chat. If `.env` already had
   logins, `demo` lists them; their passwords are whatever was set when they were created.
6. Mention `uv run python -m app.cli demo --reset --no-prompt` for a fresh database (the old one is
   kept as a backup), and that a real LLM needs `LLM_PROVIDER=openrouter` plus a key in `.env`, which
   costs money.
