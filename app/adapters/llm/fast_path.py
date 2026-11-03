"""Read closed answers without the LLM, in front of the real extractor.

When the whole message is exactly one of the options the current question allows ("sí", "Moto",
"Media jornada", "la segunda"), code can read it: the turn is faster, costs nothing and can't fail
on a provider outage. Anything else goes to the LLM, so "sí, pero solo los lunes", "¿sí?" or an
injection attempt never takes this path. The engine, validators and policy still decide what the
answer means.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from app.adapters.llm.keyword import (
    _AVAILABILITY_WORDS,
    _SCHEDULE_WORDS,
    KeywordExtractor,
    _labels,
    _vocab,
    _words,
)
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    ExtractionResult,
    FieldName,
    Schedule,
    Stage,
)
from app.domain.ports import Extractor
from app.domain.validators import normalize

# Stripped before matching. "?" and "¿" stay: "¿sí?" is a question, so it goes to the LLM.
_EDGES = " \t\r\n.,;:!¡"
_YES = _vocab("si|vale|ok|okay|claro|correcto|de acuerdo|yes|yeah|yep|sure|correct")
_NO = _vocab("no|nope|no gracias|no thanks")
_LICENSE = _vocab("moto|coche|carro|car|ambos|both|las dos|los dos|no tengo|no|none")
_SLOT = _vocab(
    "1|2|3|4|5|6|7|8|9|primera|la primera|primero|el primero|segunda|la segunda|segundo|"
    "el segundo|tercera|la tercera|tercero|el tercero|first|the first|second|the second|third|"
    "the third|ninguno|ninguna|none|neither|no"
)
# A sure reading: the whole message is one of the question's own options.
_CONFIDENCE = 1.0
_READER = KeywordExtractor(confidence=_CONFIDENCE)


def closed_answer(
    state: ConversationState, text: str, config: ClientConfig, today: date
) -> ExtractionResult | None:
    """The reading of `text` if it is exactly a closed answer to the current question."""
    if ".." in text or "…" in text:  # trailing off is hesitation: "no..." isn't a sure "no"
        return None
    answer = text.strip(_EDGES)
    said = normalize(answer)
    if state.confirming is not None or state.stage in (
        Stage.CONSENT,
        Stage.RECAP,
        Stage.KEEP_ON_FILE,
    ):
        # Only the yes or no: a "no" must never also be read as an answer to the field in question.
        if said not in _YES and said not in _NO:
            return None
        language = KeywordExtractor._language(_words(said), state.language)
        return ExtractionResult(yes_no=said in _YES, language=language)
    if state.stage == Stage.SCHEDULING:
        closed = not state.no_slot_fits and said in _SLOT
    else:
        closed = state.stage == Stage.COLLECTING and said in _options(state.awaiting, config)
    return _READER.extract(state, answer, config, today) if closed else None


def _options(field: FieldName | None, config: ClientConfig) -> frozenset[str]:
    match field:
        case FieldName.LICENSE:
            return _LICENSE
        case FieldName.AVAILABILITY:
            return _spelled(_AVAILABILITY_WORDS, config)
        case FieldName.SCHEDULE:
            return _spelled(_SCHEDULE_WORDS, config)
        case _:
            return frozenset()


def _spelled(
    words: Mapping[Availability, frozenset[str]] | Mapping[Schedule, frozenset[str]],
    config: ClientConfig,
) -> frozenset[str]:
    """Every way of saying these options: the client's labels and the extra words."""
    return frozenset(
        word for option, extra in words.items() for word in (*extra, *_labels(config, option))
    )


class FastPathExtractor:
    def __init__(self, llm: Extractor) -> None:
        self._llm = llm

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        return closed_answer(state, text, config, today) or self._llm.extract(
            state, text, config, today
        )
