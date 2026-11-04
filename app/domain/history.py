"""Event sourcing: a conversation's state is what its `state_changed` events add up to.

Each save appends the top-level values that changed, so replaying a conversation's deltas in order
gives its state back. The `conversations` table is a projection of that, which `replay` can always
rebuild. The other event types record why something happened; these record what changed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.domain.models import ConversationState


def state_delta(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    """The values of `after` that differ from `before` (all of them for a new conversation)."""
    if before is None:
        return dict(after)
    return {key: value for key, value in after.items() if key not in before or before[key] != value}


def replay(deltas: Iterable[Mapping[str, Any]]) -> ConversationState | None:
    """The state these deltas add up to, oldest first, or None if there are none. Fields added to
    the model since an event was written take their defaults, as they do when reading a state."""
    merged: dict[str, Any] = {}
    for delta in deltas:
        merged.update(delta)
    return ConversationState.model_validate(merged) if merged else None
