"""Quick-reply options for a message: the same rules the web chat uses to draw its buttons."""

from __future__ import annotations

import re

_LISTED = re.compile(r":\s*([^:?]+·[^:?]+)$")
_NUMBERED = re.compile(r"^(\d)\) ", re.MULTILINE)
_YES_NO_ES = re.compile(
    r"\((sí|si) / no\)|¿Empezamos\?|¿Es todo correcto\?|futuras ofertas\?|cerca de ti\?",
    re.IGNORECASE,
)
_YES_NO_EN = re.compile(
    r"\(yes / no\)|Shall we start\?|Is everything correct\?|future openings\?|near you\?",
    re.IGNORECASE,
)


def quick_replies(text: str) -> list[str]:
    options: list[str] = []
    numbered = _NUMBERED.findall(text)
    if numbered:
        options += [*numbered, "None" if "Pick a time" in text else "Ninguno"]
    listed = _LISTED.search(text.strip())
    if listed:
        options += [part.strip() for part in listed.group(1).split("·") if part.strip()]
    if _YES_NO_ES.search(text):
        options += ["Sí", "No"]
    if _YES_NO_EN.search(text):
        options += ["Yes", "No"]
    return list(dict.fromkeys(options))
