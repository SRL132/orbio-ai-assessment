"""Demo candidates, so a fresh local install has something on the dashboard.

Each one is a scripted chat through the real service, so the event log, the timeline and the
analytics are what a real chat would leave. The clock starts in the past and stops before now, so
the abandoned candidate's reminders are already over and nothing is dated in the future.
All invented (see evals/scenarios.yaml): written for the Grupo Sazón config and the keyword
extractor, whose replies are templates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.adapters.clock.clocks import FakeClock
from app.domain.models import Status
from app.worker.service import ScreeningService

SEED_CLIENT = "grupo_sazon"
# Long enough for the 48 h of silence, then every other chat, and still before now.
SEED_SPAN = timedelta(hours=51)
_SECONDS_BETWEEN_MESSAGES = 30
_MINUTES_BETWEEN_CANDIDATES = 10


@dataclass(frozen=True)
class DemoCandidate:
    name: str
    expected: Status
    says: tuple[str, ...]
    then_silent_for_hours: tuple[float, ...] = ()


# The silent one comes first: the clock only moves forward, and it uses most of the span.
DEMO_CANDIDATES = (
    DemoCandidate(
        "Raúl Díaz", Status.ABANDONED, ("Sí", "Raúl Díaz"), then_silent_for_hours=(2, 22, 24)
    ),
    DemoCandidate(
        "Diego Navarro",
        Status.QUALIFIED,
        (
            "Sí",
            "Diego Navarro",
            "Moto",
            "CDMX, Condesa",
            "Jornada completa",
            "Noches",
            "1 año en Rappi",
            "mañana",
            "Sí",
            "2",  # the second interview slot offered
        ),
    ),
    DemoCandidate(
        "Ana López",
        Status.QUALIFIED,
        (
            "Sí",
            "Ana López",
            "Moto",
            "Madrid, Chamberí",
            "Jornada completa",
            "Noches",
            "2 años en Glovo",
            "mañana",
            "Sí",
        ),
    ),
    DemoCandidate(
        "John Smith",
        Status.QUALIFIED,
        (
            "Yes",
            "John Smith",
            "Car",
            "Sevilla",
            "Part time",
            "Mornings",
            "never",
            "tomorrow",
            "Yes",
        ),
    ),
    DemoCandidate("Luis Pérez", Status.DISQUALIFIED, ("Sí", "Luis Pérez", "No tengo", "Sí")),
    DemoCandidate(
        "Carmen Vidal", Status.DISQUALIFIED, ("Sí", "Carmen Vidal", "Coche", "Bilbao", "Sí", "No")
    ),
    DemoCandidate(
        "Jorge Martín",
        Status.NEEDS_REVIEW,
        (
            "Sí",
            "Jorge Martín",
            "Moto",
            "Getafe",
            "Jornada completa",
            "Tardes",
            "6 meses",
            "mañana",
            "Sí",
        ),
    ),
    DemoCandidate(
        "Elena Torres", Status.NEEDS_REVIEW, ("Sí", "Elena Torres", "quiero hablar con una persona")
    ),
    DemoCandidate("Marta Ruiz", Status.IN_PROGRESS, ("Sí", "Marta Ruiz", "Moto")),
)


def seed_demo(
    service: ScreeningService, clock: FakeClock, candidates: tuple[DemoCandidate, ...]
) -> list[tuple[DemoCandidate, Status]]:
    """Play each candidate's chat. Returns each one with the status it actually reached."""
    outcomes = []
    for candidate in candidates:
        conversation_id = service.start_conversation()
        for text in candidate.says:
            clock.advance(seconds=_SECONDS_BETWEEN_MESSAGES)
            service.receive(conversation_id, text)
            service.process_pending(conversation_id)
        for hours in candidate.then_silent_for_hours:
            clock.advance(hours=hours)
            service.send_due_reminders()
        state = service.get(conversation_id)
        if state is None:
            raise RuntimeError(f"conversation {conversation_id} disappeared while seeding")
        outcomes.append((candidate, state.status))
        clock.advance(minutes=_MINUTES_BETWEEN_CANDIDATES)
    return outcomes
