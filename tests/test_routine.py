import asyncio
from collections import defaultdict
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from companion.app import Application
from companion.configuration import Configuration, decode_config, prepare, prompt, schema, snapshot
from companion.domain import Lesson, Place, Profile, Reminder, SleepSettings, WeatherHour, occurrences
from companion.forecast import route_weather
from companion.journey import extra_notices, hourly_exposure, journey_segments, journey_times, sleep_times
from companion.service import sleep_report
from companion.store import Store
from companion.worker import Worker

TZ = ZoneInfo("Europe/Moscow")
DAY = date(2026, 10, 8)


def at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time(), TZ) + timedelta(hours=hour, minutes=minute)


def config(s, **kwargs):
    return Configuration.model_validate(
        {
            "format": "day-companion/v2",
            "mode": "patch",
            "base_revision": snapshot(s, 1)["base_revision"],
            **kwargs,
        }
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite3")
    s.ensure(1)
    return s


def test_partial_changes_preserve_nested_settings_and_source_refresh(store):
    p = store.profile(1)
    p.sleep = SleepSettings(enabled=True, hours=9, latency_minutes=20)
    p.campus_routes = {"A": 90, "B": 60}
    p.source = "mirea"
    p.mirea_id = 708
    store.save_profile(1, p)
    c = config(store, profile={"sleep": {"hours": 8.5}, "campus_routes": {"A": 100, "B": None}})
    # Background runtime updates must not invalidate external AI edits.
    p.source_checked = at(DAY, 8).isoformat()
    p.departed_dates = [DAY]
    store.save_profile(1, p)
    store.replace(1, [Lesson(title="Source", date=DAY, start="09:00", end="10:30")])
    result, _ = prepare(store, 1, c)
    store.apply_config(1, *result)
    p = store.profile(1)
    assert p.sleep.hours == 8.5 and p.sleep.enabled and p.sleep.latency_minutes == 20
    assert p.campus_routes == {"A": 100}
    assert p.departed_dates == [DAY] and p.source_checked
    assert len(store.lessons(1)) == 1 and p.source == "mirea"


def test_config_rejects_stale_unknown_and_invalid_references(store):
    c = config(store, profile={"travel": 90})
    p = store.profile(1)
    p.preparation = 10
    store.save_profile(1, p)
    with pytest.raises(ValueError, match="Настройки изменились"):
        prepare(store, 1, c)
    with pytest.raises(ValueError):
        config(store, profile={"trvael": 90})
    with pytest.raises(ValueError):
        prepare(store, 1, config(store, profile={"sleep": {"hours": None}}))
    with pytest.raises(ValueError):
        prepare(store, 1, config(store, profile={"travel": None}))
    with pytest.raises(ValueError):
        prepare(store, 1, config(store, rules={"upsert": [{"name": "x", "garment": "missing"}]}))
    assert store.profile(1).preparation == 10


def test_schedule_parity_notes_and_partial_override(store):
    lesson = Lesson(id="math", title="Math", weekday=3, start="09:00", end="10:30", room="312", parity="even")
    c = config(
        store,
        profile={
            "semester_start": "2026-08-31",
            "lesson_notes": {"math": {"note": "Взять тетрадь", "allowed_lateness": 5}},
        },
        schedule={"lessons": [lesson.model_dump(mode="json")]},
    )
    result, _ = prepare(store, 1, c)
    store.apply_config(1, *result)
    # Oct 8 is semester week 6.
    items = occurrences(store.lessons(1), store.profile(1), DAY)
    assert len(items) == 1 and items[0].lesson.note == "Взять тетрадь"
    c = config(store, overrides=[{"day": str(DAY), "lesson_id": "math", "change": {"room": "200"}}])
    store.apply_config(1, *prepare(store, 1, c)[0])
    c = config(
        store, overrides=[{"day": str(DAY), "lesson_id": "math", "change": {"note": "Принести работу"}}]
    )
    store.apply_config(1, *prepare(store, 1, c)[0])
    assert store.overrides(1, DAY)["math"] == {"room": "200", "note": "Принести работу"}


def test_ai_prompt_schema_and_fenced_response(store):
    text = prompt(store, 1, True)
    assert snapshot(store, 1)["base_revision"] in text and "Текущ" in text
    assert schema()["properties"]["places"]["properties"]["upsert"]["items"]["additionalProperties"] is False
    c = config(store, profile={"sleep": {"enabled": True}})
    assert decode_config("```json\n" + c.model_dump_json() + "\n```") == c
    assert decode_config("Hello") is None


def test_sleep_midnight_and_previous_return_conflict(store):
    p = store.profile(1)
    p.notifications = True
    p.travel = 120
    p.campus_walk = 0
    p.buffer = 0
    p.preparation = 60
    p.sleep = SleepSettings(enabled=True, hours=8, latency_minutes=15)
    p.quiet_start = p.quiet_end = "00:00"
    store.save_profile(1, p)
    previous = Lesson(title="Evening", date=DAY, start="22:00", end="23:30")
    morning = Lesson(title="Morning", date=DAY + timedelta(days=1), start="09:00", end="10:00")
    store.replace(1, [previous, morning])
    today = occurrences([previous], p, DAY)
    times = journey_times(today, p)
    assert times["home_estimate"] == at(DAY + timedelta(days=1), 1, 30)
    next_items = occurrences([morning], p, DAY + timedelta(days=1))
    wake, bed, remind = sleep_times(next_items, p, DAY + timedelta(days=1))
    assert wake == at(DAY + timedelta(days=1), 6)
    assert bed == at(DAY, 21, 45) and remind == at(DAY, 21, 15)
    assert (wake - bed).total_seconds() == 8.25 * 3600
    assert "Конфликт" in sleep_report(store, 1, DAY + timedelta(days=1))
    notices = extra_notices(next_items, p, DAY + timedelta(days=1), {})
    assert next(x for x in notices if x.kind == "sleep").at == remind


def test_hourly_outdoor_mean_crosses_midnight_without_averaging_whole_day():
    a = at(DAY, 23)
    forecasts = {
        key: [
            WeatherHour(
                at=a + timedelta(hours=n),
                temperature=temp,
                feels=temp,
                rain_probability=0,
                precipitation=0,
                wind=0,
            )
            for n, temp in enumerate(temps)
        ]
        for key, temps in {"home": [0, 10, 20], "campus": [20, 20, 20]}.items()
    }
    segments = [
        {
            "point": "campus",
            "start": a + timedelta(minutes=30),
            "end": a + timedelta(minutes=45),
            "outdoor": True,
        },
        {
            "point": "home",
            "start": a + timedelta(minutes=45),
            "end": a + timedelta(hours=1, minutes=15),
            "outdoor": True,
        },
    ]
    rows, complete = hourly_exposure(segments, forecasts)
    assert complete and len(rows) == 2
    assert rows[0].temperature == pytest.approx((20 + 8.75) / 2)
    assert rows[1].at.date() == DAY + timedelta(days=1)
    assert rows[1].temperature == pytest.approx(11.25)
    assert (rows[1].at - rows[0].at).total_seconds() == 3600
    forecasts["home"].pop()
    assert not hourly_exposure(segments, forecasts)[1]


async def test_route_missing_district_does_not_silently_use_city():
    p = Profile(
        places=[Place(id="home", label="Дом"), Place(id="uni", label="Вуз")],
        home_place="home",
        destination_place="uni",
        latitude=55,
        longitude=37,
    )
    items = occurrences([Lesson(title="A", date=DAY, start="09:00", end="10:00")], p, DAY)
    provider = AsyncMock()
    text = await route_weather(provider, p, items)
    provider.weather.assert_not_awaited()
    assert "не хватает" in text


async def test_home_after_midnight_manual_only_and_dedup_after_restart(store):
    p = store.profile(1)
    p.notifications = True
    p.travel = 120
    p.buffer = p.campus_walk = 0
    p.quiet_start = p.quiet_end = "00:00"
    p.reminders = [Reminder(id="tea", text="Чай", trigger="home", weekdays=[3], offset_minutes=20)]
    store.save_profile(1, p)
    store.replace(1, [Lesson(title="Late", date=DAY, start="22:00", end="23:30")])
    bot = AsyncMock()
    worker = Worker(store, AsyncMock(), bot, defaultdict(asyncio.Lock), lambda _: None)
    now = at(DAY + timedelta(days=1), 1, 30)
    await worker.tick_user(1, now)
    bot.send_message.assert_not_awaited()  # Estimated arrival must never trigger home reminders.
    app = Application(store, AsyncMock(), defaultdict(asyncio.Lock))
    app.local_now = lambda _: now
    app.uid = lambda _: 1
    message = AsyncMock()
    await app.arrived(message)
    assert "home" in store.events(1, DAY)
    assert not store.events(1, now.date())
    # A repeated press does not postpone the reminder.
    store.record_event(1, DAY, "home", now + timedelta(minutes=5))
    assert datetime.fromisoformat(store.events(1, DAY)["home"]) == now
    restarted = Worker(Store(store.path), AsyncMock(), bot, defaultdict(asyncio.Lock), lambda _: None)
    await restarted.tick_user(1, now + timedelta(minutes=20))
    bot.send_message.assert_awaited_once()
    assert bot.send_message.call_args.args[1] == "Чай"
    await restarted.tick_user(1, now + timedelta(minutes=21))
    bot.send_message.assert_awaited_once()


def test_events_are_private_and_deleted(store):
    store.ensure(2)
    store.record_event(1, DAY, "home", at(DAY, 20))
    assert not store.events(2, DAY)
    store.delete(1)
    assert not store.events(1, DAY)


def test_no_modulo_24_in_long_journey():
    p = Profile(travel=360, campus_walk=90, return_delay=360)
    items = occurrences([Lesson(title="A", date=DAY, start="23:00", end="23:55")], p, DAY)
    times = journey_times(items, p)
    assert times["home_estimate"] == at(DAY + timedelta(days=1), 13, 25)
    assert times["home_estimate"] > times["return_start"] > items[0].end
    segments = journey_segments(items, p)
    assert all(s["end"] > s["start"] for s in segments)


def test_short_trip_never_counts_two_locations_at_the_same_time():
    p = Profile(
        travel=5,
        places=[Place(id="h", label="H"), Place(id="u", label="U")],
        home_place="h",
        destination_place="u",
    )
    items = occurrences([Lesson(title="A", date=DAY, start="09:00", end="10:00")], p, DAY)
    rows = [s for s in journey_segments(items, p) if s["outdoor"]]
    for a in rows:
        for b in rows:
            if a["point"] != b["point"]:
                assert min(a["end"], b["end"]) <= max(a["start"], b["start"])


def test_atomic_commit_rolls_back_profile_and_schedule_on_failure(store):
    before = store.profile(1)
    changed = before.model_copy(update={"travel": 100})
    with pytest.raises(TypeError):
        store.apply_config(
            1,
            changed,
            [Lesson(title="A", date=DAY, start="09:00", end="10:00")],
            [],
            [{"day": str(DAY), "lesson_id": "a", "change": object()}],
        )
    assert store.profile(1) == before
    assert not store.lessons(1)


async def test_complete_route_uses_both_forecasts_through_next_day():
    p = Profile(
        travel=120,
        buffer=0,
        campus_walk=0,
        places=[
            Place(id="h", label="Дом", latitude=55, longitude=37),
            Place(id="u", label="Вуз", latitude=56, longitude=38),
        ],
        home_place="h",
        destination_place="u",
    )
    items = occurrences([Lesson(title="A", date=DAY, start="22:00", end="23:30")], p, DAY)

    async def weather(profile, windows):
        return [
            WeatherHour(
                at=at(DAY, h),
                temperature=5 if profile.latitude == 55 else 15,
                feels=5 if profile.latitude == 55 else 15,
                rain_probability=0,
                precipitation=0,
                wind=0,
            )
            for h in range(18, 28)
        ]

    providers = AsyncMock()
    providers.weather.side_effect = weather
    text = await route_weather(providers, p, items)
    assert providers.weather.await_count == 2
    assert "09.10 01:30" in text and "Дом: +5.0" in text and "Вуз: +15.0" in text
    assert "не хватает" not in text


def test_condition_reminder_crosses_midnight_and_sleep_respects_quiet():
    p = Profile(
        notifications=True,
        sleep=SleepSettings(
            enabled=True, free_day_wake="08:00", hours=8, latency_minutes=15, reminder_minutes=30
        ),
        reminders=[
            Reminder(id="done", text="Дело", trigger="condition", condition="Тренировка", offset_minutes=30)
        ],
    )
    events = {"condition:Тренировка": at(DAY, 23, 50).isoformat()}
    notices = extra_notices([], p, DAY, events)
    # An expired condition reminder must be omitted, not delivered next morning.
    assert not [n for n in notices if n.kind == "reminder"]
    assert next(n for n in notices if n.kind == "sleep").at == at(DAY - timedelta(days=1), 22, 59)
    p.quiet_start = p.quiet_end = "00:00"
    reminder = next(n for n in extra_notices([], p, DAY, events) if n.kind == "reminder")
    assert reminder.at == at(DAY + timedelta(days=1), 0, 20)
