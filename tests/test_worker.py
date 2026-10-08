import asyncio
from collections import defaultdict
from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from companion.domain import Lesson
from companion.store import Store
from companion.worker import Worker

DAY = date(2026, 10, 7)
TZ = ZoneInfo("Europe/Moscow")


async def test_restart_no_duplicate_and_cancel_first(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    s.ensure(1)
    p = s.profile(1)
    p.notifications = True
    p.travel = 50
    s.save_profile(1, p)
    a = Lesson(title="Первое", date=DAY, start="09:00", end="10:30")
    b = Lesson(title="Второе", date=DAY, start="10:40", end="12:10")
    s.replace(1, [a, b])
    bot = AsyncMock()
    worker = Worker(s, AsyncMock(), bot, defaultdict(asyncio.Lock), lambda _: None)
    at = datetime(2026, 10, 7, 7, 50, tzinfo=TZ)
    await worker.tick_user(1, at)
    assert bot.send_message.await_count == 2
    restarted = Worker(Store(s.path), AsyncMock(), bot, defaultdict(asyncio.Lock), lambda _: None)
    await restarted.tick_user(1, at)
    assert bot.send_message.await_count == 2
    s.override(1, DAY, a.id, {"cancelled": True})
    await restarted.tick_user(1, datetime(2026, 10, 7, 8, 50, tzinfo=TZ))
    assert bot.send_message.await_count == 2
    await restarted.tick_user(1, datetime(2026, 10, 7, 9, 30, tzinfo=TZ))
    assert bot.send_message.await_count == 3
    assert "Второе" in bot.send_message.call_args.args[1]


async def test_delivery_failure_retries(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    s.ensure(1)
    p = s.profile(1)
    p.notifications = True
    p.travel = 50
    s.save_profile(1, p)
    s.replace(1, [Lesson(title="Первое", date=DAY, start="09:00", end="10:30")])
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("network")
    worker = Worker(s, AsyncMock(), bot, defaultdict(asyncio.Lock), lambda _: None)
    await worker.tick_user(1, datetime(2026, 10, 7, 7, 50, tzinfo=TZ))
    assert bot.send_message.await_count == 2
    # Make retry eligible without sleeping.
    with s.connect() as db:
        db.execute("UPDATE deliveries SET next_at=0")
    bot.send_message.side_effect = None
    await worker.tick_user(1, datetime(2026, 10, 7, 7, 51, tzinfo=TZ))
    assert bot.send_message.await_count == 4


async def test_refresh_preserves_personal_events_and_manual_overrides(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    s.ensure(1)
    p = s.profile(1)
    p.source = "mirea"
    p.mirea_id = 708
    s.save_profile(1, p)
    a = Lesson(id="source-uid", title="Первое", date=DAY, start="09:00", end="10:30", room="100")
    personal = Lesson(title="Тренировка", date=DAY, start="18:00", end="19:00")
    s.replace(1, [a])
    s.add_personal(1, [personal])
    s.override(1, DAY, a.id, {"room": "777"})
    providers = AsyncMock()
    providers.mirea.return_value = [a.model_copy(update={"room": "200"})]
    worker = Worker(s, providers, AsyncMock(), defaultdict(asyncio.Lock), lambda _: None)
    await worker.refresh(1, datetime(2026, 10, 7, 7, tzinfo=TZ))
    assert len(s.lessons(1)) == 2
    assert s.personal(1)[0].title == "Тренировка"
    assert s.overrides(1, DAY)[a.id]["room"] == "777"
