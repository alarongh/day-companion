from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from companion.domain import (
    Garment,
    Lesson,
    Profile,
    Rule,
    WeatherHour,
    choose_clothing,
    departure,
    occurrences,
    plan_notices,
    quiet,
)

DAY = date(2026, 10, 7)
TZ = ZoneInfo("Europe/Moscow")


def lesson(**kwargs):
    return Lesson(
        **{
            "title": "Математика",
            "date": DAY,
            "start": "09:00",
            "end": "10:30",
            "room": "312",
            "campus": "В-78",
            **kwargs,
        }
    )


def weather(**kwargs):
    return WeatherHour(
        **{
            "at": datetime(2026, 10, 7, 8, tzinfo=TZ),
            "temperature": 8,
            "feels": 5,
            "rain_probability": 80,
            "precipitation": 0.5,
            "wind": 20,
            **kwargs,
        }
    )


def test_departure_and_short_break():
    p = Profile(travel=50, buffer=10, campus_walk=10, preparation=40, notifications=True)
    items = occurrences([lesson(), lesson(title="Программирование", start="10:40", end="12:10")], p, DAY)
    assert departure(items, p).strftime("%H:%M") == "07:50"
    notices = plan_notices(items, p, DAY)
    assert next(n for n in notices if n.kind == "briefing").at.strftime("%H:%M") == "07:00"
    assert [n.at.strftime("%H:%M") for n in notices if n.kind == "lesson"] == ["08:50", "10:30"]


def test_campus_routes_long_break_and_transfer():
    p = Profile(notifications=True, campus_routes={"В-78": 60}, transfers={"В-78>С-20": 40})
    items = occurrences([lesson(), lesson(title="Вторая", start="11:30", end="13:00", campus="С-20")], p, DAY)
    assert departure(items, p).strftime("%H:%M") == "07:40"
    notice = [n for n in plan_notices(items, p, DAY) if n.kind == "lesson"][-1]
    assert notice.at.strftime("%H:%M") == "10:50"
    assert "40 мин." in notice.text


def test_insufficient_transfer_warns_without_interrupting_previous_lesson():
    p = Profile(notifications=True, default_transfer=40)
    items = occurrences([lesson(), lesson(title="Вторая", start="10:40", end="12:10", campus="С-20")], p, DAY)
    n = [n for n in plan_notices(items, p, DAY) if n.kind == "lesson"][-1]
    assert n.at == items[0].end
    assert "недостаточно" in n.text


def test_cancel_first_recalculates_departure():
    p = Profile()
    a, b = lesson(), lesson(title="Вторая", start="10:40", end="12:10")
    items = occurrences([a, b], p, DAY, {a.id: {"cancelled": True}})
    assert len(items) == 1
    assert departure(items, p).strftime("%H:%M") == "09:35"
    assert len(occurrences([a, b], p, DAY + timedelta(days=1))) == 0


def test_parity_weeks_and_subgroup():
    p = Profile(semester_start=date(2026, 8, 31), subgroup="1")
    a = lesson(date=None, weekday=2, parity="even", subgroup="1", weeks=[6])
    assert len(occurrences([a], p, DAY)) == 1
    assert not occurrences([a], p, DAY + timedelta(days=7))
    p.subgroup = "2"
    assert not occurrences([a], p, DAY)
    with pytest.raises(ValueError, match="первой недели"):
        occurrences([a], Profile(), DAY)


def test_quiet_home_pause_and_departed():
    p = Profile(notifications=True)
    assert quiet(datetime(2026, 10, 7, 2, tzinfo=TZ), p)
    assert not quiet(datetime(2026, 10, 7, 12, tzinfo=TZ), p)
    items = occurrences([lesson()], p, DAY)
    p.home_dates = [DAY]
    assert not plan_notices(items, p, DAY)
    p.home_dates = []
    p.pause_until = DAY
    assert not plan_notices(items, p, DAY)
    p.pause_until = None
    p.departed_dates = [DAY]
    assert all(n.kind not in ("depart", "prepare") for n in plan_notices(items, p, DAY))


def test_clothing_priority_context_and_missing_weather():
    p = Profile(
        garments=[
            Garment(name="Пальто", minimum=0, maximum=10),
            Garment(name="Куртка", minimum=0, maximum=15, waterproof=True),
        ],
        rules=[
            Rule(name="Сухо", garment="Пальто", maximum=10, priority=50),
            Rule(name="Дождь", garment="Куртка", maximum=15, rain="yes", priority=100, scenario="учёба"),
        ],
    )
    assert "Надень: Куртка" in choose_clothing(p, [weather()])
    p.scenario = "работа"
    assert "Надень: Пальто" in choose_clothing(p, [weather()])
    assert "непромокаемая" in choose_clothing(p, [weather()])
    assert "недоступен" in choose_clothing(p, [])
    p.garments = []
    assert "Добавь" in choose_clothing(p, [weather()])


def test_feedback_offset_makes_recommendation_warmer_and_disabled_items_ignored():
    p = Profile(
        comfort_offset=3,
        garments=[
            Garment(name="Лёгкая", minimum=10, maximum=20),
            Garment(name="Тёплая", minimum=0, maximum=9),
        ],
        temperature_mode="actual",
    )
    dry = weather(temperature=12, feels=12, precipitation=0, rain_probability=0, wind=0)
    assert "Надень: Тёплая" in choose_clothing(p, [dry])
    p.garments[1].enabled = False
    assert "Надень: Лёгкая" in choose_clothing(p, [dry])


def test_invalid_ranges_and_time():
    with pytest.raises(ValueError):
        lesson(start="11:00", end="10:30")
    with pytest.raises(ValueError):
        Profile(travel=-1)
    with pytest.raises(ValueError):
        Garment(name="Пальто", minimum=15, maximum=0)
    with pytest.raises(ValueError):
        Rule(name="Ошибка", garment="Пальто", minimum=20, maximum=0)


def test_semester_end_does_not_hide_personal_dated_events():
    p = Profile(semester_end=date(2026, 10, 1))
    assert len(occurrences([lesson()], p, DAY)) == 1
    assert not occurrences([lesson(date=None, weekday=2)], p, DAY)
