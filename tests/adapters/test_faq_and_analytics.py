from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.adapters.clock.clocks import FakeClock
from app.adapters.kb.faq import match_faq
from app.adapters.storage.dashboard import Analytics, DashboardQueries
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import (
    ConversationState,
    FieldName,
    FieldStatus,
    Stage,
    Status,
)
from app.domain.policy import progress
from app.domain.render import check_reply

START = datetime(2026, 11, 2, 10, tzinfo=UTC)


def test_faq_matches_keywords(config: ClientConfig) -> None:
    assert match_faq("¿Eres un bot?", config) == "bot"
    assert match_faq("what happens after this?", config) == "next_steps"
    assert match_faq("¿Cuánto pagan?", config) == "pay"
    assert match_faq("¿Dais seguro médico privado?", config) is None


def test_every_faq_answer_can_be_sent_as_written(config: ClientConfig) -> None:
    """They reach candidates word for word, on chat and read aloud on voice."""
    assert len(config.faq) >= 10
    for entry in config.faq:
        for language in ("es", "en"):
            assert check_reply(entry.answer[language], config, "voice"), entry.id
            assert "?" not in entry.answer[language], entry.id


def test_pay_answer_has_no_figure(config: ClientConfig) -> None:
    """The demo client gave no pay data, so no number may be promised."""
    pay = next(entry for entry in config.faq if entry.id == "pay")
    assert not any(ch.isdigit() for text in pay.answer.values() for ch in text)


def test_faq_ids_must_be_unique(config: ClientConfig) -> None:
    data = config.model_dump()
    data["faq"][1]["id"] = data["faq"][0]["id"]
    with pytest.raises(ValidationError, match="unique"):
        ClientConfig.model_validate(data)


def _state(
    config: ClientConfig, status: Status, answered: int, **extra: object
) -> ConversationState:
    state = ConversationState(client_id=config.client_id, status=status, stage=Stage.COLLECTING)
    for name in config.field_priority[:answered]:
        state.candidate.meta[name].status = FieldStatus.VALID
    state.progress = progress(state.candidate, config)
    for key, value in extra.items():
        setattr(state, key, value)
    return state


def _analytics(config: ClientConfig, states: list[ConversationState]) -> Analytics:
    engine = make_engine("sqlite:///:memory:")
    repo = SqlRepository(engine, FakeClock(START), config.client_id)
    for state in states:
        repo.create(state, [], [])
    other_client = ConversationState(client_id="someone_else", status=Status.QUALIFIED)
    SqlRepository(engine, FakeClock(START), "someone_else").create(other_client, [], [])
    analytics = DashboardQueries(engine, config.client_id).analytics(config.field_priority, START)
    engine.dispose()
    return analytics


def test_analytics(config: ClientConfig) -> None:
    states = [
        _state(
            config,
            Status.QUALIFIED,
            7,
            started_at=START,
            ended_at=START + timedelta(minutes=4),
            interview_at=START + timedelta(days=1),
            language="es",
        ),
        _state(
            config, Status.DISQUALIFIED, 2, started_at=START, ended_at=START + timedelta(minutes=2)
        ),
        _state(config, Status.ABANDONED, 1, dropped_at="license", language="en"),
        _state(config, Status.ABANDONED, 0, dropped_at="consent"),
        ConversationState(client_id=config.client_id),  # still at consent
    ]
    a = _analytics(config, states)
    assert (a.started, a.consented, a.completed) == (5, 3, 2)
    assert a.completion_rate == 0.4
    assert a.outcomes["abandoned"] == 2
    assert a.outcomes["needs_review"] == 0
    assert a.drop_off == {"license": 1, "consent": 1}
    assert a.median_minutes == 4.0
    assert a.interviews_booked == 1
    funnel = dict(a.funnel)
    assert funnel[FieldName.FULL_NAME.value] == 3
    assert funnel[FieldName.LOCATION.value] == 1
    assert "finished" not in funnel
    assert a.by_language["en"] == (1, 0)
    assert a.by_location == {"unknown": (5, 2)}


def test_analytics_compares_agent_versions(config: ClientConfig) -> None:
    a = _analytics(
        config,
        [
            _state(config, Status.QUALIFIED, 7, agent_version="a"),
            _state(config, Status.ABANDONED, 2, agent_version="a"),
            _state(config, Status.QUALIFIED, 7, agent_version="b"),
            _state(config, Status.QUALIFIED, 7),
        ],
    )
    assert a.by_version == {"a": (2, 1), "b": (1, 1), "unknown": (1, 1)}


def test_analytics_empty(config: ClientConfig) -> None:
    a = _analytics(config, [])
    assert a.completion_rate == 0.0
    assert a.median_minutes is None


def test_share_screened_within_target_hours(config: ClientConfig) -> None:
    hour = timedelta(hours=1)
    now = START + 48 * hour
    states = [
        # Applied 2 days ago via the ATS, screened 3 h later: on time.
        _state(
            config,
            Status.QUALIFIED,
            7,
            applied_at=START,
            started_at=START + hour,
            ended_at=START + 3 * hour,
        ),
        # Applied 2 days ago, only started the chat a day later: late, whatever the chat took.
        _state(
            config,
            Status.DISQUALIFIED,
            2,
            applied_at=START,
            started_at=START + 30 * hour,
            ended_at=START + 30.1 * hour,
        ),
        # No application date: counted from the chat's start; never finished, window closed.
        _state(config, Status.ABANDONED, 1, started_at=START, ended_at=START + 47 * hour),
        # Started an hour ago, still talking: inside the window, not counted yet.
        _state(config, Status.IN_PROGRESS, 1, started_at=now - hour),
        # Started an hour ago and already done: counts, on time.
        _state(config, Status.NEEDS_REVIEW, 3, started_at=now - hour, ended_at=now),
    ]
    engine = make_engine("sqlite:///:memory:")
    repo = SqlRepository(engine, FakeClock(START), config.client_id)
    for state in states:
        repo.create(state, [], [])
    a = DashboardQueries(engine, config.client_id).analytics(config.field_priority, now, 24.0)
    engine.dispose()
    assert (a.screened_on_time, a.screening_due) == (2, 4)
    assert a.screened_on_time_rate == 0.5
    assert a.target_hours == 24.0


def test_screened_on_time_rate_is_none_without_due_applicants(config: ClientConfig) -> None:
    a = _analytics(config, [ConversationState(client_id=config.client_id, started_at=START)])
    assert a.screening_due == 0
    assert a.screened_on_time_rate is None
