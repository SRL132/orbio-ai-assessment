"""Offline FAQ matching (keyword overlap) for the keyword extractor.

The LLM extractors pick an entry from the list in their cached system prompt instead; with a
handful of entries that is simpler and more accurate than retrieval (docs/architecture.md).
"""

from app.domain.config import ClientConfig
from app.domain.validators import normalize


def match_faq(question: str, config: ClientConfig) -> str | None:
    """The id of the entry that shares the most keywords with the question, if any."""
    words = set(normalize(question).replace("?", " ").replace("¿", " ").split())
    best, best_score = None, 0
    for entry in config.faq:
        score = len(words & {normalize(k) for k in entry.keywords})
        if score > best_score:
            best, best_score = entry, score
    return best.id if best else None
