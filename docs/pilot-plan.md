# Pilot plan

> How Grupo Sazón could start using this with real candidates, with limited risk. The thresholds are proposals to agree with the client, not results.

## Scope

- **One city for four weeks:** Madrid, chosen for the most complete zone list. Spanish and English, web chat.
- **A control group:** every other applicant stays on phone screening, so the comparison isn't against a guess.
- **Invites without an ATS:** every working day, a recruiter exports the new applications to a CSV (`applied_at`, `application_ref` with their own id, plus a phone number for their own use) and uploads it at `/dashboard/invite`. They send the links with the SMS or email tool they already have. The phone number stays in their file: the app stores only the reference. Daily, because the 24 h target below counts from `applied_at`: a weekly export would miss it by construction. Getting to minutes needs the ATS trigger ([`POST /api/v1/applications`](ats-integration.md)).
- **Chasing without WhatsApp:** web chat can't reach someone who closed the tab, so its reminders only show if they come back. Each day the recruiter also downloads "unfinished applications" from the invite page (fresh links, matched by `application_ref`) and resends them. This understates what WhatsApp's reminders would reach, so read the reach result as a floor.
- **WhatsApp comes after the pilot.** It needs a verified number, approved templates and a payment method, and every message now has a cost.

## Before the first candidate

| Item | Owner |
| --- | --- |
| Real service areas, license rules and FAQ answers (pay, vehicle, contract) in the client config | Grupo Sazón |
| Each location's real interview slots in place of the mock calendar | Us, with Grupo Sazón |
| Legal review of [appendix C](process-design-appendix.md#c-regulatory-and-platform-context), above all GDPR Art. 22 for knockouts | Grupo Sazón's counsel |
| Run the suite through the direct OpenAI adapter (tested with stubs only so far) | Us |
| Data processing agreement with OpenAI, and its EU data residency and zero data retention if counsel requires them: both need OpenAI's approval. If they fall through, see the [next choices](architecture.md#where-the-choice-stands) | Grupo Sazón and us |
| Recruiter accounts, a 30-minute walkthrough, and one named owner for the review queue | Both |

## Human oversight

- **Weeks 1 and 2:** a recruiter reads every knockout and every "asked for a human" case within one working day, and overturns wrong ones with the override, which requires a reason. This is the pilot's most important safety net, and it produces the number below.
- **Weeks 3 and 4:** continue if that error rate is above target; otherwise sample 1 in 5.

## What we measure

| Bet ([vision](../VISION.md)) | Metric | Today | Go if |
| --- | --- | --- | --- |
| Async chat reaches more people | Applicants screened within 24 h of applying ("screened on time" in Analytics; unopened links count against it) | About 40% reached, over days | ≥ 60% |
| Candidates finish | Completion rate among those who open the link | Unknown | ≥ 70% |
| Recruiters see fewer unqualified people | Share of qualified candidates among those who reach a recruiter, and recruiter hours per hire | About 20% of recruiter time well spent | Clear improvement over the control group |
| Decisions can be trusted | Knockouts a recruiter overturned (on Analytics; only meaningful if recruiters read the knockouts) | Not applicable | ≤ 2% |
| Data can be trusted | Fields a recruiter corrected, sampled from the review | Not applicable | ≤ 5% |
| Candidates aren't put off | Drop-off per question, and complaints | Not applicable | No single question loses more than 15%, and no complaint left unresolved |

## The business case

These are planning figures for the client to correct, not results. From the brief: 13 recruiters, about 1,000 applications and 975 calls a week, 60% unanswered, 80% of recruiter time on unqualified candidates.

- **Reach:** about 390 screened a week today. Meeting the 60% target above would be 600, about 54% more.
- **Recruiter time (assumes a 40-hour week):** 520 hours a week, of which about 416 go to unqualified candidates. If the agent removes half of that, about 208 hours a week are free, or 16 per recruiter.
- **Value (assumes EUR 20 an hour, for the client to replace):** about EUR 4,160 a week.
- **LLM cost:** about $15 a week at 1,000 conversations ($0.0153 each, [measured on the scripted suite](architecture.md#where-the-choice-stands)). Hosting, WhatsApp fees and the recruiters' review time come on top.

The pilot replaces the assumed half with a measurement: recruiter hours per hire against the control group.

## Stopping and rollback

Stop inviting and go back to phone screening, the same day, if any of these happen: a knockout overturn rate above 5% in any week, a candidate given wrong job information, personal data seen by someone who shouldn't see it, or an outage of more than a few hours. Going back is simple, because the phone process keeps running for the control group. Nothing is lost: conversations and their event logs stay in the database, and recruiters can pick up any candidate from the dashboard.

## After four weeks

Review with the client against the table. A go means adding locations one country at a time, Mexico with its own legal review, then WhatsApp. A no-go still leaves the drop-off data, which shows what to change.
