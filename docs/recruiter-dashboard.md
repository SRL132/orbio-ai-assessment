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
- Each item shows its **reason**: `human_requested`, `no_slot_picked` (qualified, but picked no interview slot after the reminders), `borderline_zone`, `field_failed_twice:<field>`, `ungrounded_answer:<field>` (the extracted value isn't in the candidate's words), `location_unverified:location` (the place lookup failed), `license_unclear`, `flagged_behaviour`, `data_request` (stop or delete their data), `interview_change` (a booked interview stays until you act), `recap_unconfirmed` (kept refusing the recap).
- Actions: mark qualified or mark disqualified (reason required). To contact the candidate, the detail page shows how (below).

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
- **Knockouts a recruiter overturned** (the pilot's safety number): candidates the agent knocked out whom an override later moved away from disqualified, out of everyone knocked out. A candidate correcting their own answer doesn't count. Erased conversations drop out of both numbers.
- Breakdown by location, language and agent version (a hash of the prompts and client config, plus the model), so results can be compared before and after a change.
- Computed with SQL aggregates over projected columns (overturned knockouts: over the event log), never by loading every conversation.

## Rules

- A recruiter can erase one candidate on request, from the candidate's page, by typing `ERASE`. It removes the whole conversation at once, can't be undone, and leaves an `erasures` row with who and when. Everything else is deleted only by the retention job (see the appendix to the process design).
- Overrides need a reason, and the original agent decision stays visible.
- Every form carries a per-session CSRF token, on top of SameSite cookies. Login is rate-limited (see the process design appendix).
- Login with one account per recruiter; no shared credentials. Accounts come from `RECRUITER_USERS` (`email:hash;email:hash`), with scrypt hashes made by `uv run python -m app.cli hash-password`.

## Out of scope for the demo

- Editing a field value from the dashboard (a recruiter overrides the status and explains why in the reason or a note instead).
- Per-location permissions (all recruiters see all locations).
- Bulk actions and CSV export (only JSON export per candidate is in scope).
- Messaging the candidate from the dashboard. The detail page shows how to reach them instead: the WhatsApp number on that channel, the `application_ref` for an invited candidate, or a plain statement that a candidate from the open chat link has no contact details.

## Inviting candidates

`/dashboard/invite` makes one signed chat link per applicant, from a CSV with an `applied_at` column or from a number. The download keeps the upload's other columns (a phone number, say) so links can be matched to people, and they're never stored or logged. Recruiters send the links with their own SMS or email tool, so a pilot doesn't need an ATS. Links expire after 7 days. An optional `application_ref` column (letters, digits and `. _ -`) is the one thing kept: the recruiter's own id, which points back to the person in their file.

A checkbox makes voice links instead (a `voice_url` column), so an invite-only site can offer `/voice` too. The link has the same format as a chat link, and opens the page for its conversation's channel. Speech needs Chrome or Edge; other browsers can type. Voice links work the same with WhatsApp invites on.

"Download unfinished applications" lists everyone with a reference who is still in progress or was abandoned for silence, with a fresh link in the column for their channel, because web chat and voice can't reach someone who closed the tab. Someone who comes back to a voice link sees the transcript and hears only Lucía's latest message. Candidates who declined consent aren't on it.

With `WHATSAPP_INVITE_NUMBER` set, the same page makes `wa.me` links instead (a `whatsapp_url` column), each with a signed code already typed in. Sending it ties the number to that application, and the greeting goes out then. A code works once. The unfinished list gives a new code to anyone who hasn't sent theirs, and a plain link to the chat for anyone who has. It stays off until Meta delivers replies. With it on, a new number that writes with no valid code is ignored: no conversation, no reply, no LLM call. `INVITE_ONLY=true` applies the same rule to the web chat and voice pages, and to WhatsApp while invites are still web links: `/voice` without a voice link shows a message and can't start a call.

An invited conversation has no reminders until the link is opened, and it stays out of the candidate list and counters until then. Analytics shows "Invited, not opened", and those applicants still count against "screened on time", so the agent's reach isn't flattered. Unopened invites are erased after the retention period.

## Rejected candidates who ask for a human

A knockout stands when the candidate asks for a person, so asking can't skip it. The list has a "Rejected, asked for a human" counter and an "Asked for a human" filter (rejected candidates only), and a recruiter can overturn the rejection with the usual override and a required reason.
