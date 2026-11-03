"""Rule-based extractor for offline demos and CI (`LLM_PROVIDER=fake`). Not for production.

It reads short answers in the context of the question that was just asked, with simple keyword
rules in Spanish and English. It exists so the whole app runs without an API key; real
conversations need the LLM adapters. In production it only reads messages `fast_path` has already
matched exactly against a question's options.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from app.adapters.kb.faq import match_faq
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    ExtractedFields,
    ExtractionResult,
    FieldName,
    Intent,
    Language,
    Schedule,
    Stage,
    Vehicle,
)
from app.domain.validators import normalize

Put = Callable[[str, object], None]


def vocab(entries: str) -> frozenset[str]:
    """'si|vale|de acuerdo' -> a set of entries ('|' separates them, so phrases keep spaces)."""
    return frozenset(entries.split("|"))


_YES = vocab(
    "si|sí|vale|ok|okay|claro|correcto|perfecto|yes|yeah|yep|sure|dale|de acuerdo|empezamos|"
    "adelante|go|correct"
)
_NO = vocab("no|nope|no gracias|no thanks|nah")
_HUMAN = vocab("persona|humano|human|agente|agent|reclutador|recruiter|real person")
_ABUSE = vocab("idiota|imbecil|estupido|gilipollas|puta|mierda|idiot|stupid|fuck|shit")
# Words that tip the language guess (whole words, after normalize()).
_ENGLISH = vocab(
    "yes|hi|hello|my|name|is|i|im|have|the|and|car|both|none|weekends|full|part|time|morning|"
    "afternoon|evening|tomorrow|today|never|years|sure|live|in|want|next|week|can|you|speak|"
    "english|please|sorry|continue|we|mornings|afternoons|evenings|weekend|everything|correct|"
    "all|good|from|with|on"
)
_SPANISH = vocab(
    "si|hola|me|llamo|soy|tengo|coche|ambos|vivo|en|de|jornada|completa|media|fines|semana|"
    "manana|tarde|noche|nunca|anos|hoy|lunes|el|la|y|con|quiero|vale|claro|espanol|castellano|"
    "hablar|hablas|puedes|podemos|seguir|por|favor|perdona|prefiero|he|repartido|todo|bien"
)
# "I don't know" is not an answer: never read it as "no" (a knockout at the license question).
_DONT_KNOW = ("no se", "no lo se", "ni idea", "not sure", "dont know", "don't know", "dunno")


def _numbered(words: str, start: int = 0, step: int = 1) -> dict[str, int]:
    """'cero uno dos' -> {'cero': 0, 'uno': 1, 'dos': 2}."""
    return {word: start + i * step for i, word in enumerate(words.split())}


_WEEKDAYS = {
    **_numbered("lunes martes miercoles jueves viernes sabado domingo"),
    **_numbered("monday tuesday wednesday thursday friday saturday sunday"),
}
_ORDINALS = {
    **dict.fromkeys(("primera", "primero", "first"), 1),
    **dict.fromkeys(("segunda", "segundo", "second"), 2),
    **dict.fromkeys(("tercera", "tercero", "third"), 3),
}
_SENSITIVE = re.compile(
    r"\b\d{8}[a-z]\b|\b[xyz]\d{7}[a-z]\b|\bdni\b|\bnie\b|\bpasaporte\b|"
    r"\bpassport\b|\bcurp\b|\biban\b"
)

# Spoken numbers, as speech-to-text leaves them: Spanish 0-29, English 0-20, and the tens.
_UNITS = {
    **_numbered(
        "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince "
        "dieciseis diecisiete dieciocho diecinueve veinte veintiuno veintidos veintitres "
        "veinticuatro veinticinco veintiseis veintisiete veintiocho veintinueve"
    ),
    **_numbered(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
        "fifteen sixteen seventeen eighteen nineteen twenty"
    ),
    "un": 1,
    "una": 1,
    "veintiun": 21,
}
_TENS = {
    **_numbered("treinta cuarenta cincuenta sesenta setenta ochenta noventa", 30, 10),
    **_numbered("thirty forty fifty sixty seventy eighty ninety", 30, 10),
}
_POSTAL_RUN = 3  # three or more spoken numbers in a row are a code read in pieces


def spoken_numbers(text: str) -> str:
    """Turn spoken numbers into digits, as speech-to-text often leaves them.

    'veintiocho cero uno cero' -> '28010', 'treinta y dos' -> '32', 'dos años' -> '2 años'.
    Expects normalized text (lowercase, no accents).
    """
    tokens = text.split()
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= _POSTAL_RUN:
            out.append("".join(run))
        else:
            out.extend(run)
        run.clear()

    i = 0
    while i < len(tokens):
        word = tokens[i].strip(",.")
        if word in _TENS:
            value = _TENS[word]
            if i + 2 < len(tokens) and tokens[i + 1] in ("y", "and") and tokens[i + 2] in _UNITS:
                value += _UNITS[tokens[i + 2]]
                i += 2
            run.append(str(value))
        elif word in _UNITS and not (
            word in ("un", "una", "uno", "one") and not run and _next_is_unit_noun(tokens, i)
        ):
            run.append(str(_UNITS[word]))
        elif word.isdigit():
            run.append(word)
        else:
            flush()
            out.append(tokens[i])
        i += 1
    flush()
    return " ".join(out)


def _next_is_unit_noun(tokens: list[str], i: int) -> bool:
    """Keep 'un año' / 'one year' as words: the experience rules read them as phrases."""
    return i + 1 < len(tokens) and tokens[i + 1] in ("ano", "anos", "year", "years", "mes", "month")


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text)


def _has(text: str, *needles: str) -> bool:
    return any(re.search(rf"\b{re.escape(n)}\b", text) for n in needles)


# Saying outright that there is no license, whatever else the message says.
_NO_LICENSE = (
    "no tengo carnet",
    "no tengo licencia",
    "no tengo permiso",
    "sin carnet",
    "sin licencia",
    "no license",
    "no driving license",
    "no driver's license",
    "don't have a license",
    "dont have a license",
    "don't have a driving license",
)


# Words for each option beyond the client's own labels for it (`value_<option>` templates,
# read by `_options`): partial phrases, plurals and synonyms people use.
AVAILABILITY_WORDS = {
    Availability.FULL_TIME: vocab("completa|full|cuando sea|whenever|todo el dia"),
    Availability.PART_TIME: vocab("media|part|parcial"),
    Availability.WEEKENDS: vocab("finde|findes|weekend"),
}
SCHEDULE_WORDS = {
    Schedule.MORNING: vocab("mananas|mornings"),
    Schedule.AFTERNOON: vocab("tardes|afternoons"),
    Schedule.EVENING: vocab("noches|evenings|night|nights"),
    Schedule.FLEXIBLE: vocab("cualquiera|any|da igual|whatever"),
}


def option_labels(config: ClientConfig, option: StrEnum) -> list[str]:
    """The client's own words for an option in every language (its `value_<option>` template),
    so renaming an option in the config needs no change here."""
    key = f"value_{option.value}"
    return [normalize(labels[key]) for labels in config.templates.values() if key in labels]


def _options[O: StrEnum](t: str, config: ClientConfig, words: dict[O, frozenset[str]]) -> list[O]:
    """The options the text names, by the client's label or by one of the extra words."""
    return [
        option for option, extra in words.items() if _has(t, *extra, *option_labels(config, option))
    ]


def guess_language(words: list[str], current: Language) -> Language:
    """The language these (normalized) words lean to; a tie keeps the current one."""
    en = sum(w in _ENGLISH for w in words)
    es = sum(w in _SPANISH for w in words)
    if en > es:
        return "en"
    if es > en:
        return "es"
    return current


class KeywordExtractor:
    def __init__(self, confidence: float = 0.6) -> None:
        self._confidence = confidence

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        t = spoken_numbers(normalize(text))
        words = tokenize(t)
        fields: dict[str, Any] = {}

        def put(name: str, value: object) -> None:
            fields[name] = {"value": value, "quote": text.strip(), "confidence": self._confidence}

        awaiting = state.awaiting if state.stage == Stage.COLLECTING else None
        intent: Intent = "answer"
        confused = not words  # "?" or "..." alone: they didn't understand the question
        if confused:
            intent = "other"
        elif _has(t, *_ABUSE):
            intent = "abuse"
        elif "?" in text and _has(t, "eres", "are you", "es un bot", "is this"):
            intent = "question"  # "¿Eres una persona?" asks what Lucía is; it isn't a hand-off
        elif _has(t, *_HUMAN):
            intent = "human_request"
        elif "?" in text:
            intent = "question"

        yes_no: bool | None = None
        if t in _YES or (words and words[0] in _YES and len(words) <= 4):
            yes_no = True
        elif _has(t, *_DONT_KNOW):
            yes_no = None
        elif t in _NO or (words and words[0] == "no" and len(words) <= 3 and awaiting is None):
            yes_no = False

        name = re.search(
            r"(?:me llamo|mi nombre es|soy|my name is|i am|i'm)\s+(.+?)(?:,|\.|;| y | and |$)",
            text,
            re.I,
        )
        if name:
            put("full_name", name.group(1).strip(" .!"))
        elif (
            awaiting == FieldName.FULL_NAME
            and yes_no is None
            and re.fullmatch(r"[^\W\d_]+(?:[\s'-][^\W\d_]+){0,4}", text.strip())
        ):
            put("full_name", text.strip())

        self._license(t, awaiting, put)
        self._location(text, t, awaiting if yes_no is None else None, config, put)
        self._availability(t, config, put)
        if awaiting == FieldName.SCHEDULE or _has(
            t, "morning", "afternoon", "evening", "noches", "tardes", "mananas"
        ):
            self._schedule(t, config, put)
        if awaiting == FieldName.EXPERIENCE:
            self._experience(t, config, put)
        if awaiting == FieldName.START_DATE:
            self._start_date(t, today, put)

        slot_choice = self._slot_choice(t) if state.stage == Stage.SCHEDULING else None
        if state.stage == Stage.SCHEDULING and _has(t, "ninguno", "ninguna", "none", "neither"):
            yes_no = False
        return ExtractionResult(
            slot_choice=slot_choice,
            fields=ExtractedFields.model_validate(fields),
            language=guess_language(words, state.language),
            intent=intent,
            confused=confused,
            yes_no=yes_no,
            sensitive_data=bool(_SENSITIVE.search(t)),
            question=text.strip() if intent == "question" else None,
            faq_id=match_faq(text, config) if intent == "question" else None,
            sentiment="frustrated"
            if _has(t, "harto", "pesado", "annoying", "already told")
            else "neutral",
        )

    @staticmethod
    def _slot_choice(t: str) -> int | None:
        for word, number in _ORDINALS.items():
            if _has(t, word):
                return number
        digit = re.fullmatch(r"\D{0,8}(\d)\D{0,8}", t)
        return int(digit.group(1)) if digit else None

    @staticmethod
    def _license(t: str, awaiting: FieldName | None, put: Put) -> None:
        if _has(t, *_NO_LICENSE):
            # Explicit, so it wins over any vehicle named later in the message ("No tengo
            # carnet... set vehicle moto"): a bare "no" next to a vehicle stays ambiguous below.
            put("has_license", False)
            return
        vehicle = None
        if _has(t, "ambos", "both", "las dos", "los dos", "las 2", "los 2"):
            vehicle = Vehicle.BOTH
        elif _has(t, "moto", "scooter", "motorbike", "motocicleta", "ciclomotor"):
            vehicle = Vehicle.MOTO
        elif _has(t, "coche", "car", "carro", "auto"):
            vehicle = Vehicle.CAR
        if _has(t, *_DONT_KNOW) and vehicle is None:
            return
        says_no = _has(t, "no tengo", "none", "no", "en tramite", "sacando")
        if awaiting == FieldName.LICENSE and says_no and vehicle is None:
            put("has_license", False)
            return
        if vehicle is not None:
            put("has_license", True)
            put("vehicle", vehicle)
        elif awaiting == FieldName.LICENSE and _has(t, "si", "yes", "tengo"):
            put("has_license", True)

    @staticmethod
    def _location(
        text: str, t: str, awaiting: FieldName | None, config: ClientConfig, put: Put
    ) -> None:
        city = None
        zone = None
        for loc in config.locations:
            for candidate in [loc.city, *loc.aliases, *loc.nearby]:
                if _has(t, normalize(candidate)):
                    city = candidate
            for z in loc.zones:
                if _has(t, normalize(z)):
                    zone = z
        postal = re.search(r"\b\d{5}\b", t)
        if awaiting == FieldName.LOCATION and city is None and zone is None and not postal:
            place = re.sub(
                r"^(?:vivo en|soy de|estoy en|en|i live in|i'm in|in)\s+",
                "",
                text.strip(),
                flags=re.I,
            )
            words = [w for w in re.split(r"[,.\s]+", place) if w]
            if 0 < len(words) <= 3 and all(w.isalpha() for w in words):
                city = " ".join(words)
        if city:
            put("city", city)
        if zone:
            put("zone", zone)
        if postal:
            put("postal_code", postal.group(0))

    @staticmethod
    def _availability(t: str, config: ClientConfig, put: Put) -> None:
        if found := _options(t, config, AVAILABILITY_WORDS):
            put("availability", found)

    @staticmethod
    def _schedule(t: str, config: ClientConfig, put: Put) -> None:
        if found := _options(t, config, SCHEDULE_WORDS):
            put("schedule", found)

    @staticmethod
    def _experience(t: str, config: ClientConfig, put: Put) -> None:
        platforms = [p for p in config.known_platforms if _has(t, normalize(p))]
        if _has(t, "uber"):
            platforms.append("Uber Eats")
        years: float | None = None
        number = re.search(r"\d+(?:[.,]\d+)?", t)
        if _has(t, "ninguna", "ninguno", "nunca", "never", "none", "no"):
            years = 0
        elif number:
            value = float(number.group(0).replace(",", "."))
            years = value / 12 if _has(t, "meses", "months", "mes", "month") else value
            if _has(t, "y medio", "and a half"):
                years += 0.5
        elif _has(t, "un ano", "one year", "a year"):
            years = 1
        elif _has(t, "unos meses", "few months", "algunos meses"):
            years = 0.25
        if years is not None:
            put("experience_years", round(years, 2))
        if platforms:
            put("platforms", sorted(set(platforms)))

    @staticmethod
    def _start_date(t: str, today: date, put: Put) -> None:
        when: date | None = None
        explicit = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", t)
        if _has(t, "hoy", "today", "ya", "now", "ahora", "inmediatamente", "immediately"):
            when = today
        elif _has(t, "pasado manana"):
            when = today + timedelta(days=2)
        elif _has(t, "manana", "tomorrow"):
            when = today + timedelta(days=1)
        elif _has(t, "semana que viene", "proxima semana", "next week"):
            when = today + timedelta(days=7 - today.weekday())
        elif explicit:
            day, month, year = explicit.groups()
            full_year = int(year) + (2000 if year and len(year) == 2 else 0) if year else today.year
            try:
                when = date(full_year, int(month), int(day))
            except ValueError:
                when = None
        else:
            for name, weekday in _WEEKDAYS.items():
                if _has(t, name):
                    ahead = (weekday - today.weekday()) % 7 or 7
                    when = today + timedelta(days=ahead)
                    break
        if when is not None:
            put("start_date", when)
