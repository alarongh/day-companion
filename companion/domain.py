from __future__ import annotations

import hashlib
from datetime import date as Date
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Place(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    label: str = Field(min_length=1, max_length=100)
    city: str = Field(default="", max_length=100)
    district: str = Field(default="", max_length=100)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def check(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Для точки нужны обе координаты или обе пустые")
        return self


class SleepSettings(Model):
    enabled: bool = False
    hours: float = Field(default=8, ge=1, le=16)
    latency_minutes: int = Field(default=15, ge=0, le=120)
    reminder_minutes: int = Field(default=30, ge=0, le=180)
    wake_lead: int = Field(default=0, ge=0, le=180)
    free_day_wake: str | None = None
    week_start: Date | None = None

    @model_validator(mode="after")
    def check(self):
        if self.free_day_wake is not None:
            clock(self.free_day_wake)
        if self.week_start and self.week_start.weekday() != 0:
            raise ValueError("Начало цикла недель сна должно быть понедельником")
        return self


class LessonNote(Model):
    note: str = Field(default="", max_length=1000)
    allowed_lateness: int = Field(default=0, ge=0, le=180)


class Reminder(Model):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    text: str = Field(min_length=1, max_length=1000)
    trigger: Literal[
        "time", "wake", "departure", "arrival", "last_lesson", "home", "bedtime", "condition", "lesson_start"
    ]
    time: str | None = None
    condition: str = Field(default="", max_length=80)
    subject: str = Field(default="", max_length=200)
    offset_minutes: int = Field(default=0, ge=-1440, le=1440)
    date: Date | None = None
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    parity: Literal["all", "odd", "even"] = "all"
    weeks: list[int] = Field(default_factory=list, max_length=60)
    expires_minutes: int = Field(default=180, ge=1, le=1440)
    enabled: bool = True

    @model_validator(mode="after")
    def check(self):
        if self.trigger == "time":
            if self.time is None:
                raise ValueError("Для напоминания по времени нужно время")
            clock(self.time)
        if self.trigger == "condition" and not self.condition:
            raise ValueError("Нужно название подтверждаемого условия")
        if self.trigger in ("home", "condition") and self.offset_minutes < 0:
            raise ValueError("Напоминание после подтверждения не может предшествовать ему")
        if any(x < 0 or x > 6 for x in self.weekdays) or any(x < 1 or x > 60 for x in self.weeks):
            raise ValueError("Некорректный день недели или номер недели")
        return self


def clock(value: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Время нужно записать как 09:00") from exc


class Lesson(Model):
    id: str = Field(default="", max_length=32, pattern=r"^[A-Za-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=200)
    start: str
    end: str
    date: Date | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    weeks: list[int] = Field(default_factory=list)
    parity: Literal["all", "odd", "even"] = "all"
    room: str = Field(default="", max_length=100)
    campus: str = Field(default="", max_length=100)
    subgroup: str = Field(default="", max_length=50)
    valid_from: Date | None = None
    valid_until: Date | None = None
    place_id: str = ""
    note: str = Field(default="", max_length=1000)
    allowed_lateness: int = Field(default=0, ge=0, le=180)

    @model_validator(mode="after")
    def check(self):
        if clock(self.end) <= clock(self.start):
            raise ValueError("Окончание занятия должно быть позже начала в тот же день")
        if (self.date is None) == (self.weekday is None):
            raise ValueError("Укажите либо конкретную дату, либо день недели")
        if any(w < 1 or w > 60 for w in self.weeks):
            raise ValueError("Номер учебной недели должен быть от 1 до 60")
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("Период расписания указан в обратном порядке")
        if not self.id:
            self.id = hashlib.sha256(
                self.model_dump_json(exclude={"id", "place_id", "note", "allowed_lateness"}).encode()
            ).hexdigest()[:16]
        return self


class Garment(Model):
    name: str = Field(min_length=1, max_length=60)
    minimum: float = Field(ge=-60, le=60)
    maximum: float = Field(ge=-60, le=60)
    waterproof: bool = False
    windproof: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def check(self):
        if self.minimum > self.maximum:
            raise ValueError("Минимальная температура больше максимальной")
        return self


class Rule(Model):
    name: str = Field(min_length=1, max_length=100)
    garment: str = Field(min_length=1, max_length=60)
    minimum: float = Field(default=-60, ge=-60, le=60)
    maximum: float = Field(default=60, ge=-60, le=60)
    rain: Literal["any", "yes", "no"] = "any"
    wind_min: float = Field(default=0, ge=0, le=200)
    scenario: str = Field(default="*", max_length=60)
    priority: int = Field(default=0, ge=0, le=1000)

    @model_validator(mode="after")
    def check(self):
        if self.minimum > self.maximum:
            raise ValueError("Нижняя граница больше верхней")
        return self


class Profile(Model):
    routine_mode: Literal["full", "sleep_only"] = "full"
    sleep_schedule: list[Lesson] = Field(default_factory=list, max_length=2000)
    city: str = ""
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    timezone: str = "Europe/Moscow"
    travel: int = Field(default=45, ge=0, le=360)
    preparation: int = Field(default=40, ge=0, le=240)
    buffer: int = Field(default=10, ge=0, le=120)
    campus_walk: int = Field(default=10, ge=0, le=120)
    campus_routes: dict[str, int] = Field(default_factory=dict)
    transfers: dict[str, int] = Field(default_factory=dict)
    default_transfer: int = Field(default=30, ge=0, le=240)
    lesson_lead: int = Field(default=10, ge=1, le=120)
    short_break: int = Field(default=20, ge=1, le=120)
    departure_lead: int = Field(default=10, ge=0, le=120)
    briefing_lead: int = Field(default=10, ge=0, le=180)
    fixed_briefing: str | None = None
    quiet_start: str = "23:00"
    quiet_end: str = "06:00"
    notifications: bool = False
    pause_until: Date | None = None
    home_dates: list[Date] = Field(default_factory=list)
    departed_dates: list[Date] = Field(default_factory=list)
    semester_start: Date | None = None
    semester_end: Date | None = None
    subgroup: str = ""
    scenario: str = "учёба"
    temperature_mode: Literal["actual", "feels"] = "feels"
    rain_threshold: int = Field(default=50, ge=1, le=100)
    comfort_offset: float = Field(default=0, ge=-10, le=10)
    garments: list[Garment] = Field(default_factory=list, max_length=100)
    rules: list[Rule] = Field(default_factory=list, max_length=200)
    source: Literal["manual", "mirea"] = "manual"
    mirea_id: int | None = None
    mirea_name: str = ""
    source_checked: str | None = None
    source_until: Date | None = None
    places: list[Place] = Field(default_factory=list, max_length=12)
    home_place: str = ""
    destination_place: str = ""
    campus_places: dict[str, str] = Field(default_factory=dict)
    outdoor_before: int = Field(default=10, ge=0, le=180)
    outdoor_after: int = Field(default=10, ge=0, le=180)
    return_delay: int = Field(default=0, ge=0, le=360)
    hourly_temperature: Literal["weighted_mean", "coldest"] = "weighted_mean"
    sleep: SleepSettings = Field(default_factory=SleepSettings)
    reminders: list[Reminder] = Field(default_factory=list, max_length=100)
    lesson_notes: dict[str, LessonNote] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check(self):
        ZoneInfo(self.timezone)
        if len({x.id for x in self.sleep_schedule}) != len(self.sleep_schedule):
            raise ValueError("Повторяющиеся идентификаторы статичного расписания сна")
        if any(x.weeks or x.parity != "all" for x in self.sleep_schedule) and not self.sleep.week_start:
            raise ValueError("Для чётности статичного расписания сна нужен sleep.week_start")
        for v in [self.quiet_start, self.quiet_end, self.fixed_briefing]:
            if v is not None:
                clock(v)
        if self.semester_start and self.semester_start.weekday() != 0:
            raise ValueError("Начало первой учебной недели должно быть понедельником")
        if self.semester_end and self.semester_start and self.semester_end < self.semester_start:
            raise ValueError("Конец семестра раньше начала")
        if any(not 0 <= v <= 360 for v in self.campus_routes.values()):
            raise ValueError("Время дороги до корпуса: 0–360 минут")
        if any(not 0 <= v <= 240 for v in self.transfers.values()):
            raise ValueError("Время перехода: 0–240 минут")
        for values, key, label in [
            (self.garments, "name", "вещей"),
            (self.rules, "name", "правил"),
            (self.places, "id", "точек"),
            (self.reminders, "id", "напоминаний"),
        ]:
            if len({getattr(x, key) for x in values}) != len(values):
                raise ValueError(f"Повторяющиеся идентификаторы или названия {label}")
        known = {x.id for x in self.places}
        if any(
            x and x not in known
            for x in [self.home_place, self.destination_place, *self.campus_places.values()]
        ):
            raise ValueError("Дом, место учёбы или корпус ссылается на неизвестную точку")
        if any(r.garment not in {g.name for g in self.garments} for r in self.rules):
            raise ValueError("Правило ссылается на вещь, которой нет в гардеробе")
        return self


class Occurrence(BaseModel):
    lesson: Lesson
    start: datetime
    end: datetime


def occurrences(lessons: list[Lesson], profile: Profile, day: Date, overrides=None) -> list[Occurrence]:
    result = []
    for source in lessons:
        if source.weekday is not None and (
            profile.semester_end
            and day > profile.semester_end
            or profile.semester_start
            and day < profile.semester_start
        ):
            continue
        if source.date is not None and source.date != day:
            continue
        if source.weekday is not None and source.weekday != day.weekday():
            continue
        if source.valid_from and day < source.valid_from or source.valid_until and day > source.valid_until:
            continue
        if source.subgroup and profile.subgroup and source.subgroup != profile.subgroup:
            continue
        if source.weeks or source.parity != "all":
            if not profile.semester_start:
                raise ValueError("Для учебных недель задайте понедельник первой недели в настройках")
            week = (day - profile.semester_start).days // 7 + 1
            if week < 1 or source.weeks and week not in source.weeks:
                continue
            if source.parity == "odd" and week % 2 != 1 or source.parity == "even" and week % 2 != 0:
                continue
        change = (overrides or {}).get(source.id, {})
        if change.get("cancelled"):
            continue
        lesson = Lesson.model_validate(
            {**source.model_dump(), **{k: v for k, v in change.items() if k != "cancelled"}}
        )
        note = profile.lesson_notes.get(source.id, profile.lesson_notes.get(source.title))
        if note:
            lesson = Lesson.model_validate(
                {
                    **lesson.model_dump(),
                    **note.model_dump(),
                    **{k: v for k, v in change.items() if k in ("note", "allowed_lateness")},
                }
            )
        tz = ZoneInfo(profile.timezone)
        result.append(
            Occurrence(
                lesson=lesson,
                start=datetime.combine(day, clock(lesson.start), tz),
                end=datetime.combine(day, clock(lesson.end), tz),
            )
        )
    return sorted(result, key=lambda v: (v.start, v.lesson.id))


def departure(items: list[Occurrence], p: Profile) -> datetime | None:
    if not items:
        return None
    trip = p.campus_routes.get(items[0].lesson.campus, p.travel)
    return items[0].start - timedelta(minutes=trip + p.buffer + p.campus_walk)


def quiet(moment: datetime, p: Profile) -> bool:
    now = moment.astimezone(ZoneInfo(p.timezone)).time().replace(tzinfo=None)
    start, end = clock(p.quiet_start), clock(p.quiet_end)
    if start == end:
        return False
    return start <= now < end if start < end else now >= start or now < end


def after_quiet(moment: datetime, p: Profile) -> datetime:
    if not quiet(moment, p):
        return moment
    local = moment.astimezone(ZoneInfo(p.timezone))
    end = datetime.combine(local.date(), clock(p.quiet_end), local.tzinfo)
    return end if end > local else end + timedelta(days=1)


def weather_windows(items: list[Occurrence], p: Profile) -> list[tuple[datetime, datetime]]:
    if not items:
        return []
    leave = departure(items, p)
    windows = [(leave, items[0].start)]
    for previous, current in zip(items, items[1:], strict=False):
        if previous.lesson.campus != current.lesson.campus:
            minutes = p.transfers.get(f"{previous.lesson.campus}>{current.lesson.campus}", p.default_transfer)
            windows.append((max(previous.end, current.start - timedelta(minutes=minutes)), current.start))
    trip = p.campus_routes.get(items[-1].lesson.campus, p.travel)
    windows.append((items[-1].end, items[-1].end + timedelta(minutes=trip)))
    return windows


class WeatherHour(BaseModel):
    at: datetime
    temperature: float
    feels: float
    rain_probability: float
    precipitation: float
    wind: float


def choose_clothing(p: Profile, hours: list[WeatherHour], effective_temperature: float | None = None) -> str:
    if not hours:
        return "Прогноз на время дороги недоступен. Одежду по погоде сейчас не выбираю."
    temps = [h.feels if p.temperature_mode == "feels" else h.temperature for h in hours]
    low, high = min(temps), max(temps)
    effective = (low if effective_temperature is None else effective_temperature) - p.comfort_offset
    rain = any(h.rain_probability >= p.rain_threshold or h.precipitation >= 0.1 for h in hours)
    wind = max(h.wind for h in hours)
    header = f"На улице в часы дороги: {min(h.temperature for h in hours):+.0f}…{max(h.temperature for h in hours):+.0f}°, ощущается {min(h.feels for h in hours):+.0f}…{max(h.feels for h in hours):+.0f}°."
    available = {g.name: g for g in p.garments if g.enabled}
    if not available:
        return header + "\nДобавь свою верхнюю одежду через меню «Одежда»."
    candidates = [
        r
        for r in p.rules
        if r.garment in available
        and r.minimum <= effective <= r.maximum
        and (r.rain == "any" or (r.rain == "yes") == rain)
        and wind >= r.wind_min
        and r.scenario in ("*", p.scenario)
    ]
    if candidates:
        selected = sorted(candidates, key=lambda r: (-r.priority, r.maximum - r.minimum, r.name))[0]
        garment = available[selected.garment]
        reason = f"Твоё правило «{selected.name}» (приоритет {selected.priority})."
    else:

        def penalty(g):
            distance = max(g.minimum - effective, 0, effective - g.maximum)
            return (
                distance
                + (10 if rain and not g.waterproof else 0)
                + (3 if wind >= 25 and not g.windproof else 0),
                g.name,
            )

        garment = min(available.values(), key=penalty)
        reason = "Ближайший вариант по заданному тобой диапазону комфорта, дождю и ветру."
    text = f"{header}\nНадень: {garment.name}. {reason}"
    if rain:
        text += "\nВозможны осадки."
        if not garment.waterproof:
            text += " Выбранная вещь не отмечена как непромокаемая — возьми зонт."
    if not garment.minimum <= effective <= garment.maximum:
        text += "\nТемпература вне комфортного диапазона этой вещи; подходящего варианта в гардеробе нет."
    if high - low >= 8 or high > garment.maximum:
        text += "\nЗаметный перепад температуры: днём верхний слой может понадобиться снять."
    return text


class Notice(BaseModel):
    key: str
    kind: str
    at: datetime
    expires: datetime
    text: str = ""
    day: Date
    catch_up_seconds: int = 180


def plan_notices(items: list[Occurrence], p: Profile, day: Date) -> list[Notice]:
    if p.routine_mode == "sleep_only":
        return []
    if not p.notifications or not items or p.pause_until and day <= p.pause_until or day in p.home_dates:
        return []
    leave = departure(items, p)
    notices = []

    def add(kind, at, expires, text="", identity=""):
        at = after_quiet(at, p)
        if at < expires:
            timing = "" if kind == "briefing" else at.isoformat()
            notices.append(
                Notice(
                    key=f"{day}:{kind}:{identity}:{timing}",
                    kind=kind,
                    at=at,
                    expires=expires,
                    text=text,
                    day=day,
                )
            )

    brief = (
        datetime.combine(day, clock(p.fixed_briefing), ZoneInfo(p.timezone))
        if p.fixed_briefing
        else leave
        - timedelta(minutes=p.preparation + (p.sleep.wake_lead if p.sleep.enabled else p.briefing_lead))
    )
    add("briefing", brief, items[0].start)
    if day not in p.departed_dates:
        if p.departure_lead:
            add(
                "prepare",
                leave - timedelta(minutes=p.departure_lead),
                leave,
                f"До выхода {p.departure_lead} мин. Выход в {leave:%H:%M}.",
            )
        add(
            "depart",
            leave,
            items[0].start,
            f"Пора выходить. Первая пара в {items[0].start:%H:%M}: {items[0].lesson.title}, ауд. {items[0].lesson.room or 'не указана'} ({items[0].lesson.campus or 'корпус не указан'}).",
        )
    for index, item in enumerate(items):
        at = item.start - timedelta(minutes=p.lesson_lead)
        warning = ""
        if index:
            previous = items[index - 1]
            gap = (item.start - previous.end).total_seconds() / 60
            if gap <= p.short_break:
                at = previous.end
            else:
                at = max(at, previous.end)
            if previous.lesson.campus and item.lesson.campus and previous.lesson.campus != item.lesson.campus:
                transfer = p.transfers.get(
                    f"{previous.lesson.campus}>{item.lesson.campus}", p.default_transfer
                )
                at = max(previous.end, item.start - timedelta(minutes=max(p.lesson_lead, transfer)))
                warning = f"\nДругой корпус: на переход заложено {transfer} мин."
                if gap < transfer:
                    warning += " Перерыва для этого недостаточно."
        add(
            "lesson",
            at,
            item.start,
            f"Следующая пара в {item.start:%H:%M}: {item.lesson.title}.\nАудитория: {item.lesson.room or 'не указана'}. Корпус: {item.lesson.campus or 'не указан'}.{warning}"
            + (f"\nЗаметка: {item.lesson.note}" if item.lesson.note else "")
            + (
                f"\nПо твоей настройке допустимое опоздание: {item.lesson.allowed_lateness} мин."
                if item.lesson.allowed_lateness
                else ""
            ),
            item.lesson.id,
        )
    return sorted(notices, key=lambda n: n.at)
