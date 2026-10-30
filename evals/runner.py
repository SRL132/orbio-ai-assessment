"""Eval harness: run scenarios against the real engine and score the outcome.

Two modes:
- scripted: replay the scenario's candidate messages (works offline with LLM_PROVIDER=fake)
- simulated: an LLM plays the candidate from the scenario's persona (needs a provider key)

Every run uses a fresh in-memory database and a fake clock starting on Monday 2026-11-02, so
relative dates ("mañana", "el lunes") are deterministic.
"""

from __future__ import annotations

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
from app.adapters.llm.usage import UsageMeter
from app.adapters.storage.sql import REDACTED, SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import Channel, ConversationState, Stage
from app.domain.ports import Extractor, Ports, Responder
from app.domain.render import check_reply, speakable
from app.domain.validators import normalize
from app.worker.service import ScreeningService

SCENARIOS = Path(__file__).parent / "scenarios.yaml"
EVAL_START = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
MAX_SIMULATED_TURNS = 20


class Expect(BaseModel):
    status: str
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


class Scenario(BaseModel):
    id: str
    description: str
    channel: Channel = "chat"
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


def load_scenarios(only: list[str] | None = None) -> list[Scenario]:
    raw = yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))
    scenarios = [Scenario.model_validate(item) for item in raw]
    if only:
        scenarios = [s for s in scenarios if s.id in only]
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
        ports = Ports(
            extractor=self.extractor, responder=self.responder, clock=clock, calendar=calendar
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
            message = candidate.complete(system, f"Conversation so far:\n{transcript}").strip()
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

        check(
            state.status.value == expect.status, f"status {state.status.value} != {expect.status}"
        )
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
        return channel == "chat" or speakable(text)  # length and question count exempt
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
