from datetime import UTC, datetime, timedelta

from app.adapters.clock.clocks import FakeClock
from app.adapters.kb.faq import match_faq
from app.adapters.llm.fake import TemplateResponder
from app.adapters.storage.dashboard import Analytics, DashboardQueries
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    ConversationState,
    FieldName,
    FieldStatus,
    Stage,
    Status,
)
from app.domain.policy import progress

START = datetime(2026, 11, 2, 10, tzinfo=UTC)


def test_faq_matches_keywords(config: ClientConfig) -> None:
    assert match_faq("¿Eres un bot?", config, "es").startswith("Soy Lucía")
    assert match_faq("what happens after this?", config, "en").startswith("If you meet")
    assert match_faq("¿Cuánto pagan?", config, "es") is None


def test_template_responder_answers_from_faq(
    config: ClientConfig, state: ConversationState
) -> None:
    responder = TemplateResponder()
    assert "asistente virtual" in responder.compose(
        AnswerQuestion(question="¿eres un robot?"), state, config
    )
    assert responder.compose(AnswerQuestion(question="¿cuánto pagan?"), state, config) == (
        config.template("es", "answer_unknown")
    )


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
    repo = SqlRepository(engine, FakeClock(START))
    for state in states:
        repo.create(state, [], [])
    other_client = ConversationState(client_id="someone_else", status=Status.QUALIFIED)
    repo.create(other_client, [], [])  # never counted
    analytics = DashboardQueries(engine, config.client_id).analytics(config.field_priority)
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


def test_analytics_empty(config: ClientConfig) -> None:
    a = _analytics(config, [])
    assert a.completion_rate == 0.0
    assert a.median_minutes is None
