"""Event sourcing: a state is what its deltas add up to."""

from app.domain.history import replay, state_delta
from app.domain.models import ConversationState, Stage, Status


def dump(state: ConversationState) -> dict[str, object]:
    return state.model_dump(mode="json")


def test_a_new_conversation_records_its_whole_state(state: ConversationState) -> None:
    assert state_delta(None, dump(state)) == dump(state)


def test_only_what_changed_is_recorded(state: ConversationState) -> None:
    changed = state.model_copy(update={"stage": Stage.COLLECTING, "turns": 1})
    assert state_delta(dump(state), dump(changed)) == {"stage": "collecting", "turns": 1}


def test_nothing_changed_records_nothing(state: ConversationState) -> None:
    assert state_delta(dump(state), dump(state)) == {}


def test_a_field_new_to_the_model_counts_as_changed(state: ConversationState) -> None:
    before = dump(state)
    del before["turns"]
    assert state_delta(before, dump(state)) == {"turns": 0}


def test_replay_gives_the_state_back(state: ConversationState) -> None:
    middle = state.model_copy(update={"stage": Stage.COLLECTING})
    last = middle.model_copy(update={"stage": Stage.CLOSED, "status": Status.QUALIFIED})
    deltas = [
        state_delta(None, dump(state)),
        state_delta(dump(state), dump(middle)),
        state_delta(dump(middle), dump(last)),
    ]
    assert replay(deltas) == last


def test_replaying_nothing_is_no_state() -> None:
    assert replay([]) is None


def test_fields_added_since_the_event_take_their_default(state: ConversationState) -> None:
    old = dump(state)
    del old["recap_rounds"]
    replayed = replay([old])
    assert replayed is not None
    assert replayed.recap_rounds == 0
