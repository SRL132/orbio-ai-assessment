import asyncio

import pytest

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.ports import Ports
from app.worker.runner import Runner
from app.worker.service import ScreeningService
from tests.conftest import ex


@pytest.fixture
def runner(config: ClientConfig, ports: Ports, clock: FakeClock, tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    yield Runner(
        ScreeningService(SqlRepository(engine, clock, config.client_id), config, ports),
        debounce_seconds=0.05,
    )
    engine.dispose()


async def test_debounce_merges_messages_into_one_turn(
    runner: Runner, extractor: ScriptedExtractor
) -> None:
    service = runner._service
    cid = service.start_conversation()
    extractor.push(ex(yes_no=True))
    for text in ["sí", "vale"]:
        service.receive(cid, text)
        runner.notify(cid)
    await asyncio.sleep(0.3)
    assert extractor.seen == ["sí\nvale"]
    await runner.stop()


async def test_reminder_loop_runs_and_stops(runner: Runner, clock: FakeClock) -> None:
    runner._service.start_conversation()
    clock.advance(hours=2)
    runner.start()
    await asyncio.sleep(0.1)
    await runner.stop()
    assert await runner.tick() == 0  # the loop already sent the due reminder


async def test_failed_turn_is_logged_not_raised(runner: Runner, caplog) -> None:

    def boom(_cid: object) -> None:
        raise RuntimeError("db down")

    runner._service.process_pending = boom  # type: ignore[method-assign]
    cid = runner._service.start_conversation()
    runner.notify(cid)
    await asyncio.sleep(0.2)
    await runner.stop()
    assert "turn_failed" in caplog.text
