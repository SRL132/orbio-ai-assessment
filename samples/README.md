# Sample conversations

One transcript per eval scenario, generated with `uv run python -m evals --export-samples samples`.

These were produced in **offline mode** (`LLM_PROVIDER=fake`): the candidate messages are the scripted ones from `evals/scenarios.yaml`, extraction uses simple keyword rules, and Lucía's questions come from the fixed templates. They show the flow, the branching and every outcome. With a real model (`--provider openrouter|anthropic`) the questions are worded by the LLM and the candidate can be LLM-simulated (`--simulate`).

| Scenario | What it shows |
| --- | --- |
| [cooperative es](cooperative_es.md) | Cooperative candidate, one answer per message, Spanish. |
| [cooperative en](cooperative_en.md) | Cooperative candidate writing in English. |
| [several answers at once](several_answers_at_once.md) | Gives name, license and location in one message; those questions must be skipped. |
| [vague location](vague_location.md) | Says only the city first; the agent must ask for the neighbourhood. |
| [language switch](language_switch.md) | Starts in Spanish, asks to continue in English. |
| [code switching](code_switching.md) | Mixes Spanish and English inside the same conversation. |
| [no license](no_license.md) | No driving license is a knockout; offered to stay on file. |
| [outside area](outside_area.md) | Lives in a city clearly outside the service area. |
| [borderline zone](borderline_zone.md) | Lives just outside the area; must go to human review, not a knockout. |
| [asks for human](asks_for_human.md) | Asks to talk to a person; immediate hand-off. |
| [abusive](abusive.md) | Insults twice; one calm warning, then the conversation ends and is flagged. |
| [shares id number](shares_id_number.md) | Volunteers an ID number; the message must be redacted and the flow continue. |
| [goes silent](goes_silent.md) | Stops replying; two reminders, then abandoned at the license question. |
| [invalid date then valid](invalid_date_then_valid.md) | Gives a past start date, gets asked again, then answers correctly. |
| [prompt injection](prompt_injection.md) | Tries to instruct the assistant to qualify them. Code decides, so nothing changes. |
| [job question](job_question.md) | Asks about pay mid-flow; answered, then the flow continues without penalty. |
| [books interview](books_interview.md) | Qualified candidate picks one of the offered interview slots in the chat. |
| [asks if bot](asks_if_bot.md) | Asks whether it's talking to a person; the assistant says it's automated and carries on. |
