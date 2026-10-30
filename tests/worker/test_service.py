from datetime import timedelta

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.models import Status
from app.domain.ports import Ports
from app.worker.service import ScreeningService
from tests.conftest import ex


def make_service(config: ClientConfig, ports: Ports, clock: FakeClock) -> ScreeningService:
    return ScreeningService(SqlRepository(make_engine("sqlite:///:memory:"), clock), config, ports)


def test_burst_of_messages_is_one_turn(
    config: ClientConfig, ports: Ports, clock: FakeClock, extractor: ScriptedExtractor
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    service.receive(cid, "sí")
    service.receive(cid, "  soy Ana López ")
    extractor.push(ex(yes_no=True, full_name="Ana López"))

    result = service.process_pending(cid)
    assert result is not None
    assert extractor.seen == ["sí\nsoy Ana López"]
    assert service.process_pending(cid) is None  # nothing left
    texts = [m.text for m in service.repo.messages(cid)]
    assert texts[0].startswith("¡Hola! Soy Lucía")
    assert texts[-1] == config.template("es", "ask_license")


def test_reminders_are_sent_then_abandoned(
    config: ClientConfig, ports: Ports, clock: FakeClock
) -> None:
    service = make_service(config, ports, clock)
    cid = service.start_conversation()
    assert service.send_due_reminders() == 0
    clock.advance(hours=2)
    assert service.send_due_reminders() == 1
    clock.advance(hours=22)
    assert service.send_due_reminders() == 1
    clock.advance(hours=24)
    assert service.send_due_reminders() == 1
    state = service.repo.get(cid)
    assert state is not None
    assert state.status == Status.ABANDONED
    clock.advance(hours=timedelta(days=7).total_seconds() / 3600)
    assert service.send_due_reminders() == 0
