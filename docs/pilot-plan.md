# Pilot plan

> How Grupo Sazón could start using this with real candidates, with limited risk. The thresholds are proposals to agree with the client, not results.

## Scope

- **One city for four weeks:** Madrid, chosen for the most complete zone list. Spanish and English, web chat.
- **A control group:** every other applicant stays on phone screening, so the comparison isn't against a guess.
- **Invites without an ATS:** a recruiter exports the week's applications to a CSV (`applied_at`, plus a phone number for their own use) and uploads it at `/dashboard/invite`. They send the links with the SMS or email tool they already have. The phone number stays in their file: the app doesn't store it.
- **WhatsApp comes after the pilot.** It needs a verified number, approved templates and a payment method, and every message now has a cost.

## Before the first candidate

| Item | Owner |
| --- | --- |
| Real service areas, license rules and FAQ answers (pay, vehicle, contract) in the client config | Grupo Sazón |
| Each location's real interview slots in place of the mock calendar | Us, with Grupo Sazón |
| Legal review of [appendix C](process-design-appendix.md#c-regulatory-and-platform-context), above all GDPR Art. 22 for knockouts | Grupo Sazón's counsel |
| Data processing agreement with the chosen LLM vendor, with EU processing if counsel requires it | Grupo Sazón and us |
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
| Decisions can be trusted | Knockouts a recruiter overturned | Not applicable | ≤ 2% |
| Data can be trusted | Fields a recruiter corrected, sampled from the review | Not applicable | ≤ 5% |
| Candidates aren't put off | Drop-off per question, and complaints | Not applicable | No single question loses more than 15%, and no complaint left unresolved |

## Stopping and rollback

Stop inviting and go back to phone screening, the same day, if any of these happen: a knockout overturn rate above 5% in any week, a candidate given wrong job information, personal data seen by someone who shouldn't see it, or an outage of more than a few hours. Going back is simple, because the phone process keeps running for the control group. Nothing is lost: conversations and their event logs stay in the database, and recruiters can pick up any candidate from the dashboard.

## After four weeks

Review with the client against the table. A go means adding locations one country at a time, Mexico with its own legal review, then WhatsApp. A no-go still leaves the drop-off data, which shows what to change.
