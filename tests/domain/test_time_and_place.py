"""Time passing and places sharing a name: a stale slot or date must not stand, "today" is the
candidate's own, a qualified candidate who goes quiet reaches a person, and a city name alone can't
decide the country (docs/failure-modes.md, gaps 1-5)."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import TemplateResponder
from app.domain import engine
from app.domain.config import ClientConfig
from app.domain.models import (
    Availability,
    ConversationState,
    EventType,
    ExtractionResult,
    FieldName,
    FieldStatus,
    InboundMessage,
    Reason,
    ReviewReason,
    Schedule,
    Stage,
    Status,
    Vehicle,
)
from app.domain.ports import Ports
from app.domain.validators import COUNTRY_READBACK, validate_location
from tests.conftest import START, ex, said


class SpyExtractor:
    """Scripted answers, and a record of the date each call was told it is."""

    def __init__(self) -> None:
        self.script: list[ExtractionResult] = []
        self.today: list[date] = []

    def extract(
        self, state: ConversationState, text: str, config: ClientConfig, today: date
    ) -> ExtractionResult:
        self.today.append(today)
        return self.script.pop(0) if self.script else ExtractionResult(language=state.language)


class Calendar:
    """Like SqlCalendar.book, this has no check on the slot's time: the engine must refuse it."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.booked: list[datetime] = []

    def available_slots(self, location_id: str, after: datetime, count: int) -> list[datetime]:
        return [after + timedelta(days=d) for d in range(count)]

    def book(self, location_id: str, starts_at: datetime, conversation_id: UUID) -> bool:
        self.booked.append(starts_at)
        return True


class Chat:
    def __init__(self, config: ClientConfig, start: datetime = START, channel: str = "chat"):
        self.config = config
        self.clock = FakeClock(start)
        self.extractor = SpyExtractor()
        self.calendar: Calendar | None = Calendar(self.clock)
        self.ports = Ports(self.extractor, TemplateResponder(), self.clock, self.calendar)
        state = ConversationState(client_id=config.client_id, channel=channel)
        self.result = engine.start(state, config, self.ports)

    @property
    def state(self) -> ConversationState:
        return self.result.state

    def say(self, extraction: ExtractionResult, text: str | None = None):
        self.extractor.script.append(extraction)
        message = InboundMessage(
            conversation_id=UUID(int=1),
            text=said(extraction.fields, extraction.yes_no) if text is None else text,
            received_at=self.clock.now(),
        )
        self.result = engine.turn(self.state, message, self.config, self.ports)
        return self.result

    def silence(self):
        self.clock._now = self.state.next_reminder_at
        result = engine.on_silence(self.state, self.config, self.ports)
        assert result is not None
        self.result = result
        return result

    def event_types(self) -> list[EventType]:
        return [event.type for event in self.result.events]

    def to_location(self) -> None:
        self.say(ex(yes_no=True))
        self.say(ex(full_name="Ana López", has_license=True, vehicle=Vehicle.MOTO))

    def to_recap(self, start_date: date | None = None) -> None:
        self.to_location()
        self.say(ex(city="Madrid", zone="Retiro", availability=[Availability.FULL_TIME]))
        self.say(ex(schedule=[Schedule.MORNING], experience_years=1))
        self.say(ex(start_date=start_date or self.clock.now().date() + timedelta(days=3)))
        assert self.state.stage == Stage.RECAP

    def to_scheduling(self) -> None:
        self.to_recap()
        self.say(ex(yes_no=True))
        assert self.state.stage == Stage.SCHEDULING


@pytest.fixture
def chat(config: ClientConfig) -> Chat:
    return Chat(config)


# --- Gap 1: a city name shared by two countries ------------------------------------------------


class TestSharedCityNames:
    @pytest.mark.parametrize(
        ("city", "location_id"),
        [
            ("Guadalajara", "mx-guadalajara"),
            ("León", "mx-leon"),
            ("Mérida", "mx-merida"),
            ("Córdoba", "es-cordoba"),
            ("Valladolid", "es-valladolid"),
        ],
    )
    def test_the_name_alone_needs_the_country_confirmed(
        self, config: ClientConfig, city: str, location_id: str
    ) -> None:
        outcome = validate_location(city, None, None, config)
        assert (outcome.kind, outcome.note) == ("valid", COUNTRY_READBACK)
        assert outcome.note == "country_readback"
        assert outcome.updates == {"location_id": location_id, "city": city, "zone": None}

    def test_a_city_no_other_country_has_needs_no_read_back(self, config: ClientConfig) -> None:
        assert validate_location("Monterrey", None, None, config).note is None

    def test_its_own_postal_code_settles_it(self, config: ClientConfig) -> None:
        outcome = validate_location("Guadalajara", None, "44100", config)
        assert (outcome.kind, outcome.note) == ("valid", None)
        assert outcome.updates["location_id"] == "mx-guadalajara"

    @pytest.mark.parametrize(
        ("city", "postal_code"),
        [("Guadalajara", "19001"), ("León", "24001"), ("Córdoba", "94500"), ("Monterrey", "28010")],
    )
    def test_a_postal_code_from_elsewhere_goes_to_review(
        self, config: ClientConfig, city: str, postal_code: str
    ) -> None:
        outcome = validate_location(city, "Centro", postal_code, config)
        assert (outcome.kind, outcome.reason) == ("review", Reason.POSTAL_CODE_NOT_LISTED)
        assert outcome.updates == {"city": city, "postal_code": postal_code, "zone": "Centro"}
        assert "location_id" not in outcome.updates

    def test_a_zone_is_kept_with_the_read_back(self, config: ClientConfig) -> None:
        assert validate_location("Mérida", "Centro", None, config).updates["zone"] == "Centro"

    @pytest.mark.parametrize(
        ("language", "words"),
        [
            ("es", "Solo para confirmar: Guadalajara, en México, ¿correcto?"),
            ("en", "Just to check: Guadalajara, in Mexico, right?"),
        ],
    )
    def test_the_read_back_names_the_country(self, chat: Chat, language: str, words: str) -> None:
        chat.to_location()
        chat.state.language = language
        chat.extractor.script.clear()
        result = chat.say(ex(city="Guadalajara", language=language))
        assert result.replies == [words]
        assert chat.state.candidate.location_id is None

    def test_yes_applies_it(self, chat: Chat) -> None:
        chat.to_location()
        chat.say(ex(city="Guadalajara"))
        chat.say(ex(yes_no=True), text="sí")
        candidate = chat.state.candidate
        assert candidate.location_id == "mx-guadalajara"
        assert candidate.meta[FieldName.LOCATION].status == FieldStatus.VALID

    def test_no_asks_again_and_counts_an_attempt(self, chat: Chat) -> None:
        chat.to_location()
        chat.say(ex(city="Guadalajara"))
        chat.say(ex(yes_no=False), text="no")
        assert chat.state.awaiting == FieldName.LOCATION
        assert chat.state.candidate.meta[FieldName.LOCATION].attempts == 1
        assert chat.state.candidate.location_id is None

    def test_repeating_the_name_goes_to_a_person(self, chat: Chat) -> None:
        """ "No, Guadalajara de España": asking again would only read the same thing back."""
        chat.to_location()
        chat.say(ex(city="Guadalajara"))
        chat.say(ex(yes_no=False, city="Guadalajara"), text="no, Guadalajara de España")
        candidate = chat.state.candidate
        assert candidate.location_id is None
        assert candidate.meta[FieldName.LOCATION].status == FieldStatus.NEEDS_REVIEW
        assert chat.state.confirming is None
        assert (
            ReviewReason(reason=Reason.FIELD_FAILED_TWICE, field=FieldName.LOCATION)
            in chat.state.review_reasons
        )

    def test_naming_another_place_is_checked_as_usual(self, chat: Chat) -> None:
        chat.to_location()
        chat.say(ex(city="Guadalajara"))
        chat.say(ex(yes_no=False, city="Monterrey"), text="no, Monterrey")
        assert chat.state.candidate.location_id == "mx-monterrey"

    def test_a_config_without_the_country_name_is_refused(self, config: ClientConfig) -> None:
        data = config.model_dump()
        data["country_names"] = {}
        with pytest.raises(ValidationError, match="country_names needs MX"):
            ClientConfig.model_validate(data)


# --- Gap 2: "today" where the candidate is -----------------------------------------------------


class TestLocalDate:
    # 20:00 on 6 Nov in Mexico City, already 7 Nov in UTC and in Madrid.
    EVENING_IN_MEXICO = datetime(2026, 11, 7, 2, 0, tzinfo=UTC)

    def in_monterrey(self, config: ClientConfig) -> Chat:
        chat = Chat(config, self.EVENING_IN_MEXICO)
        chat.to_location()
        chat.say(ex(city="Monterrey"))
        chat.extractor.today.clear()
        return chat

    def test_the_extractor_gets_the_candidates_date(self, config: ClientConfig) -> None:
        chat = self.in_monterrey(config)
        chat.say(ex(availability=[Availability.FULL_TIME]))
        assert chat.extractor.today == [date(2026, 11, 6)]

    def test_their_today_is_not_in_the_past(self, config: ClientConfig) -> None:
        chat = self.in_monterrey(config)
        chat.say(
            ex(
                availability=[Availability.FULL_TIME],
                schedule=[Schedule.MORNING],
                experience_years=1,
                start_date=date(2026, 11, 6),
            )
        )
        assert chat.state.candidate.start_date == date(2026, 11, 6)

    def test_their_yesterday_still_is(self, config: ClientConfig) -> None:
        chat = self.in_monterrey(config)
        chat.say(ex(start_date=date(2026, 11, 5)))
        assert chat.state.candidate.start_date is None

    def test_before_the_location_the_client_default_applies(self, config: ClientConfig) -> None:
        # 00:30 on 3 Nov in Madrid is still 2 Nov in UTC.
        chat = Chat(config, datetime(2026, 11, 2, 23, 30, tzinfo=UTC))
        chat.say(ex(yes_no=True))
        assert chat.extractor.today == [date(2026, 11, 3)]

    def test_the_default_comes_from_the_config(self, config: ClientConfig) -> None:
        other = config.model_copy(update={"default_timezone": "America/Tijuana"})
        chat = Chat(other, datetime(2026, 11, 3, 2, 0, tzinfo=UTC))
        chat.say(ex(yes_no=True))
        assert chat.extractor.today == [date(2026, 11, 2)]

    def test_a_chat_that_has_closed_uses_it_too(self, config: ClientConfig) -> None:
        chat = Chat(config, datetime(2026, 11, 2, 23, 30, tzinfo=UTC))
        chat.say(ex(yes_no=False), text="no")
        chat.extractor.today.clear()
        chat.state.stage = Stage.CLOSED
        chat.say(ExtractionResult(), text="gracias")
        assert chat.extractor.today == [date(2026, 11, 3)]

    @pytest.mark.parametrize("field", ["default_timezone"])
    def test_a_bad_timezone_is_refused_at_startup(self, config: ClientConfig, field: str) -> None:
        data = config.model_dump()
        data[field] = "Europe/Atlantis"
        with pytest.raises(Exception, match="Atlantis"):
            ClientConfig.model_validate(data)

    def test_a_bad_location_timezone_is_refused_at_startup(self, config: ClientConfig) -> None:
        data = config.model_dump()
        data["locations"][0]["timezone"] = "Europe/Atlantis"
        with pytest.raises(Exception, match="Atlantis"):
            ClientConfig.model_validate(data)


# --- Gap 3: a slot that has passed -------------------------------------------------------------


class TestExpiredSlots:
    def test_picking_one_books_nothing_and_offers_new_ones(self, chat: Chat) -> None:
        chat.to_scheduling()
        stale = chat.state.offered_slots
        chat.clock.advance(days=30)
        result = chat.say(ExtractionResult(slot_choice=1), text="la 1")
        assert chat.calendar.booked == []
        assert chat.state.interview_at is None
        assert chat.state.stage == Stage.SCHEDULING
        assert all(slot > chat.clock.now() for slot in chat.state.offered_slots)
        assert chat.state.offered_slots != stale
        assert result.replies[0] == "Esos horarios ya han pasado. Te propongo otros."
        assert EventType.SLOTS_EXPIRED in chat.event_types()

    def test_a_slot_starting_right_now_has_passed(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.clock._now = chat.state.offered_slots[0]
        chat.say(ExtractionResult(slot_choice=1), text="la 1")
        assert chat.calendar.booked == []

    def test_one_still_ahead_is_booked(self, chat: Chat) -> None:
        chat.to_scheduling()
        slots = chat.state.offered_slots
        chat.clock._now = slots[1] - timedelta(minutes=1)
        chat.say(ExtractionResult(slot_choice=2), text="la 2")
        assert chat.state.interview_at == slots[1]
        assert chat.calendar.booked == [slots[1]]

    def test_a_stale_list_is_not_shown_again(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.clock.advance(days=30)
        chat.say(ExtractionResult(), text="hmm")
        assert all(slot > chat.clock.now() for slot in chat.state.offered_slots)
        assert EventType.SLOTS_EXPIRED in chat.event_types()

    def test_a_fresh_list_is_shown_again_as_before(self, chat: Chat) -> None:
        chat.to_scheduling()
        slots = chat.state.offered_slots
        chat.say(ExtractionResult(), text="hmm")
        assert chat.state.offered_slots == slots
        assert EventType.SLOTS_EXPIRED not in chat.event_types()

    def test_with_nothing_left_to_offer_a_recruiter_arranges_it(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.clock.advance(days=30)
        chat.calendar.available_slots = lambda *_: []
        result = chat.say(ExtractionResult(slot_choice=1), text="la 1")
        assert chat.state.stage == Stage.CLOSED
        assert result.replies[-1] == chat.config.template("es", "qualified_no_slots")

    def test_having_said_none_fit_is_forgotten_with_the_old_list(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.say(ex(yes_no=False), text="no")
        assert chat.state.no_slot_fits
        chat.clock.advance(days=30)
        chat.say(ExtractionResult(slot_choice=1), text="la 1")
        assert not chat.state.no_slot_fits


# --- Gap 4: qualified, but no slot picked ------------------------------------------------------


class TestNoSlotPicked:
    def test_a_reminder_is_scheduled(self, chat: Chat) -> None:
        chat.to_scheduling()
        assert chat.state.next_reminder_at == chat.clock.now() + timedelta(hours=2)

    def test_the_reminders_ask_for_a_slot_then_a_person_takes_over(self, chat: Chat) -> None:
        chat.to_scheduling()
        first = chat.silence()
        assert first.replies == [chat.config.template("es", "reminder_1_slots")]
        assert chat.state.status == Status.QUALIFIED
        second = chat.silence()
        assert second.replies == [chat.config.template("es", "reminder_2_slots")]
        assert chat.state.status == Status.QUALIFIED

        qualified_at = chat.state.ended_at
        last = chat.silence()
        assert last.replies == []
        assert chat.state.status == Status.NEEDS_REVIEW
        assert chat.state.stage == Stage.CLOSED
        assert chat.state.offered_slots == []
        assert chat.state.next_reminder_at is None
        assert chat.state.ended_at == qualified_at
        assert ReviewReason(reason=Reason.NO_SLOT_PICKED) in chat.state.review_reasons
        assert EventType.NO_SLOT_PICKED in chat.event_types()
        assert not chat.state.went_silent
        assert "did not pick an interview slot" in chat.state.summary

    def test_booking_stops_the_reminders(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.say(ExtractionResult(slot_choice=1), text="la 1")
        assert chat.state.stage == Stage.CLOSED
        assert chat.state.next_reminder_at is None

    def test_giving_preferred_times_stops_them(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.say(ex(yes_no=False), text="no")
        assert chat.state.next_reminder_at is not None
        chat.say(ExtractionResult(), text="los jueves por la tarde")
        assert chat.state.stage == Stage.CLOSED
        assert chat.state.next_reminder_at is None

    def test_a_reply_starts_the_reminders_over(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.silence()
        assert chat.state.reminders_sent == 1
        chat.say(ExtractionResult(), text="hmm")
        assert chat.state.reminders_sent == 0
        assert chat.state.next_reminder_at == chat.clock.now() + timedelta(hours=2)

    def test_a_closed_qualified_candidate_gets_no_reminder(self, chat: Chat) -> None:
        chat.to_scheduling()
        chat.say(ExtractionResult(slot_choice=1), text="la 1")
        chat.state.next_reminder_at = chat.clock.now()
        assert engine.on_silence(chat.state, chat.config, chat.ports) is None


# --- Gap 5: a start date that passes before the recap's yes ------------------------------------


class TestStartDateExpired:
    def test_it_is_asked_again_and_counts_against_nobody(self, chat: Chat) -> None:
        chat.to_recap()
        chat.clock.advance(days=10)
        result = chat.say(ex(yes_no=True), text="sí")
        candidate = chat.state.candidate
        assert chat.state.status == Status.IN_PROGRESS
        assert chat.state.stage == Stage.COLLECTING
        assert chat.state.awaiting == FieldName.START_DATE
        assert candidate.start_date is None
        assert candidate.meta[FieldName.START_DATE].status == FieldStatus.MISSING
        assert candidate.meta[FieldName.START_DATE].attempts == 0
        assert candidate.meta[FieldName.START_DATE].quote is None
        assert result.replies[0] == "La fecha de inicio que me diste ya ha pasado."
        assert EventType.START_DATE_EXPIRED in chat.event_types()

    def test_a_new_date_leads_back_to_the_recap_and_qualifies(self, chat: Chat) -> None:
        chat.to_recap()
        chat.clock.advance(days=10)
        chat.say(ex(yes_no=True), text="sí")
        chat.say(ex(start_date=chat.clock.now().date() + timedelta(days=1)))
        assert chat.state.stage == Stage.RECAP
        chat.say(ex(yes_no=True), text="sí")
        assert chat.state.status == Status.QUALIFIED

    def test_today_still_counts(self, chat: Chat) -> None:
        start = chat.clock.now().date() + timedelta(days=3)
        chat.to_recap(start)
        chat.clock._now = datetime(start.year, start.month, start.day, 12, 0, tzinfo=UTC)
        chat.say(ex(yes_no=True), text="sí")
        assert chat.state.status == Status.QUALIFIED
        assert chat.state.candidate.start_date == start

    def test_a_field_that_gave_up_has_no_date_to_check(self, chat: Chat) -> None:
        chat.to_location()
        chat.say(ex(city="Madrid", zone="Retiro", availability=[Availability.FULL_TIME]))
        chat.say(ex(schedule=[Schedule.MORNING], experience_years=1))
        chat.say(ExtractionResult(), text="ni idea")
        chat.say(ExtractionResult(), text="ni idea")
        assert chat.state.stage == Stage.RECAP
        chat.say(ex(yes_no=True), text="sí")
        assert chat.state.status == Status.NEEDS_REVIEW
