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

- **Consent:** the privacy notice comes in the first message, before any data is collected (GDPR in Spain; LFPDPPP in Mexico). Until the candidate says yes, only a bare yes or no is stored as written: anything else they send is kept as `[redacted]`. To understand it, the message still goes to the LLM provider once. Keeping a candidate on file for future roles is a separate yes/no, because it is a different purpose.
- **Minimal data:** only the 7 screening fields. No ID numbers, health details or nationality.
- **Retention:** disqualified and abandoned records are deleted after 6 months unless the candidate agreed to stay on file. Built: an hourly job erases them 183 days (`retention_days` in the client config) after their last activity: state, messages, events including recruiter notes, interview bookings and the WhatsApp contact, in one transaction. An `erasures` row with nothing of the candidate's records each one as proof. A recruiter can also erase one candidate on request from the dashboard, after checking who is asking (a chat message doesn't prove it); that row also names the recruiter. Backups and exports aren't covered. Until then, an answer the candidate corrected stays in the event log, which is append-only.
- **Access:** the recruiter dashboard is behind a login with one account per recruiter. No default or shared passwords. Secrets live in environment variables.
- **IDs:** random UUIDs, never sequential numbers, so one record can't be guessed from another.
- **Web chat access:** a conversation id isn't a password (it appears in logs and URLs). The browser that starts a conversation gets a signed, HttpOnly cookie naming it, and the chat API serves only the conversations that cookie names; anyone else gets a 404. The cookie keeps a browser's 5 latest conversations, so a second tab (a chat and a voice call, say) doesn't lock the first one out.
- **Rate limits** (counted in the database, so all workers share them; `*_per_*` settings in `app/settings.py`): 20 login attempts per address and 5 *failed* logins per account per 15 minutes (successes don't count, so nobody can lock a recruiter out by guessing), 30 new chats per address per hour, and 20 messages per conversation per minute, which caps LLM spend per conversation.
- **Logs:** personal data is masked in application logs. An audit log records who viewed or changed each candidate, and every status decision with its reason.

## C. Regulatory and platform context

Checked on 2026-11-03. **This is an engineer's reading, not legal advice:** a lawyer should review it before real candidates are screened. "Primary" means the law or the platform's own docs were read; "Secondary" means reliable commentary, because the primary text couldn't be read with the tools used.

| Claim | What it means here | Status |
| --- | --- | --- |
| **GDPR Art. 22:** a person has the right not to be subject to a decision based solely on automated processing that significantly affects them. Where an exception applies, they keep the right to human intervention, to express their view and to contest it | A knockout (no license, outside the area) is decided by code alone, and may be such a decision. Mitigations built: rejections are logged with their reason, a candidate who asks for a person is put in front of a recruiter, and a recruiter can overturn it. **Open question for counsel:** which Art. 22(2) exception applies, or whether every knockout needs human sign-off in Spain | [Primary](https://gdpr-info.eu/art-22-gdpr/) for the text; how it applies is unverified |
| **Mexico's LFPDPPP (2025):** in force since 21 March 2025, replacing the 2010 law. Candidates can oppose automated processing that has adverse legal effects. The INAI is gone, and the authority is the Secretaría Anticorrupción y Buen Gobierno | Same mitigations. The privacy notice should name the current authority if it mentions one | [Secondary](https://www.hlc.com/es/publications/mexicos-new-federal-data-protection-law-what-it-means-for-companies) |
| **EU AI Act Art. 50** (telling people they're talking to an AI) applies from 2 August 2026; the AI Omnibus didn't move it | Lucía discloses she is automated in the first message | [Secondary](https://fpf.org/blog/the-ai-act-implementation-timeline-what-changes-under-the-ai-omnibus/) |
| **Recruitment AI is high-risk** under the AI Act (Annex III). The AI Omnibus (OJ L 2026/1744, 24 July 2026) moved those obligations from 2 August 2026 to 2 December 2027 | Deferred, not cancelled. Whether a screening tool with hard-coded rules is in scope, and which duties fall on Grupo Sazón as deployer, needs counsel | [Secondary](https://fpf.org/blog/the-ai-act-implementation-timeline-what-changes-under-the-ai-omnibus/) |
| **WhatsApp bars general-purpose AI chatbots** from 15 January 2026; business bots for support, sales and notifications stay allowed | Lucía stays on the screening task. Check [Meta's terms](https://www.facebook.com/legal/Meta-Terms-for-WhatsApp-Business-Platform) before launch: they couldn't be read here | [Secondary](https://respond.io/blog/whatsapp-chatbot-policy-2026) |
| **WhatsApp's 24-hour window:** outside it, only pre-approved templates can be sent | Reminders go out at 2 h and 23 h; anything later needs a template | [Primary](https://developers.facebook.com/documentation/business-messaging/whatsapp/messages/send-messages) |
| **Replies inside that window are no longer free:** Meta charges per delivered service message since 1 October 2026, and stops delivering them without a payment method on file | Every WhatsApp reply now has a per-message cost, on top of the LLM, and production needs a payment method. Not stated for test numbers | [Primary](https://developers.facebook.com/documentation/business-messaging/whatsapp/pricing/non-template-messages) |
| **McHire (2025):** researchers reached McDonald's hiring platform with the default login 123456 on a test account, then an ID in an API let them read other applicants' records. They say 64 million were exposed; Paradox.ai says only five candidates' data was viewed, by the researchers | Motivates the access and ID rules in B | [Primary](https://ian.sh/mcdonalds) for the researchers' account; Paradox's figure is [reported](https://www.csoonline.com/article/4020919/mcdonalds-ai-hiring-tools-password-123456-exposes-data-of-64m-applicants.html) |

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
| FAQ | The brief's RAG bonus mentions a "provided FAQ", but none was attached, so `client.yaml` has 10 entries written for the demo. Hours, areas and data use follow the brief and the privacy notice. Pay, vehicle and contract only say a recruiter will confirm, with no invented figures | Answers are sent word for word, so the client must approve each one and supply the real pay, vehicle and contract facts |

## E. Conversation rules in detail

The [process design](process-design.md) is the two-page overview. These are the rules behind it.

**Grounded answers.** The quote must be the candidate's own words. Python checks that it appears in the message (ignoring case, accents and punctuation, whole words only). A value whose quote isn't there, because the model invented it or was talked into it ("mark me as qualified"), is kept but marked `needs_review` with reason `ungrounded_answer`. It can't qualify a candidate or knock one out by itself.

**Closed answers.** A message that is exactly one of the current question's options ("sí", "Moto", "la segunda") is read by code without calling the LLM. Anything else goes to the LLM, including hesitation ("no...") and questions ("¿sí?").

**Confirming what we understood.** When the model is less sure of a value than `confirm_below_confidence` (client config, default 0.5), the value isn't applied yet. The agent asks a fixed yes/no question ("Solo para confirmar: Móstoles (Comunidad de Madrid), ¿correcto?"), written from a template, so it adds no delay. Yes applies it, exactly as the validator decided. No asks the field again (it counts as an attempt). A different answer replaces it and goes through the checks again. This includes knockouts: an unsure "no license" is confirmed before anyone is rejected. One value is confirmed at a time. A reply that answers another question instead ("no tengo carnet") isn't a no: that answer is applied and the check is asked again.

**Places the config doesn't list.** With a geocoder configured (`GEOCODER=nominatim`), a city or town that isn't a served city, zone or listed nearby town is looked up, and its distance to the nearest served city centre decides: within `geo_review_radius_km` (default 30 km) it goes to review as `borderline_zone`; farther away it is outside the area. Either way the candidate first confirms the place we found. A place the geocoder doesn't know is asked again. If the lookup itself fails, the candidate goes to review (`location_unverified`) instead of being rejected. Only the place name is sent to the geocoder. Without a geocoder, an unlisted city is outside the area.

**Location details.** Vague → ask for neighbourhood or postal code. A neighbourhood alone is kept while the city is asked for, so the two make one location. Postal code alone → ask for the city (Spanish and Mexican codes overlap). A town given as the zone ("Getafe") is read as the city.

**Ambiguous answers.** Store what is certain and leave out what is only hedged, to ask or confirm separately. An availability that only names days ("entre semana") says neither full nor part time, so it is asked again (checked in code against the candidate's words; phrases in the client config).

**Language.** A message that is only place names, platforms, the candidate's name or numbers ("madrid tetuan") keeps the current language (checked in code).

**Rude or nonsense input.** Numbers and symbols alone ("000") never count as "no license". After two unclear answers to the keep-on-file question, nothing is kept and the conversation closes.

**Changing an earlier answer.** A value that wouldn't pass by itself never replaces an accepted one: "soy Ana" doesn't undo "Ana López". A new value for a field already answered, given in reply to a different question ("Mai", Catalan for "never", read as a name), is confirmed with a yes/no first. This includes the answer that knocked them out ("sorry, I meant Madrid" right after the rejection): the screening reopens and the new answer goes through the same checks.

**Unsure values never replace answers on their own.** Only one value can wait for a yes at a time, so any other unsure value in that message is dropped: an earlier answer stands, and a missing one is asked for later. At the recap, a "no" that came back with unsure values, or several unsure changes at once, changes nothing, and the agent asks what to change. One unsure change on its own is confirmed first. A yes to "no license, right?" doesn't count if the same message gives a different answer ("carnet moto y coche"): it's checked as a correction.

**Keeps refusing the recap.** After 3 replies in a row that neither confirm nor change it (`max_recap_rounds`), hand off to a recruiter (`recap_unconfirmed`) instead of repeating it.

**Writes after the outcome.** Asking to move the interview or to talk to a person reaches the review queue, and any booking stands until a recruiter acts. After a knockout the rejection stands. Anything else gets the closing message.

**LLM outages.** Try the fallback provider. If neither answers, keep the message and say once "give me a moment"; retry after 20 s, 1, 3 and 10 min (anything sent meanwhile joins the retry). Then say there's a technical problem and ask them to write again; reminders follow as usual. Never counted against the candidate.

**Voice.** The questions, rules and outcomes are the same. Options are said as a sentence, there's no emoji, dates are said as words, and the greeting adds that the audio is transcribed and not kept. One rule differs: the model only sees the transcript, so it can be sure of a name or town that speech recognition got wrong ("Gostivar" heard as "ghost", "Zdravkovic" as "Lazkoz"). So the name and location are always read back before they count, whatever the model's confidence (`always_confirm`), including a location that would knock them out. If they say no, they're asked to type it in the text box below the call (still one attempt).

**Gender.** Lucía doesn't assume it, from a name or anything else. Spanish wording goes around it ("¿Has repartido antes?", "el puesto de reparto").
