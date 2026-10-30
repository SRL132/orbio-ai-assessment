from datetime import UTC, datetime, timedelta

from app.adapters.kb.faq import match_faq
from app.adapters.llm.fake import TemplateResponder
from app.adapters.storage.dashboard import compute_analytics
from app.domain.config import ClientConfig
from app.domain.models import (
    AnswerQuestion,
    ConversationState,
    FieldName,
    FieldStatus,
    Stage,
    Status,
)


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
    for key, value in extra.items():
        setattr(state, key, value)
    return state


def test_analytics(config: ClientConfig) -> None:
    start = datetime(2026, 11, 2, 10, tzinfo=UTC)
    states = [
        _state(
            config,
            Status.QUALIFIED,
            7,
            started_at=start,
            ended_at=start + timedelta(minutes=4),
            interview_at=start + timedelta(days=1),
            language="es",
        ),
        _state(
            config, Status.DISQUALIFIED, 2, started_at=start, ended_at=start + timedelta(minutes=2)
        ),
        _state(config, Status.ABANDONED, 1, dropped_at="license", language="en"),
        _state(config, Status.ABANDONED, 0, dropped_at="consent"),
        ConversationState(client_id=config.client_id),  # still at consent
    ]
    a = compute_analytics(states, config.field_priority)
    assert (a.started, a.consented, a.completed) == (5, 3, 2)
    assert a.completion_rate == 0.4
    assert a.drop_off == {"license": 1, "consent": 1}
    assert a.median_minutes == 4.0
    assert a.interviews_booked == 1
    funnel = dict(a.funnel)
    assert funnel[FieldName.FULL_NAME.value] == 3
    assert funnel[FieldName.LOCATION.value] == 1
    assert "finished" not in funnel
    assert a.by_language["en"] == (1, 0)


def test_analytics_empty(config: ClientConfig) -> None:
    a = compute_analytics([], config.field_priority)
    assert a.completion_rate == 0.0
    assert a.median_minutes is None
