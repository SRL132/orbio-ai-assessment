"""Rule-based extractor for offline demos and CI (`LLM_PROVIDER=fake`). Not for production.

It reads short answers in the context of the question that was just asked, with simple keyword
rules in Spanish and English. It exists so the whole app runs without an API key; real
conversations need the LLM adapters.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any

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

_YES = {
    "si",
    "sí",
    "vale",
    "ok",
    "okay",
    "claro",
    "correcto",
    "perfecto",
    "yes",
    "yeah",
    "yep",
    "sure",
    "dale",
    "de acuerdo",
    "empezamos",
    "adelante",
    "go",
    "correct",
}
_NO = {"no", "nope", "no gracias", "no thanks", "nah"}
_HUMAN = ("persona", "humano", "human", "agente", "agent", "reclutador", "recruiter", "real person")
_ABUSE = (
    "idiota",
    "imbecil",
    "estupido",
    "gilipollas",
    "puta",
    "mierda",
    "idiot",
    "stupid",
    "fuck",
    "shit",
)
_ENGLISH = {
    "yes",
    "hi",
    "hello",
    "my",
    "name",
    "is",
    "i",
    "im",
    "have",
    "the",
    "and",
    "car",
    "both",
    "none",
    "weekends",
    "full",
    "part",
    "time",
    "morning",
    "afternoon",
    "evening",
    "tomorrow",
    "today",
    "never",
    "years",
    "sure",
    "live",
    "in",
    "want",
    "next",
    "week",
}
_SPANISH = {
    "si",
    "hola",
    "me",
    "llamo",
    "soy",
    "tengo",
    "coche",
    "ambos",
    "vivo",
    "en",
    "de",
    "jornada",
    "completa",
    "media",
    "fines",
    "semana",
    "manana",
    "tarde",
    "noche",
    "nunca",
    "anos",
    "hoy",
    "lunes",
    "el",
    "la",
    "y",
    "con",
    "quiero",
    "vale",
    "claro",
}
_WEEKDAYS = {
    "lunes": 0,
    "martes": 1,
    "miercoles": 2,
    "jueves": 3,
    "viernes": 4,
    "sabado": 5,
    "domingo": 6,
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_SENSITIVE = re.compile(
    r"\b\d{8}[a-z]\b|\b[xyz]\d{7}[a-z]\b|\bdni\b|\bnie\b|\bpasaporte\b|"
    r"\bpassport\b|\bcurp\b|\biban\b"
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text)


def _has(text: str, *needles: str) -> bool:
    return any(re.search(rf"\b{re.escape(n)}\b", text) for n in needles)


class KeywordExtractor:
    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        t = normalize(text)
        words = _words(t)
        fields: dict[str, Any] = {}

        def put(name: str, value: object) -> None:
            fields[name] = {"value": value, "quote": text.strip(), "confidence": 0.6}

        awaiting = state.awaiting if state.stage == Stage.COLLECTING else None
        intent: Intent = "answer"
        if _has(t, *_ABUSE):
            intent = "abuse"
        elif _has(t, *_HUMAN):
            intent = "human_request"
        elif "?" in text:
            intent = "question"

        yes_no: bool | None = None
        if t in _YES or (words and words[0] in _YES and len(words) <= 4):
            yes_no = True
        elif t in _NO or (words and words[0] == "no" and len(words) <= 3 and awaiting is None):
            yes_no = False

        name = re.search(
            r"(?:me llamo|mi nombre es|soy|my name is|i am|i'm)\s+(.+?)(?:,|\.|;| y | and |$)",
            text,
            re.I,
        )
        if name:
            put("full_name", name.group(1).strip(" .!"))
        elif awaiting == FieldName.FULL_NAME and re.fullmatch(
            r"[^\W\d_]+(?:[\s'-][^\W\d_]+){0,4}", text.strip()
        ):
            put("full_name", text.strip())

        self._license(t, awaiting, put)
        self._location(text, t, awaiting, config, put)
        self._availability(t, put)
        if awaiting == FieldName.SCHEDULE or _has(
            t, "morning", "afternoon", "evening", "noches", "tardes", "mananas"
        ):
            self._schedule(t, put)
        if awaiting == FieldName.EXPERIENCE:
            self._experience(t, config, put)
        if awaiting == FieldName.START_DATE:
            self._start_date(t, today, put)

        return ExtractionResult(
            fields=ExtractedFields.model_validate(fields),
            language=self._language(words, state.language),
            intent=intent,
            yes_no=yes_no,
            sensitive_data=bool(_SENSITIVE.search(t)),
            question=text.strip() if intent == "question" else None,
            sentiment="frustrated"
            if _has(t, "harto", "pesado", "annoying", "already told")
            else "neutral",
        )

    @staticmethod
    def _language(words: list[str], current: Language) -> Language:
        en = sum(w in _ENGLISH for w in words)
        es = sum(w in _SPANISH for w in words)
        if en > es:
            return "en"
        if es > en:
            return "es"
        return current

    @staticmethod
    def _license(t: str, awaiting: FieldName | None, put: Put) -> None:
        vehicle = None
        if _has(t, "ambos", "both", "las dos", "los dos"):
            vehicle = Vehicle.BOTH
        elif _has(t, "moto", "scooter", "motorbike", "motocicleta", "ciclomotor"):
            vehicle = Vehicle.MOTO
        elif _has(t, "coche", "car", "carro", "auto"):
            vehicle = Vehicle.CAR
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
        postal = re.search(r"\b\d{5}\b", text)
        if awaiting == FieldName.LOCATION and city is None and zone is None and not postal:
            words = [w for w in re.split(r"[,.\s]+", text.strip()) if w]
            if 0 < len(words) <= 3 and all(w.isalpha() for w in words):
                city = " ".join(words)
        if city:
            put("city", city)
        if zone:
            put("zone", zone)
        if postal:
            put("postal_code", postal.group(0))

    @staticmethod
    def _availability(t: str, put: Put) -> None:
        found: list[Availability] = []
        if _has(t, "completa", "full", "cuando sea", "whenever", "todo el dia"):
            found.append(Availability.FULL_TIME)
        if _has(t, "media", "part", "parcial"):
            found.append(Availability.PART_TIME)
        if _has(t, "fines de semana", "finde", "findes", "weekend", "weekends"):
            found.append(Availability.WEEKENDS)
        if found:
            put("availability", found)

    @staticmethod
    def _schedule(t: str, put: Put) -> None:
        found: list[Schedule] = []
        if _has(t, "manana", "mananas", "morning", "mornings"):
            found.append(Schedule.MORNING)
        if _has(t, "tarde", "tardes", "afternoon", "afternoons"):
            found.append(Schedule.AFTERNOON)
        if _has(t, "noche", "noches", "evening", "evenings", "night", "nights"):
            found.append(Schedule.EVENING)
        if _has(t, "flexible", "cualquiera", "any", "da igual", "whatever"):
            found.append(Schedule.FLEXIBLE)
        if found:
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
