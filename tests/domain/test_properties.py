"""Property tests: whatever the LLM extracts, the code-owned rules hold.

The extractor is the untrusted part of the system ("code decides, the LLM talks"), so these
tests let Hypothesis play an arbitrary, even adversarial, extractor for a whole conversation
and check the engine's invariants after every turn.
"""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

from app.adapters.clock.clocks import FakeClock
from app.adapters.config.yaml_loader import load_client_config
from app.adapters.geo.fake import FakeGeocoder
from app.adapters.llm.fake import ScriptedExtractor, TemplateResponder
from app.adapters.llm.keyword import spoken_numbers
from app.domain import engine
from app.domain.models import (
    KNOCKOUT_REASONS,
    TERMINAL_STATUSES,
    Availability,
    ConversationState,
    ExtractedFields,
    ExtractionResult,
    FieldStatus,
    InboundMessage,
    Reason,
    Schedule,
    Stage,
    Status,
    Vehicle,
)
from app.domain.ports import Ports
from app.domain.validators import normalize
from tests.conftest import said

CONFIG = load_client_config("grupo_sazon")  # module-level: Hypothesis reruns the test body
START = datetime(2026, 11, 2, 10, tzinfo=UTC)
CITIES = [loc.city for loc in CONFIG.locations] + ["Narnia", "Getafe", "Móstoles", "Toledo", ""]
ZONES = [*[z for loc in CONFIG.locations for z in loc.zones][:6], "Nowhere", ""]


class _Calendar:
    def __init__(self) -> None:
        self.booked: dict[datetime, UUID] = {}

    def available_slots(self, location_id: str, after: datetime, count: int) -> list[datetime]:
        slots = [after + timedelta(hours=h) for h in (1, 2, 3)]
        return [s for s in slots if s not in self.booked][:count]

    def book(self, location_id: str, starts_at: datetime, conversation_id: UUID) -> bool:
        holder = self.booked.setdefault(starts_at, conversation_id)
        return holder == conversation_id


def _extracted(value: st.SearchStrategy[Any]) -> st.SearchStrategy[dict[str, Any] | None]:
    item = st.fixed_dictionaries(
        {
            "value": value,
            "quote": st.sampled_from(["…", "", "sí", "Madrid", "tengo carnet"]),
            "confidence": st.floats(0, 1),
        }
    )
    return st.none() | item


fields = st.builds(
    lambda **kw: ExtractedFields.model_validate({k: v for k, v in kw.items() if v is not None}),
    full_name=_extracted(st.sampled_from(["Ana López", "Ana", "ana", "123", "  ", "X Y Z W V U"])),
    has_license=_extracted(st.booleans()),
    vehicle=_extracted(st.sampled_from(list(Vehicle))),
    license_type=_extracted(st.sampled_from(["B", "A1", "AM", "zzz"])),
    license_years=_extracted(st.floats(-5, 50, allow_nan=False)),
    city=_extracted(st.sampled_from(CITIES)),
    zone=_extracted(st.sampled_from(ZONES)),
    postal_code=_extracted(st.sampled_from(["28013", "99999", "abc"])),
    availability=_extracted(st.lists(st.sampled_from(list(Availability)), max_size=3)),
    schedule=_extracted(st.lists(st.sampled_from(list(Schedule)), max_size=4)),
    experience_years=_extracted(st.floats(-10, 100, allow_nan=False)),
    platforms=_extracted(st.lists(st.sampled_from(["Glovo", "Uber Eats", "???"]), max_size=2)),
    start_date=_extracted(st.dates(date(2026, 1, 1), date(2027, 12, 31))),
)

extractions = st.builds(
    ExtractionResult,
    fields=fields,
    language=st.sampled_from(["es", "en"]),
    intent=st.sampled_from(["answer", "question", "human_request", "off_topic", "abuse", "other"]),
    sentiment=st.sampled_from(["positive", "neutral", "frustrated"]),
    confused=st.booleans(),
    yes_no=st.none() | st.booleans(),
    sensitive_data=st.booleans(),
    question=st.none() | st.just("¿Cuánto se paga?"),
    slot_choice=st.none() | st.integers(-1, 5),
)


def _mostly(good: object, *noise: object) -> st.SearchStrategy[dict[str, Any] | None]:
    """Usually the right answer, sometimes nothing, sometimes a wrong one."""
    choices = [*[good] * 8, None, *noise]
    return st.sampled_from(choices).map(
        lambda v: None if v is None else {"value": v, "quote": str(v), "confidence": 0.9}
    )


# A mostly cooperative candidate, so conversations also reach recap, qualified and scheduling.
cooperative_fields = st.builds(
    lambda **kw: ExtractedFields.model_validate({k: v for k, v in kw.items() if v is not None}),
    full_name=_mostly("Ana López", "Ana"),
    has_license=_mostly(True, False),
    vehicle=_mostly(Vehicle.CAR, Vehicle.MOTO),
    license_type=_mostly("B", "AM"),
    city=_mostly("Madrid", "Narnia"),
    zone=_mostly("Centro", "Nowhere"),
    availability=_mostly([Availability.FULL_TIME], []),
    schedule=_mostly([Schedule.MORNING]),
    experience_years=_mostly(2.0, -1.0),
    platforms=_mostly(["Glovo"]),
    start_date=_mostly(START.date() + timedelta(days=3), START.date() - timedelta(days=3)),
)
cooperative = st.builds(
    ExtractionResult,
    fields=cooperative_fields,
    intent=st.sampled_from(["answer"] * 8 + ["question", "other", "abuse", "human_request"]),
    sentiment=st.sampled_from(["neutral"] * 5 + ["frustrated"]),
    yes_no=st.sampled_from([True, True, True, None, False]),
    sensitive_data=st.sampled_from([False] * 9 + [True]),
    slot_choice=st.sampled_from([1, 2, None, 9]),
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    script=st.lists(extractions | cooperative, min_size=1, max_size=25)
    | st.lists(cooperative, min_size=8, max_size=25),
    channel=st.sampled_from(["chat", "voice"]),
    # Half the runs quote only what the candidate wrote; the rest invent some values.
    honest=st.just([True] * 25) | st.lists(st.booleans(), min_size=25, max_size=25),
    geocoder=st.sampled_from(["none", "works", "fails"]),
)
def test_no_extraction_can_break_the_rules(
    script: list[ExtractionResult], channel: str, honest: list[bool], geocoder: str
) -> None:
    clock = FakeClock(START)
    extractor = ScriptedExtractor(script)
    geo = None if geocoder == "none" else FakeGeocoder(fail=geocoder == "fails")
    ports = Ports(extractor, TemplateResponder(), clock, _Calendar(), geo)
    state = engine.start(
        ConversationState(client_id=CONFIG.client_id, channel=channel), CONFIG, ports
    ).state
    was_terminal = False
    for extraction, grounded in zip(script, honest, strict=False):
        clock.advance(minutes=1)
        # Either the candidate wrote what the extractor quotes, or the extractor made it up.
        text = said(extraction.fields, extraction.yes_no) if grounded else "…"
        message = InboundMessage(
            conversation_id=state.conversation_id, text=text, received_at=clock.now()
        )
        before = state
        result = engine.turn(state, message, CONFIG, ports)  # never raises
        state = result.state

        assert result.replies, "every candidate message gets an answer"
        assert all(reply.strip() for reply in result.replies)
        assert state.progress >= before.progress, "an answered field never becomes unanswered"
        if text == "…":
            # Nothing in the message backs any value: none is accepted, nobody is knocked out.
            assert not [
                name
                for name, meta in state.candidate.meta.items()
                if meta.status == FieldStatus.VALID
                and before.candidate.meta[name].status != FieldStatus.VALID
            ], "a value the candidate never wrote is not accepted"
            if before.status != Status.DISQUALIFIED:
                assert state.status != Status.DISQUALIFIED
        if was_terminal and state.status not in TERMINAL_STATUSES:
            # Only two ways back: writing again after declining consent (asked again, nothing
            # collected without a yes), or a different answer to the field that knocked them out.
            declined = before.status == Status.ABANDONED and before.dropped_at == "consent"
            corrected = before.status == Status.DISQUALIFIED and before.stage == Stage.KEEP_ON_FILE
            assert declined or corrected, "a final status is never reopened"
            if declined and extraction.yes_no is not True:
                assert state.stage == Stage.CONSENT, "nothing goes past consent without a yes"
                assert state.candidate.full_name is None
        was_terminal = state.status in TERMINAL_STATUSES
        assert (state.ended_at is not None) == was_terminal
        if state.status == Status.QUALIFIED:
            # Qualifying is the code's decision: every field answered, nothing flagged.
            assert all(m.status == FieldStatus.VALID for m in state.candidate.meta.values())
            assert not state.review_reasons
            assert state.disqualify_reason is None
        if state.status == Status.DISQUALIFIED:
            assert state.disqualify_reason in KNOCKOUT_REASONS
        if state.stage == Stage.SCHEDULING:
            assert state.status == Status.QUALIFIED
            assert state.offered_slots
        if state.interview_at is not None and state.status != Status.QUALIFIED:
            # A booking only leaves "qualified" when the candidate asks a person for something
            # after it; it stands until a recruiter acts.
            assert state.status == Status.NEEDS_REVIEW
            assert {r.reason for r in state.review_reasons} & {
                Reason.INTERVIEW_CHANGE,
                Reason.HUMAN_REQUESTED,
                Reason.DATA_REQUEST,
            }
    event(f"ended {state.status.value} at {state.stage.value}")  # --hypothesis-show-statistics


@given(st.text(max_size=60))
def test_normalize_is_idempotent_and_plain(text: str) -> None:
    once = normalize(text)
    assert normalize(once) == once
    assert once == once.lower() or not once.isascii()
    assert "  " not in once
    assert once == once.strip()


@given(st.text(alphabet=st.characters(categories=["Ll", "Zs"]), max_size=60))
def test_spoken_numbers_never_crashes_and_keeps_plain_words(text: str) -> None:
    words = normalize(text)
    converted = spoken_numbers(words)
    if not any(ch.isdigit() for ch in converted):
        assert converted == " ".join(words.split())
