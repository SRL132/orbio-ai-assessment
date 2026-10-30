# Process Design — Appendix

> Supporting detail for [`process-design.md`](process-design.md). Not part of the 2-page Phase 1 document.

## A. License rules (config, not conversation)

The conversation asks one question: does the candidate have a license, and for which vehicle (*Moto · Coche · Ambos · No tengo*). The detailed rules below live in per-country config and only change the outcome from "qualified" to "needs review", never to a knockout.

**Spain (working default):**

- Moped up to 50cc: AM or higher.
- 125cc motorbike: A1, A2 or A. A car license (B) also covers a 125cc once held for 3 years.
- Car: B.

**Mexico:** categories vary by state, so no rule is applied by default. If a candidate says they have a license, it is accepted, and a recruiter confirms the category at interview.

> To confirm with Grupo Sazón: which vehicles their drivers use, and whether they provide them.

## B. Privacy and security

The agent collects only what screening needs, and every decision can be traced.

- **Consent:** the privacy notice comes in the first message, before any data is collected (GDPR in Spain; LFPDPPP in Mexico). Keeping a candidate on file for future roles is a separate yes/no, because it is a different purpose.
- **Minimal data:** only the 7 screening fields. No ID numbers, health details or nationality.
- **Retention:** disqualified and abandoned records are deleted after 6 months unless the candidate agreed to stay on file.
- **Access:** the recruiter dashboard is behind a login with one account per recruiter. No default or shared passwords. Secrets live in environment variables.
- **IDs:** random UUIDs, never sequential numbers, so one record can't be guessed from another.
- **Web chat access:** a conversation id isn't a password (it appears in logs and URLs). The browser that starts a conversation gets a signed, HttpOnly cookie naming it, and the chat API serves only that conversation; anyone else gets a 404.
- **Logs:** personal data is masked in application logs. An audit log records who viewed or changed each candidate, and every status decision with its reason.

## C. Regulatory and platform context

These claims came from earlier research and **must be verified against primary sources before the presentation**. Until then, present them as "to our understanding", not as fact.

| Claim | Why it matters | Status |
| --- | --- | --- |
| EU AI Act Article 50 (transparency: users must know they're talking to an AI) applies from 2 August 2026 | Lucía discloses she is automated in the first message | To verify |
| Recruitment AI is "high-risk" under the EU AI Act, with high-risk obligations possibly deferred | The agent never makes the final hiring decision; a human does | To verify |
| WhatsApp Business API restricts general-purpose AI chatbots from 15 January 2026; task-specific bots are allowed | Lucía stays strictly on the screening task | To verify |
| WhatsApp messages outside the 24-hour customer-service window need a pre-approved template | Shapes the 24 h reminder | To verify |
| McHire (2025): a large applicant-data exposure caused by a test account with a weak default password and guessable IDs | Motivates the access and ID rules in B | To verify |

## D. Assumptions to confirm with Grupo Sazón

The brief leaves these open. Each has a working default in config, so it can change without new code.

| Assumption | Working default | Why it matters |
| --- | --- | --- |
| Which license counts | Has a license for the vehicle they'd use; detailed rules per country (A) | Delivery is often by moped or motorbike, so a car license alone may be the wrong check |
| Service areas | One list of zones and postal codes per location (45 locations); a plausible set is invented for the demo | The zone knockout is only as good as this list |
| Experience | Nice to have, never a knockout | Requiring it would shrink a pool that is already hard to reach |
| Channel | WhatsApp in production, web chat for the demo | Decides reminder rules and message templates |
| Data retention | 6 months for disqualified and abandoned, unless the candidate opts in | GDPR / LFPDPPP |
| Hand-off | Qualified candidates book an interview slot in the chat, from each location's calendar (mocked for the demo) | We need each location's interview calendar |
| FAQ | The brief's RAG bonus mentions a "provided FAQ", but none was attached; we write our own for the demo | Job questions are answered only from approved content |
