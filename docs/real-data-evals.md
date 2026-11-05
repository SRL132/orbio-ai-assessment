# Turning real conversations into evals

> A design for after the pilot starts. Not built, on purpose.

Synthetic scenarios only find the failures we can imagine. The README says so: a live end-to-end run still found two bugs the scripted suite had passed. Real conversations are the better source, but today's rule is that evals only use synthetic scenarios, and it exists for a good reason.

## Why it isn't built yet

- **There's no real data to test it on.** A tool tested only on synthetic chats can't show it removes real personal data, and one that looks trustworthy without being so is worse than none.
- **Live evals send scenarios to LLM providers.** An anonymization miss would send a candidate's data to a provider again, under a different purpose from the one they consented to.
- **It needs a legal basis.** Using candidates' messages to improve the agent is a new purpose, for Grupo Sazón's counsel to review along with [appendix C](process-design-appendix.md#c-regulatory-and-platform-context).

## The proposed flow

1. **Find.** A recruiter's override or a drop-off spike points to a conversation.
2. **Rewrite, don't scrub.** A person writes a new synthetic scenario that keeps the shape of the failure: the wording pattern, the order of the answers, and what went wrong. Name, street, phone number and any other detail are invented. Automatic scrubbing can only assist with this, because a person can be identified by things no pattern catches, such as "the only driver at my village's bakery".
3. **Review.** A second person checks that nothing traces back to the candidate, and records only the failure type, not the conversation id, next to the scenario.
4. **Add.** The scenario goes in `evals/scenarios.yaml` like any other, or in a new held-out file if it's kept for measuring rather than tuning.
5. **Fix and measure.** Change the prompt or policy, then run the live evals.

Because the output is a new synthetic scenario, the rule still holds, and retention and erasure of the original conversation aren't affected.

## What to build, once the pilot shows it's worth it

Start by hand and count how long it takes. Build tooling only if volume demands it: a dashboard button that opens a pre-filled draft for a person to rewrite, with obvious identifiers already masked. It never writes a scenario by itself.

## Decisions for the client

- Whether counsel accepts this as a compatible purpose, and whether the privacy notice must mention it.
- Who may read conversations for this, and whether that's logged like other recruiter views.
