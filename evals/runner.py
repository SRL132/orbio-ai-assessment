"""Eval harness: run scenarios against the real engine and score the outcome.

Two modes:
- scripted: replay the scenario's candidate messages (works offline with LLM_PROVIDER=fake)
- simulated: an LLM plays the candidate from the scenario's persona (needs a provider key)

Every run uses a fresh in-memory database and a fake clock starting on Monday 2026-11-02, so
relative dates ("mañana", "el lunes") are deterministic.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import Engine

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.geo.fake import FakeGeocoder
from app.adapters.llm.usage import UsageMeter
from app.adapters.storage.sql import REDACTED, SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Channel,
    ConversationState,
    FieldName,
    Language,
    Stage,
    StoredMessage,
)
from app.domain.ports import Extractor, Ports, Responder
from app.domain.render import check_reply, speakable
from app.domain.validators import normalize
from app.worker.service import ScreeningService

SCENARIOS = Path(__file__).parent / "scenarios.yaml"
HOLDOUT = Path(__file__).parent / "holdout.yaml"
EVAL_START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
MAX_SIMULATED_TURNS = 20
SIMULATOR_ATTEMPTS = 3


class Expect(BaseModel):
    status: str | list[str]  # a list: any of these outcomes is acceptable
    language: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    review_reasons_include: list[str] = Field(default_factory=list)
    events_include: list[str] = Field(default_factory=list)
    disqualify_reason: str | None = None
    keep_on_file: bool | None = None
    redacted: bool | None = None
    reminders_sent: int | None = None
    awaiting: str | None = None
    max_turns: int | None = None
    interview_booked: bool | None = None
    reply_contains: list[str] = Field(default_factory=list)
    # Regexes (case-insensitive) no agent message after the opening may match, e.g. a greeting.
    later_replies_avoid: list[str] = Field(default_factory=list)
    dropped_at: str | None = None
    # Per field: its status ("valid", "needs_review", "missing") and/or note ("surname_missing").
    field_status: dict[str, str] = Field(default_factory=dict)
    field_notes: dict[str, str] = Field(default_factory=dict)
    # One entry per candidate message: the language every agent reply to it must be in
    # (null = don't check). The rule is "reply in the language of the latest message".
    reply_languages: list[Language | None] = Field(default_factory=list)


class Scenario(BaseModel):
    id: str
    description: str
    channel: Channel = "chat"
    # Needs a real LLM to understand the candidate: skipped by the offline keyword extractor.
    requires_llm: bool = False
    persona: str
    steps: list[str | dict[str, float]]
    expect: Expect


@dataclass
class ScenarioResult:
    id: str
    passed: bool
    failures: list[str]
    field_accuracy: float
    turns: int
    replies_checked: int
    replies_failed: int
    seconds: float
    transcript: list[tuple[str, str]] = field(default_factory=list)
    turn_seconds: list[float] = field(default_factory=list)
    cost_usd: float = 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "field_accuracy": round(self.field_accuracy, 3),
            "turns": self.turns,
            "replies_failed": self.replies_failed,
            "failures": self.failures,
        }


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None when there are no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * len(ordered)) - 1))]


@runtime_checkable
class Completer(Protocol):
    def complete(self, system: str, user: str, max_tokens: int = ...) -> str: ...


def _candidate_says(candidate: Completer, system: str, user: str) -> str:
    """The simulated candidate's next message. A model sometimes returns nothing: that's about the
    simulator, not the model being measured, so it mustn't end the run."""
    for attempt in range(SIMULATOR_ATTEMPTS):
        try:
            return candidate.complete(system, user).strip()
        except ValueError:
            if attempt == SIMULATOR_ATTEMPTS - 1:
                raise
    return ""


def load_scenarios(
    only: list[str] | None = None, *, offline: bool = False, path: Path = SCENARIOS
) -> list[Scenario]:
    """The scenarios to run. `offline` (the keyword extractor) leaves out those that need an LLM."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    scenarios = [Scenario.model_validate(item) for item in raw]
    if only:
        scenarios = [s for s in scenarios if s.id in only]
    if offline:
        scenarios = [s for s in scenarios if not s.requires_llm]
    return scenarios


class Harness:
    def __init__(
        self,
        config: ClientConfig,
        extractor: Extractor,
        responder: Responder,
        meter: UsageMeter | None = None,
    ) -> None:
        self.config = config
        self.extractor = extractor
        self.responder = responder
        self._engines: list[Engine] = []
        self.meter = meter
        self._turn_seconds: list[float] = []
        self._cost_start = 0.0

    def close(self) -> None:
        for engine in self._engines:
            engine.dispose()
        self._engines.clear()

    def _service(self) -> tuple[ScreeningService, FakeClock, SqlRepository]:
        self._turn_seconds = []
        self._cost_start = self.meter.cost_usd if self.meter else 0.0
        clock = FakeClock(EVAL_START)
        db = make_engine("sqlite:///:memory:")
        self._engines.append(db)
        repo = SqlRepository(db, clock, self.config.client_id)
        calendar = SqlCalendar(db, self.config, clock)
        # A fixed list of towns, never the real geocoder: evals stay offline and repeatable.
        ports = Ports(
            extractor=self.extractor,
            responder=self.responder,
            clock=clock,
            calendar=calendar,
            geocoder=FakeGeocoder(),
        )
        return ScreeningService(repo, self.config, ports), clock, repo

    def _timed(self, service: ScreeningService, cid: UUID) -> None:
        started = time.monotonic()
        service.process_pending(cid)
        self._turn_seconds.append(time.monotonic() - started)

    def run_scripted(self, scenario: Scenario) -> ScenarioResult:
        service, clock, repo = self._service()
        started = time.monotonic()
        cid = service.start_conversation(scenario.channel)
        turns = 0
        for step in scenario.steps:
            if isinstance(step, dict):
                clock.advance(hours=step["wait_hours"])
                service.send_due_reminders()
                continue
            turns += 1
            service.receive(cid, step)
            self._timed(service, cid)
        result = self._score(scenario, repo, cid, turns, time.monotonic() - started)
        self.close()
        return result

    def run_simulated(self, scenario: Scenario, candidate: Completer) -> ScenarioResult:
        service, _, repo = self._service()
        started = time.monotonic()
        cid = service.start_conversation(scenario.channel)
        system = (
            "You are role-playing a job candidate chatting with a hiring assistant on WhatsApp. "
            f"Persona: {scenario.persona}\n"
            "Reply with ONE short chat message as the candidate and nothing else. Stay in "
            "character, answer what you're asked, and keep it natural. If the assistant has "
            "clearly ended the conversation, reply exactly DONE."
        )
        turns = 0
        while turns < MAX_SIMULATED_TURNS:
            state = repo.get(cid)
            if state is None or state.stage == Stage.CLOSED:
                break
            transcript = "\n".join(
                f"{'Assistant' if m.direction == 'out' else 'Candidate'}: {m.text}"
                for m in repo.messages(cid)
            )
            message = _candidate_says(candidate, system, f"Conversation so far:\n{transcript}")
            if not message or message == "DONE":
                break
            turns += 1
            service.receive(cid, message)
            self._timed(service, cid)
        result = self._score(scenario, repo, cid, turns, time.monotonic() - started)
        self.close()
        return result

    def _score(
        self, scenario: Scenario, repo: SqlRepository, cid: UUID, turns: int, seconds: float
    ) -> ScenarioResult:
        state = repo.get(cid)
        assert state is not None
        expect = scenario.expect
        failures: list[str] = []

        def check(ok: bool, message: str) -> None:
            if not ok:
                failures.append(message)

        allowed = [expect.status] if isinstance(expect.status, str) else expect.status
        check(state.status.value in allowed, f"status {state.status.value} not in {allowed}")
        if expect.language:
            check(state.language == expect.language, f"language {state.language}")
        for reason in expect.review_reasons_include:
            check(
                reason in {str(r) for r in state.review_reasons}, f"missing review reason {reason}"
            )
        if expect.disqualify_reason:
            check(
                state.disqualify_reason == expect.disqualify_reason,
                f"disqualify_reason {state.disqualify_reason}",
            )
        if expect.keep_on_file is not None:
            check(state.keep_on_file == expect.keep_on_file, f"keep_on_file {state.keep_on_file}")
        if expect.reminders_sent is not None:
            check(
                state.reminders_sent == expect.reminders_sent, f"reminders {state.reminders_sent}"
            )
        if expect.awaiting:
            awaiting = state.awaiting.value if state.awaiting else None
            check(awaiting == expect.awaiting, f"awaiting {awaiting}")
        if expect.max_turns is not None:
            check(turns <= expect.max_turns, f"{turns} turns > {expect.max_turns}")
        messages = repo.messages(cid)
        if expect.redacted is not None:
            redacted = any(m.text == REDACTED for m in messages)
            check(redacted == expect.redacted, f"redacted {redacted}")
        if expect.interview_booked is not None:
            booked = state.interview_at is not None
            check(booked == expect.interview_booked, f"interview booked {booked}")
        all_replies = " ".join(m.text for m in messages if m.direction == "out")
        for needle in expect.reply_contains:
            check(needle in all_replies, f"no reply contains {needle!r}")
        later = [m.text for m in messages if m.direction == "out"][1:]
        for pattern in expect.later_replies_avoid:
            hits = [t for t in later if re.search(pattern, t, re.IGNORECASE)]
            check(not hits, f"{len(hits)} replies match {pattern!r}, e.g. {hits[:1]}")
        if expect.dropped_at is not None:
            check(state.dropped_at == expect.dropped_at, f"dropped_at {state.dropped_at}")
        for name, status in expect.field_status.items():
            actual_status = state.candidate.meta[FieldName(name)].status.value
            check(actual_status == status, f"{name} status {actual_status} != {status}")
        for name, note in expect.field_notes.items():
            actual_note = state.candidate.meta[FieldName(name)].note
            check(actual_note == note, f"{name} note {actual_note} != {note}")
        failures.extend(_language_failures(messages, expect.reply_languages))
        check(repo.replay(cid) == state, "the event log doesn't add up to the saved state")
        event_types = {e.type for e in repo.events(cid)}
        for event in expect.events_include:
            check(event in event_types, f"missing event {event}")

        matched = 0
        for name, expected in expect.fields.items():
            actual = _field(state, name)
            if _same(actual, expected):
                matched += 1
            else:
                failures.append(f"field {name}: {actual!r} != {expected!r}")
        accuracy = matched / len(expect.fields) if expect.fields else 1.0

        # Output guardrail on every agent message. The recap and the slot offer may be longer;
        # on voice, nothing may contain emoji or "·" lists, whatever its length.
        outbound = [m.text for m in messages if m.direction == "out"]
        bad = [t for t in outbound if not _reply_ok(t, scenario.channel, self.config)]
        if bad:
            failures.append(f"{len(bad)} replies failed the output check")

        return ScenarioResult(
            id=scenario.id,
            passed=not failures,
            failures=failures,
            field_accuracy=accuracy,
            turns=turns,
            replies_checked=len(outbound),
            replies_failed=len(bad),
            seconds=seconds,
            transcript=[(m.direction, m.text) for m in messages],
            turn_seconds=list(self._turn_seconds),
            cost_usd=(self.meter.cost_usd - self._cost_start) if self.meter else 0.0,
        )


_ES_WORDS = frozenset(
    [
        "de",
        "la",
        "el",
        "que",
        "y",
        "en",
        "tu",
        "te",
        "por",
        "para",
        "con",
        "una",
        "un",
        "es",
        "los",
        "las",
        "del",
        "se",
        "me",
        "lo",
        "al",
        "ya",
        "muy",
        "bien",
        "gracias",
        "hola",
        "perfecto",
        "vale",
        "dime",
        "tienes",
        "cuando",
        "donde",
        "como",
        "vives",
        "puedes",
        "eres",
        "estas",
        "sigues",
        "seguimos",
        "nos",
        "quedan",
        "preguntas",
        "si",
        "o",
        "jornada",
        "completa",
        "media",
        "semana",
        "fines",
        "manana",
        "tarde",
        "noche",
    ]
)
_EN_WORDS = frozenset(
    [
        "the",
        "you",
        "your",
        "to",
        "and",
        "for",
        "is",
        "are",
        "an",
        "we",
        "what",
        "do",
        "can",
        "please",
        "thanks",
        "hi",
        "great",
        "have",
        "of",
        "in",
        "it",
        "this",
        "that",
        "when",
        "where",
        "how",
        "which",
        "would",
        "could",
        "if",
        "still",
        "left",
        "questions",
        "got",
        "thank",
        "yes",
        "or",
        "more",
        "one",
        "pick",
        "sorry",
        "give",
        "moment",
        "looking",
        "answer",
        "right",
        "back",
        "full",
        "part",
        "time",
        "weekends",
        "morning",
        "afternoon",
        "evening",
    ]
)


def reply_language(text: str) -> Language | None:
    """A rough guess at the language of an agent reply, from common words (None if unclear)."""
    words = re.findall(r"[a-z]+", normalize(text))
    es = sum(w in _ES_WORDS for w in words) + 2 * (("¿" in text) + ("¡" in text))
    en = sum(w in _EN_WORDS for w in words)
    if es == en:
        return None
    return "es" if es > en else "en"


def _language_failures(messages: list[StoredMessage], expected: list[Language | None]) -> list[str]:
    """Every reply to candidate message i must be in expected[i]."""
    failures: list[str] = []
    turn = -1
    for message in messages:
        if message.direction == "in":
            turn += 1
            continue
        if turn < 0 or turn >= len(expected) or expected[turn] is None:
            continue
        found = reply_language(message.text)
        if found != expected[turn]:
            failures.append(f"reply to message {turn + 1} in {found}, not {expected[turn]}")
    return failures


def _field(state: ConversationState, name: str) -> object:
    return state.candidate.model_dump(mode="json").get(name)


def _same(actual: object, expected: object) -> bool:
    if isinstance(expected, list):
        return isinstance(actual, list) and {_norm(a) for a in actual} == {
            _norm(e) for e in expected
        }
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(actual) - float(expected)) < 0.01
    if isinstance(expected, date):
        expected = expected.isoformat()
    return _norm(actual) == _norm(expected)


def _norm(value: object) -> str:
    return normalize(str(value)) if value is not None else ""


LONG_ALLOWED = ("Repasemos", "Let's recap", "Elige un horario", "Pick a time")


def _reply_ok(text: str, channel: Channel, config: ClientConfig) -> bool:
    if "\n•" in text or text.startswith(LONG_ALLOWED):
        return channel != "voice" or speakable(text)  # length and question count exempt
    return check_reply(text, config, channel)


def run_suite(
    harness: Harness,
    scenarios: list[Scenario],
    simulator: Completer | None = None,
    budget_usd: float | None = None,
    on_result: Callable[[Scenario, ScenarioResult], None] | None = None,
) -> tuple[list[ScenarioResult], bool]:
    """Run scenarios (scripted, or simulated when a simulator is given). Stops once the meter
    passes the budget; returns the results and whether every scenario ran."""
    results: list[ScenarioResult] = []
    for scenario in scenarios:
        meter = harness.meter
        if budget_usd is not None and meter is not None and meter.cost_usd >= budget_usd:
            return results, False
        if simulator is not None:
            if any(isinstance(step, dict) for step in scenario.steps):
                continue  # waiting needs the scripted clock
            result = harness.run_simulated(scenario, simulator)
        else:
            result = harness.run_scripted(scenario)
        results.append(result)
        if on_result is not None:
            on_result(scenario, result)
    return results, True
