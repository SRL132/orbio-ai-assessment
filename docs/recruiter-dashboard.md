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
| Fix a mistake | Edit a field or change a status, with a required reason, logged |

## Views

### 1. Candidate list (home)

- One row per candidate: name, location / zone, status, availability, schedule, start date, last activity.
- **Status** values: `in_progress`, `qualified`, `needs_review`, `disqualified`, `abandoned`.
- Default sort: `needs_review` and `qualified` first, then most recent activity.
- Filters: status, location, availability, schedule, start date range, language.
- Text search by name.
- Counters at the top: new today, qualified, waiting for review.

### 2. Review queue

- The `needs_review` candidates, oldest first, so nobody waits too long.
- Each item shows its **reason**: `human_requested`, `borderline_zone`, `field_failed_twice:<field>`, `license_unclear`, `flagged_behaviour`.
- Actions: mark qualified, mark disqualified (reason required), or contact the candidate.

### 3. Candidate detail

- **Summary:** the post-conversation summary (3–5 lines) at the top.
- **Fields:** each screening field with its value, a confidence score, and the source quote. Low-confidence fields are highlighted.
- **Transcript:** the full conversation, with timestamps and language.
- **Timeline:** status changes, reminders sent, manual edits (who, when, why).
- **Actions:** edit a field, override the status, add a note. Every action is written to the audit log.

### 4. Analytics (bonus)

- Funnel: started → each stage → completed, with drop-off per stage.
- Completion rate, outcome mix, median time to complete.
- Breakdown by location and language.

## Rules

- The dashboard never deletes data; retention is handled by a scheduled job (see the appendix to the process design).
- Overrides need a reason, and the original agent decision stays visible.
- Login with one account per recruiter; no shared credentials.

## Out of scope for the demo

- Per-location permissions (all recruiters see all locations).
- Bulk actions and CSV export (only JSON export per candidate is in scope).
- Messaging the candidate from the dashboard (shown as a disabled "contact" action).
