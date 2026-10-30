"""Fake LLM adapters: deterministic, offline, used by tests and `LLM_PROVIDER=fake`."""

from collections import deque
from collections.abc import Iterable
from datetime import date

from app.adapters.kb.faq import match_faq
from app.domain.config import ClientConfig
from app.domain.models import AnswerQuestion, AskField, ConversationState, ExtractionResult, ReAsk


class ScriptedExtractor:
    """Returns pre-written extractions in order; an empty result once the script runs out."""

    def __init__(self, script: Iterable[ExtractionResult] = ()) -> None:
        self._script = deque(script)
        self.seen: list[str] = []

    def push(self, *results: ExtractionResult) -> None:
        self._script.extend(results)

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        self.seen.append(text)
        if self._script:
            return self._script.popleft()
        return ExtractionResult(language=state.language)


class TemplateResponder:
    """Answers with the configured fallback templates, so fake mode reads like the real thing."""

    def compose(
        self,
        action: AskField | ReAsk | AnswerQuestion,
        state: ConversationState,
        config: ClientConfig,
    ) -> str:
        lang, channel = state.language, state.channel
        match action:
            case AskField(field=name):
                return config.template(lang, f"ask_{name.value}", channel)
            case ReAsk(field=name):
                return config.template(lang, f"reask_{name.value}", channel)
            case AnswerQuestion(question=question):
                return match_faq(question, config, lang) or config.template(
                    lang, "answer_unknown", channel
                )
