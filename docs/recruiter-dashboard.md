# Recruiter Dashboard — Spec

> One of the two things the brief expects ("a tool for the recruiters to keep track of the candidates"). Conversation side: [`process-design.md`](process-design.md).

## Goal

A recruiter opens the dashboard in the morning and knows within a minute who to interview, who needs a human decision, and what the agent did with everyone else. The agent does the screening; the dashboard is where recruiters act on the result.

## Users and jobs

| Recruiter wants to… | Dashboard gives them |
| --- | --- |
| See who to interview | Qualified candidates first, with their booked interview slot |
| Resolve the cases the agent couldn't | A review queue, each item with the reason it is there |
| Trust an extraction | Every field shows the candidate's own words it came from |
| Find candidates for a specific need | Filters: location, availability, schedule, start date |
| Fix a mistake | Change the status with a required reason, or add a note; both logged |

## Views

### 1. Candidate list (home)

- One row per candidate: name, location / zone, status, availability, schedule, start date, last activity.
- **Status** values: `in_progress`, `qualified`, `needs_review`, `disqualified`, `abandoned`.
- Default sort: `needs_review` and `qualified` first, then most recent activity.
- Filters: status, location, availability, schedule, start date range, language.
- Text search by name.
- 50 per page; every filter runs in SQL, so the total and the pages agree.
- Counters at the top: new today, qualified, waiting for review.

### 2. Review queue

- The `needs_review` candidates, oldest first, so nobody waits too long.
- Each item shows its **reason**: `human_requested`, `borderline_zone`, `field_failed_twice:<field>`, `ungrounded_answer:<field>` (the extracted value isn't in the candidate's words), `license_unclear`, `flagged_behaviour`.
- Actions: mark qualified, mark disqualified (reason required), or contact the candidate.

### 3. Candidate detail

- **Summary:** the post-conversation summary (3–5 lines) at the top.
- **Fields:** each screening field with its value, a confidence score, and the source quote. Low-confidence fields are highlighted.
- **Transcript:** the full conversation, with timestamps and language.
- **Timeline:** status changes, reminders sent, manual edits (who, when, why).
- **Actions:** override the status (reason required), add a note, export JSON. Every action, and every view, is written to the audit log with the recruiter's email.

### 4. Analytics (bonus)

- Funnel: started → each stage → completed, with drop-off per stage.
- Completion rate, outcome mix, median time to complete.
- **Screened within 24 h of applying** (the pilot's success metric): applicants who reached an outcome (qualified, disqualified or needs review) within `screening_target_hours` of applying, out of everyone whose window has closed or who already has an outcome. Time counts from the application date the ATS passes in (`applied_at`), or from the start of the chat when there is none.
- Breakdown by location and language.
- Computed with SQL aggregates over projected columns, never by loading every conversation.

## Rules

- The dashboard never deletes data; retention is handled by a scheduled job (see the appendix to the process design).
- Overrides need a reason, and the original agent decision stays visible.
- Every form carries a per-session CSRF token, on top of SameSite cookies. Login is rate-limited (see the process design appendix).
- Login with one account per recruiter; no shared credentials. Accounts come from `RECRUITER_USERS` (`email:hash;email:hash`), with scrypt hashes made by `uv run python -m app.cli hash-password`.

## Out of scope for the demo

- Editing a field value from the dashboard (a recruiter overrides the status and explains why in the reason or a note instead).
- Per-location permissions (all recruiters see all locations).
- Bulk actions and CSV export (only JSON export per candidate is in scope).
- Messaging the candidate from the dashboard (shown as a disabled "contact" action).
