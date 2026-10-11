import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx

from companion.configuration import Configuration, prepare, snapshot
from companion.domain import Lesson, Reminder
from companion.journey import extra_notices, sleep_times
from companion.providers import Providers
from companion.service import briefing, day_items, weather_report
from companion.store import Store
from companion.worker import Worker

DAY = date(2026, 10, 12)
TZ = ZoneInfo("Europe/Moscow")


def at(hour, minute=0, day=DAY):
    return datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=hour, minutes=minute)


def environment(tmp_path):
    store = Store(tmp_path / "db.sqlite3")
    store.ensure(1)
    p = store.profile(1)
    p.notifications = True
    p.quiet_start = p.quiet_end = "00:00"
    p.fixed_briefing = "06:00"
    p.travel = p.preparation = p.buffer = p.campus_walk = p.departure_lead = 0
    store.save_profile(1, p)
    providers, bot = AsyncMock(), AsyncMock()
    worker = Worker(store, providers, bot, defaultdict(asyncio.Lock), lambda _: None)
    return store, providers, bot, worker


async def test_each_weather_report_fetches_new_forecast(tmp_path):
    store, _, _, _ = environment(tmp_path)
    p = store.profile(1)
    p.latitude, p.longitude = 55.7, 37.6
    calls = []

    def respond(request):
        calls.append(request)
        hourly = {"time": [at(i).replace(tzinfo=None).isoformat() for i in range(24)]}
        for key in (
            "temperature_2m",
            "apparent_temperature",
            "precipitation_probability",
            "precipitation",
            "wind_speed_10m",
        ):
            hourly[key] = [len(calls) * 5] * 24
        return httpx.Response(200, json={"hourly": hourly})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        providers = Providers(client)
        first = await weather_report(providers, p, DAY)
        second = await weather_report(providers, p, DAY)
    assert len(calls) == 2 and "+5.0 °C" in first and "+10.0 °C" in second


async def test_due_reminder_rechecks_recent_schedule_and_moves_time(tmp_path):
    store, providers, bot, worker = environment(tmp_path)
    p = store.profile(1)
    p.source, p.mirea_id, p.source_checked = "mirea", 1, at(8, 49).isoformat()
    store.save_profile(1, p)
    old = Lesson(id="a", title="A", date=DAY, start="09:00", end="10:00", room="100")
    moved = old.model_copy(update={"start": "10:00", "end": "11:00", "room": "200"})
    store.replace(1, [old])
    providers.mirea.return_value = [moved]
    await worker.tick_user(1, at(8, 50))
    providers.mirea.assert_awaited_once()
    assert all("Следующая пара" not in call.args[1] for call in bot.send_message.call_args_list)
    await worker.tick_user(1, at(9, 50))
    assert "10:00" in bot.send_message.call_args.args[1] and "200" in bot.send_message.call_args.args[1]
    count = bot.send_message.await_count
    refresh_count = providers.mirea.await_count
    await worker.tick_user(1, at(9, 50, day=DAY) + timedelta(seconds=20))
    assert bot.send_message.await_count == count
    assert providers.mirea.await_count == refresh_count


async def test_periodic_check_detects_earlier_lesson(tmp_path):
    store, providers, bot, worker = environment(tmp_path)
    p = store.profile(1)
    p.source, p.mirea_id, p.source_checked = "mirea", 1, at(8, 30).isoformat()
    store.save_profile(1, p)
    old = Lesson(id="a", title="A", date=DAY, start="10:00", end="11:00")
    store.replace(1, [old])
    providers.mirea.return_value = [old.model_copy(update={"start": "09:00", "end": "10:00"})]
    await worker.tick_user(1, at(8, 50))
    assert "Следующая пара в 09:00" in bot.send_message.call_args.args[1]


async def test_unavailable_source_marks_saved_schedule(tmp_path):
    store, providers, bot, worker = environment(tmp_path)
    p = store.profile(1)
    p.source, p.mirea_id = "mirea", 1
    store.save_profile(1, p)
    store.replace(1, [Lesson(title="A", date=DAY, start="09:00", end="10:00")])
    providers.mirea.side_effect = httpx.ConnectError("offline")
    await worker.tick_user(1, at(8, 50))
    assert "последнее сохранённое расписание" in bot.send_message.call_args.args[1]


async def test_sleep_only_static_config_and_no_source_or_weather(tmp_path):
    store, providers, bot, worker = environment(tmp_path)
    p = store.profile(1)
    p.source, p.mirea_id = "mirea", 1
    p.reminders = [Reminder(id="other", text="Other", trigger="time", time="20:00")]
    store.save_profile(1, p)
    original = [Lesson(title="МИРЭА", date=DAY, start="06:00", end="07:00")]
    store.replace(1, original)
    config = Configuration.model_validate(
        {
            "format": "day-companion/v2",
            "mode": "patch",
            "base_revision": snapshot(store, 1)["base_revision"],
            "profile": {
                "routine_mode": "sleep_only",
                "travel": 60,
                "preparation": 60,
                "sleep": {"enabled": True, "hours": 8, "latency_minutes": 0, "free_day_wake": "09:00"},
                "sleep_schedule": [
                    {"id": "work", "title": "Работа", "weekday": 0, "start": "09:00", "end": "18:00"}
                ],
            },
        }
    )
    store.apply_config(1, *prepare(store, 1, config)[0])
    p = store.profile(1)
    times = sleep_times(day_items(store, 1, DAY), p, DAY)
    assert times[0] == at(7) and times[1] == at(23, day=DAY - timedelta(days=1))
    await worker.tick_user(1, at(22, 30, day=DAY - timedelta(days=1)))
    assert bot.send_message.await_count == 1 and "Лечь: 23:00" in bot.send_message.call_args.args[1]
    await worker.tick_user(1, at(7))
    providers.mirea.assert_not_awaited()
    assert bot.send_message.await_count == 1
    text = await briefing(store, providers, 1, DAY)
    providers.weather.assert_not_awaited()
    assert "Подъём: 12.10 07:00" in text and "Погода" not in text
    assert store.base_lessons(1) == original and store.profile(1).mirea_id == 1
    assert {n.kind for n in extra_notices([], p, DAY + timedelta(days=1), {})} == {"sleep"}
    p.routine_mode = "full"
    store.save_profile(1, p)
    assert day_items(store, 1, DAY)[0].lesson.title == "МИРЭА"


async def test_queued_reminder_cancelled_or_updated_before_delivery(tmp_path):
    store, _, _, worker = environment(tmp_path)
    lesson = Lesson(id="a", title="A", date=DAY, start="09:00", end="10:00", room="100")
    store.replace(1, [lesson])
    notice = next(n for n in worker.notices(1, store.profile(1), DAY) if n.kind == "lesson")
    meta = {"key": notice.key, "day": DAY.isoformat()}
    store.override(1, DAY, lesson.id, {"room": "200"})
    assert "200" in await worker.prepare_delivery(1, meta, at(8, 50))
    store.override(1, DAY, lesson.id, {"start": "10:00", "end": "11:00"})
    assert await worker.prepare_delivery(1, meta, at(8, 50)) is None
    assert await worker.prepare_delivery(1, meta, at(12)) is None


async def test_sleep_schedule_early_wake_crosses_midnight_without_wrapping(tmp_path):
    store, _, bot, worker = environment(tmp_path)
    p = store.profile(1)
    p.routine_mode = "sleep_only"
    p.travel, p.preparation = 180, 120
    p.sleep.enabled, p.sleep.hours, p.sleep.latency_minutes = True, 8, 0
    p.sleep_schedule = [Lesson(title="Смена", date=DAY, start="02:00", end="03:00")]
    store.save_profile(1, p)
    wake, bed, reminder = sleep_times(day_items(store, 1, DAY), p, DAY)
    assert wake == at(21, day=DAY - timedelta(days=1))
    assert bed == at(13, day=DAY - timedelta(days=1))
    assert (wake - bed).total_seconds() == 8 * 3600
    await worker.tick_user(1, reminder)
    assert bot.send_message.await_count == 1


def test_static_sleep_schedule_has_independent_week_cycle(tmp_path):
    store, _, _, _ = environment(tmp_path)
    p = store.profile(1)
    p.routine_mode = "sleep_only"
    p.semester_start, p.semester_end = date(2025, 9, 1), date(2025, 12, 1)
    p.home_dates = [DAY]
    p.sleep.week_start = DAY
    p.sleep_schedule = [Lesson(title="Смена", weekday=0, parity="odd", start="09:00", end="18:00")]
    store.save_profile(1, p)
    assert day_items(store, 1, DAY)[0].lesson.title == "Смена"
    assert not day_items(store, 1, DAY + timedelta(days=7))
    assert day_items(store, 1, DAY + timedelta(days=14))[0].lesson.title == "Смена"


async def test_queued_briefing_moved_later_can_be_sent_again(tmp_path):
    store, _, bot, worker = environment(tmp_path)
    bot.session.deferred_notices = True
    p = store.profile(1)
    p.fixed_briefing = None
    p.briefing_lead = 0
    p.travel = 30
    store.save_profile(1, p)
    old = Lesson(id="a", title="A", date=DAY, start="09:00", end="10:00")
    store.replace(1, [old])
    await worker.tick_user(1, at(8, 30))
    notice = next(n for n in worker.notices(1, p, DAY) if n.kind == "briefing")
    meta = {"key": notice.key, "day": DAY.isoformat()}
    store.replace(1, [old.model_copy(update={"start": "10:00", "end": "11:00"})])
    assert await worker.prepare_delivery(1, meta, at(8, 30)) is None
    assert store.delivery_ready(1, notice.key, at(9, 30).timestamp())
    await worker.tick_user(1, at(9, 30))
    assert any(call.args[1] == "Сводка готовится." for call in bot.send_message.call_args_list)
