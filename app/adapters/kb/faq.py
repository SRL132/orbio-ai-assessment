"""Offline FAQ matching (keyword overlap) for the template responder.

The LLM responders get the whole approved FAQ in their cached system prompt instead; with a
handful of entries that is simpler and more accurate than retrieval (docs/architecture.md).
"""

from app.domain.config import ClientConfig
from app.domain.models import Language
from app.domain.validators import normalize


def match_faq(question: str, config: ClientConfig, language: Language) -> str | None:
    words = set(normalize(question).replace("?", " ").replace("¿", " ").split())
    best, best_score = None, 0
    for entry in config.faq:
        score = len(words & {normalize(k) for k in entry.keywords})
        if score > best_score:
            best, best_score = entry, score
    return best.answer[language] if best else None
