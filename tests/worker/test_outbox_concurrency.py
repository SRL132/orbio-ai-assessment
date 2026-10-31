"""The WhatsApp outbox sends to several conversations at once, each one still in order."""

import threading
from pathlib import Path

import httpx
import pytest

from app.adapters.calendar.sql_calendar import SqlCalendar
from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import TemplateResponder
from app.adapters.llm.keyword import KeywordExtractor
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.ports import Ports
from app.worker.service import ScreeningService
from app.worker.whatsapp import WhatsAppChannel
from tests.worker.test_whatsapp_channel import FakeWhatsApp, incoming

SLOW, FAST, DOWN = "34600000001", "34600000002", "34600000003"


class Phones(FakeWhatsApp):
    """SLOW blocks until released; DOWN fails while `down`; everyone else is instant."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.down = True

    def send(self, to: str, text: str, options: list[str] | None = None) -> str:
        if to == SLOW:
            self.release.wait(5)
        if to == DOWN and self.down:
            request = httpx.Request("POST", "https://graph.facebook.com")
            raise httpx.HTTPStatusError(
                "fail", request=request, response=httpx.Response(503, request=request)
            )
        with self.lock:
            self.sent.append((to, text, options))
        return "wamid.out"


@pytest.fixture
def outbox(config: ClientConfig, clock: FakeClock, tmp_path: Path):
    db = make_engine(f"sqlite:///{tmp_path / 'outbox.db'}")  # threads share it
    ports = Ports(KeywordExtractor(), TemplateResponder(), clock, SqlCalendar(db, config, clock))
    service = ScreeningService(SqlRepository(db, clock, config.client_id), config, ports)
    phones = Phones()
    channel = WhatsAppChannel(service, phones, concurrency=4)  # type: ignore[arg-type]
    yield service, channel, phones
    phones.release.set()
    db.dispose()


def start(service: ScreeningService, channel: WhatsAppChannel, phone: str) -> None:
    for cid in channel.handle(incoming("hola", phone)):
        service.process_pending(cid)


def test_a_slow_recipient_does_not_hold_up_the_others(outbox) -> None:
    service, channel, phones = outbox
    for phone in (SLOW, FAST):
        start(service, channel, phone)
    done = threading.Event()
    result: list[int] = []

    def dispatch() -> None:
        result.append(channel.dispatch_once())
        done.set()

    threading.Thread(target=dispatch).start()
    # FAST is delivered while SLOW is still waiting on its send.
    for _ in range(500):
        if any(to == FAST for to, _, _ in phones.sent):
            break
        done.wait(0.01)
    assert [to for to, _, _ in phones.sent] == [FAST]
    phones.release.set()
    assert done.wait(5)
    assert result == [2]


def test_a_failing_recipient_keeps_its_order_and_others_still_go(outbox) -> None:
    service, channel, phones = outbox
    phones.release.set()
    for phone in (DOWN, FAST):
        start(service, channel, phone)
    for cid in channel.handle(incoming("sí", DOWN)):  # a second reply queued for DOWN
        service.process_pending(cid)
    assert channel.dispatch_once() == 1  # FAST went; DOWN's first failed, its second waited
    assert [to for to, _, _ in phones.sent] == [FAST]

    phones.down = False  # DOWN recovers: both its messages go, first one first
    assert channel.dispatch_once() == 2
    down_texts = [text for to, text, _ in phones.sent if to == DOWN]
    assert down_texts[0].startswith("¡Hola!")
    assert len(down_texts) == 2
