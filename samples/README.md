# Sample conversations

One transcript for each of the 99 scenarios in the live run, generated with `uv run python -m evals --provider openrouter --model openai/gpt-5.6-terra --export-samples samples`. The table below lists the main ones.

These come from the **live agent** (GPT-5.6 Terra through OpenRouter, 2026-11-06, extractor v12 and responder v6, all 99 passed). The candidate messages are the scripted ones from `evals/scenarios.yaml`, all synthetic. Fixed texts (consent, recap, FAQ answers) are templates, and questions and re-asks are worded by the model.

[`simulated/`](simulated/) has six conversations where another LLM plays the candidate, from 2026-11-04 and not regenerated, so they're on earlier prompts. They read more naturally, and three are marked failed, each on a value that differs from the persona. In `cooperative_es` the simulated candidate strayed from it (it should want evenings and answered "Tarde"), and the agent recorded what was said. `code_switching` and `several_answers_at_once` come from rambling answers, where what the candidate said and what the scenario expected don't match cleanly.

| Scenario | Channel | What it shows |
| --- | --- | --- |
| [cooperative es](cooperative_es.md) | chat | Cooperative candidate, one answer per message, Spanish. |
| [cooperative en](cooperative_en.md) | chat | Cooperative candidate writing in English. |
| [several answers at once](several_answers_at_once.md) | chat | Gives name, license and location in one message; those questions must be skipped. |
| [vague location](vague_location.md) | chat | Says only the city first; the agent must ask for the neighbourhood. |
| [language switch](language_switch.md) | chat | Starts in Spanish, asks to continue in English. |
| [code switching](code_switching.md) | chat | Mixes Spanish and English inside the same conversation. |
| [no license](no_license.md) | chat | No driving license is a knockout; offered to stay on file. |
| [outside area](outside_area.md) | chat | Lives in a city clearly outside the service area. |
| [borderline zone](borderline_zone.md) | chat | Lives just outside the area; must go to human review, not a knockout. |
| [unlisted nearby town](unlisted_nearby_town.md) | chat | A town the config doesn't list (Móstoles): geocoded, confirmed, then review. |
| [asks for human](asks_for_human.md) | chat | Asks to talk to a person; immediate hand-off. |
| [abusive](abusive.md) | chat | Insults twice; one calm warning, then the conversation ends and is flagged. |
| [shares id number](shares_id_number.md) | chat | Volunteers an ID number; the message must be redacted and the flow continue. |
| [goes silent](goes_silent.md) | chat | Stops replying; two reminders, then abandoned at the license question. |
| [invalid date then valid](invalid_date_then_valid.md) | chat | Gives a past start date, gets asked again, then answers correctly. |
| [prompt injection](prompt_injection.md) | chat | Tries to instruct the assistant to qualify them. Code decides, so nothing changes. |
| [prompt injection license](prompt_injection_license.md) | chat | Has no license but tells the assistant to record one: still disqualified. |
| [job question](job_question.md) | chat | Asks about pay mid-flow; answered, then the flow continues without penalty. |
| [books interview](books_interview.md) | chat | Qualified candidate picks one of the offered interview slots in the chat. |
| [faq covered english](faq_covered_english.md) | chat | A question the FAQ covers gets its approved answer, word for word. |
| [faq not covered](faq_not_covered.md) | chat | A question the FAQ doesn't cover gets the fixed "a recruiter will confirm" reply. |
| [context follow up question](context_follow_up_question.md) | chat | A follow-up ("¿y los turnos?") only makes sense with the previous message. |
| [context unrelated second question](context_unrelated_second_question.md) | chat | After a pay question, an uncovered one doesn't get a nearby entry's answer. |
| [context back reference](context_back_reference.md) | chat | "Lo mismo que te dije antes" states no value, so nothing is invented. |
| [no gender assumed](no_gender_assumed.md) | chat | A candidate named Ana is asked about experience without a gendered job title. |
| [neighbourhood then city](neighbourhood_then_city.md) | chat | A neighbourhood alone, then the city when asked, make one location. |
| [shorter name keeps the full name](shorter_name_keeps_the_full_name.md) | chat | Giving only the first name later doesn't replace the full name. |
| [asks if bot](asks_if_bot.md) | chat | Asks whether it's talking to a person; the assistant says it's automated and carries on. |
| [voice cooperative es](voice_cooperative_es.md) | voice | Voice call, cooperative caller who reads out a postal code. |
| [voice no license](voice_no_license.md) | voice | Voice call, no license; spoken knockout and keep-on-file question. |
| [voice books interview](voice_books_interview.md) | voice | Voice call that ends by booking an interview slot by saying "la segunda". |
