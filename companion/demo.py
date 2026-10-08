"""Offline demo; no token, no network, no changes to the real user database."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import (
    Garment,
    Lesson,
    Profile,
    Rule,
    WeatherHour,
    choose_clothing,
    departure,
    occurrences,
    plan_notices,
)
from .service import agenda


def main():
    day = date(2026, 10, 7)
    p = Profile(
        city="Демонстрационный город",
        travel=50,
        preparation=40,
        buffer=10,
        campus_walk=10,
        notifications=True,
        garments=[
            Garment(name="Пальто", minimum=0, maximum=10),
            Garment(name="Куртка", minimum=-5, maximum=15, waterproof=True, windproof=True),
            Garment(name="Кофта", minimum=19, maximum=25),
        ],
        rules=[
            Rule(name="Дождь до +15", garment="Куртка", maximum=14.9, rain="yes", priority=100),
            Rule(name="Сухо до +10", garment="Пальто", minimum=0, maximum=10, rain="no", priority=50),
            Rule(name="Тепло от +19", garment="Кофта", minimum=19, priority=50),
        ],
    )
    lessons = [
        Lesson(title="Математика", date=day, start="09:00", end="10:30", room="Б-312", campus="В-78"),
        Lesson(title="Программирование", date=day, start="10:40", end="12:10", room="Б-214", campus="В-78"),
    ]
    items = occurrences(lessons, p, day)
    at = datetime(2026, 10, 7, 7, tzinfo=ZoneInfo(p.timezone))
    weather = [
        WeatherHour(at=at, temperature=8, feels=5, rain_probability=80, precipitation=0.5, wind=20),
        WeatherHour(
            at=at + timedelta(hours=5),
            temperature=13,
            feels=11,
            rain_probability=60,
            precipitation=0.1,
            wind=15,
        ),
    ]
    print("ДЕМОНСТРАЦИЯ: расписание, город и погода условные.\n")
    print(f"Выход: {departure(items, p):%H:%M}\n")
    print(choose_clothing(p, weather) + "\n")
    print(agenda(items) + "\n")
    print("Автоматические уведомления:")
    for notice in plan_notices(items, p, day):
        print(f"{notice.at:%H:%M} {notice.kind}: {notice.text or 'Утренняя сводка'}")


if __name__ == "__main__":
    main()
