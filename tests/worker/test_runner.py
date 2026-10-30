import asyncio

from app.adapters.clock.clocks import FakeClock
from app.adapters.llm.fake import ScriptedExtractor
from app.adapters.storage.sql import SqlRepository, make_engine
from app.domain.config import ClientConfig
from app.domain.ports import Ports
from app.worker.runner import Runner
from app.worker.service import ScreeningService
from tests.conftest import ex


def make_runner(config: ClientConfig, ports: Ports, clock: FakeClock, tmp_path) -> Runner:
    repo = SqlRepository(make_engine(f"sqlite:///{tmp_path / 'r.db'}"), clock)
    return Runner(ScreeningService(repo, config, ports), debounce_seconds=0.05)


async def test_debounce_merges_messages_into_one_turn(
    config: ClientConfig, ports: Ports, clock: FakeClock, extractor: ScriptedExtractor, tmp_path
) -> None:
    runner = make_runner(config, ports, clock, tmp_path)
    service = runner._service
    cid = service.start_conversation()
    extractor.push(ex(yes_no=True))
    for text in ["sí", "vale"]:
        service.receive(cid, text)
        runner.notify(cid)
    await asyncio.sleep(0.3)
    assert extractor.seen == ["sí\nvale"]
    await runner.stop()


async def test_reminder_loop_runs_and_stops(
    config: ClientConfig, ports: Ports, clock: FakeClock, tmp_path
) -> None:
    runner = make_runner(config, ports, clock, tmp_path)
    runner._service.start_conversation()
    clock.advance(hours=2)
    runner.start()
    await asyncio.sleep(0.1)
    await runner.stop()
    assert await runner.tick() == 0  # the loop already sent the due reminder


async def test_failed_turn_is_logged_not_raised(
    config: ClientConfig, ports: Ports, clock: FakeClock, tmp_path, caplog
) -> None:
    runner = make_runner(config, ports, clock, tmp_path)

    def boom(_cid: object) -> None:
        raise RuntimeError("db down")

    runner._service.process_pending = boom  # type: ignore[method-assign]
    cid = runner._service.start_conversation()
    runner.notify(cid)
    await asyncio.sleep(0.2)
    await runner.stop()
    assert "turn_failed" in caplog.text
