from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .domain import Place, choose_clothing, departure, occurrences, weather_windows
from .forecast import route_weather, temperature_at
from .journey import journey_times, sleep_times


def day_items(store, uid, day):
    p = store.profile(uid)
    if day in p.home_dates:
        return []
    return occurrences(store.lessons(uid), p, day, store.overrides(uid, day))


def agenda(items, show_id=False):
    if not items:
        return "Занятий на этот день нет."
    lines = []
    for item in items:
        lesson = item.lesson
        prefix = f"[{lesson.id}] " if show_id else ""
        lines.append(
            f"{prefix}{item.start:%H:%M}–{item.end:%H:%M} {lesson.title}\n  Ауд. {lesson.room or 'не указана'} · {lesson.campus or 'корпус не указан'}"
            + (f" · подгр. {lesson.subgroup}" if lesson.subgroup else "")
            + (f"\n  {lesson.note}" if lesson.note else "")
            + (
                f"\n  Допустимое опоздание по твоей заметке: {lesson.allowed_lateness} мин."
                if lesson.allowed_lateness
                else ""
            )
        )
    return "\n".join(lines)


async def weather_report(providers, p, day):
    """Forecast independently of schedules, including first-time onboarding."""
    now = datetime.now(ZoneInfo(p.timezone))
    moment = (
        now
        if day == now.date()
        else datetime.combine(day, datetime.min.time(), ZoneInfo(p.timezone)) + timedelta(hours=9)
    )
    points = p.places
    if not points and p.latitude is not None and p.longitude is not None:
        points = [
            Place(id="city", label=p.city or "Выбранный город", latitude=p.latitude, longitude=p.longitude)
        ]
    if not points:
        return "Погода ещё не настроена. Открой «Точки погоды», добавь район и отметь его на карте. После этого температуру бот получает автоматически — расписание и ИИ для просмотра погоды не нужны."

    async def one(point):
        if point.latitude is None:
            return f"{point.label}: район не отмечен на карте. Открой «Точки погоды» и выбери этот район."
        copy = p.model_copy(update={"latitude": point.latitude, "longitude": point.longitude})
        try:
            hours = await providers.weather(copy, [(moment, moment + timedelta(hours=1))])
            temperature = temperature_at(hours, moment)
            feels = temperature_at(hours, moment, "feels")
            if temperature is None or feels is None:
                return f"{point.label}: прогноз на этот час отсутствует. Попробуй позже."
            hour = next(h for h in hours if h.at == moment.replace(minute=0, second=0, microsecond=0))
            return f"{point.label}: {temperature:+.1f} °C, ощущается {feels:+.1f} °C.\nВероятность осадков {hour.rain_probability:g}%, ветер {hour.wind:g} км/ч."
        except (httpx.HTTPError, ValueError, KeyError):
            return f"{point.label}: погодный сервис временно недоступен. Точка сохранена, повторно отмечать её не нужно."

    lines = await asyncio.gather(*(one(point) for point in points))
    return f"Погода · {moment:%d.%m %H:%M} (прогноз):\n\n" + "\n\n".join(lines)


async def briefing(store, providers, uid, day):
    p = store.profile(uid)
    items = day_items(store, uid, day)
    if not items:
        suffix = "Сегодня выбран режим «Дома»." if day in p.home_dates else "Занятий нет."
        return (
            f"{day:%d.%m.%Y}. {suffix}\n"
            + sleep_report(store, uid, day)
            + "\n\n"
            + await weather_report(providers, p, day)
        )
    leave = departure(items, p)
    prepare = leave - timedelta(minutes=p.preparation)
    lines = [
        f"Твой день · {day:%d.%m.%Y}",
        f"Выход: {leave:%H:%M} ({leave:%d.%m}). Начать сборы: {prepare:%H:%M}.",
        f"Сценарий: {p.scenario}.",
    ]
    if p.sleep.enabled:
        lines.append(sleep_report(store, uid, day))
    times = journey_times(items, p)
    lines.append(
        f"Расчётное возвращение: {times['home_estimate']:%d.%m %H:%M}. Напоминания «после дома» ждут кнопку «Я дома»."
    )
    if p.source == "mirea":
        lines.append(f"Группа: {p.mirea_name}. Проверено: {p.source_checked or 'ещё не обновлялось'}.")
        if (
            not p.source_checked
            or (datetime.now(ZoneInfo(p.timezone)) - datetime.fromisoformat(p.source_checked)).total_seconds()
            > 7200
        ):
            lines.append("Источник давно не обновлялся: проверь возможные изменения.")
    try:
        if p.places:
            lines.append(await route_weather(providers, p, items))
        else:
            hours = await providers.weather(p, weather_windows(items, p))
            lines.append(choose_clothing(p, hours))
    except (httpx.HTTPError, ValueError, KeyError):
        lines.append("Погодный сервис сейчас недоступен. Время выхода и расписание рассчитаны.")
    for a, b in zip(items, items[1:], strict=False):
        gap = int((b.start - a.end).total_seconds() / 60)
        if gap >= 45:
            lines.append(f"Окно {gap} мин.: {a.end:%H:%M}–{b.start:%H:%M}.")
        if gap < 0:
            lines.append("Внимание: занятия пересекаются по времени. Проверь подгруппу или расписание.")
    if any(x.lesson.subgroup for x in items) and not p.subgroup:
        lines.append("Есть занятия разных подгрупп. Выбери свою в настройках.")
    lines.append(agenda(items))
    return "\n\n".join(lines)


def sleep_plan(items, p, day):
    if not p.sleep.enabled:
        return "Напоминания о сне выключены. Настрой их через /config."
    times = sleep_times(items, p, day)
    if not times:
        return "Нет занятий и времени подъёма для свободного дня: сон на эту дату не рассчитан."
    wake, bed, reminder = times
    from .journey import before_quiet

    reminder = before_quiet(reminder, p)
    return f"Подъём: {wake:%d.%m %H:%M}. Лечь: {bed:%d.%m %H:%M}.\nТвоя цель сна {p.sleep.hours:g} ч. + {p.sleep.latency_minutes} мин. на засыпание. Напоминание: {reminder:%d.%m %H:%M}."


def sleep_report(store, uid, day):
    p = store.profile(uid)
    items = day_items(store, uid, day)
    text = sleep_plan(items, p, day)
    times = sleep_times(items, p, day) if p.sleep.enabled else None
    if times:
        wake, bed, _ = times
        # Check previous calendar days without wrapping times into a fictional 24-hour day.
        returns = []
        for n in (1, 2):
            previous = journey_times(day_items(store, uid, day - timedelta(days=n)), p)
            if previous:
                returns.append(previous["home_estimate"])
        if returns and max(returns) > bed:
            home = max(returns)
            available = max(0, (wake - home).total_seconds() / 3600 - p.sleep.latency_minutes / 60)
            text += f"\nКонфликт: расчётное возвращение {home:%d.%m %H:%M} позже отбоя. До подъёма остаётся {available:.1f} ч. сна после возвращения. Проверь дорогу, расписание или цель сна."
    return text


def import_warnings(lessons, p):
    warnings = []
    if any(x.weeks or x.parity != "all" for x in lessons) and not p.semester_start:
        warnings.append("Для чётных/нечётных недель нужно задать понедельник первой учебной недели.")
    if any(x.subgroup for x in lessons) and not p.subgroup:
        warnings.append("Выбери подгруппу в настройках: иначе будут показаны все варианты.")
    if any(x.weekday is not None and x.valid_until is None for x in lessons) and not p.semester_end:
        warnings.append("Повторяющееся расписание без срока окончания. Укажи конец семестра в настройках.")
    if any(not x.room for x in lessons):
        warnings.append("У некоторых занятий не указана аудитория.")
    return warnings
