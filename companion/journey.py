from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .domain import Notice, Occurrence, Profile, WeatherHour, after_quiet, clock, departure, quiet


def wake_time(items: list[Occurrence], p: Profile, day: date):
    if items:
        return departure(items, p) - timedelta(minutes=p.preparation + p.sleep.wake_lead)
    if p.sleep.free_day_wake:
        return datetime.combine(day, clock(p.sleep.free_day_wake), ZoneInfo(p.timezone))
    return None


def sleep_times(items, p, day):
    wake = wake_time(items, p, day)
    if wake is None:
        return None
    bed = wake - timedelta(minutes=p.sleep.hours * 60 + p.sleep.latency_minutes)
    return wake, bed, bed - timedelta(minutes=p.sleep.reminder_minutes)


def journey_times(items, p):
    if not items:
        return None
    first = items[0]
    last = max(items, key=lambda x: x.end)
    leave = departure(items, p)
    arrive = first.start - timedelta(minutes=p.buffer + p.campus_walk)
    return_start = last.end + timedelta(minutes=p.return_delay + p.campus_walk)
    home = return_start + timedelta(minutes=p.campus_routes.get(last.lesson.campus, p.travel))
    return {
        "departure": leave,
        "arrival": arrive,
        "last_lesson": last.end,
        "return_start": return_start,
        "home_estimate": home,
    }


def location_id(item, p):
    return item.lesson.place_id or p.campus_places.get(
        item.lesson.campus, p.destination_place or p.home_place
    )


def journey_segments(items, p):
    times = journey_times(items, p)
    if times is None:
        return []
    first, last = items[0], max(items, key=lambda x: x.end)
    rows = []

    def add(label, point, start, end, outdoor):
        if end > start:
            rows.append({"label": label, "point": point, "start": start, "end": end, "outdoor": outdoor})

    home = p.home_place or p.destination_place
    dest = location_id(first, p)
    home_exit_end = min(times["arrival"], times["departure"] + timedelta(minutes=p.outdoor_before))
    add(
        "Выезд из дома",
        home,
        times["departure"],
        home_exit_end,
        True,
    )
    add(
        "Приезд и путь к аудитории",
        dest,
        max(home_exit_end, times["arrival"] - timedelta(minutes=p.outdoor_after)),
        first.start - timedelta(minutes=p.buffer),
        True,
    )
    for index, item in enumerate(items):
        add("Во время занятий", location_id(item, p), item.start, item.end, False)
        if index:
            previous = items[index - 1]
            if location_id(previous, p) != location_id(item, p):
                exit_end = min(item.start, previous.end + timedelta(minutes=p.outdoor_after))
                add(
                    "Выход из корпуса",
                    location_id(previous, p),
                    previous.end,
                    exit_end,
                    True,
                )
                add(
                    "Переход к следующему корпусу",
                    location_id(item, p),
                    max(exit_end, item.start - timedelta(minutes=p.outdoor_after)),
                    item.start,
                    True,
                )
    home_return_start = max(
        times["return_start"], times["home_estimate"] - timedelta(minutes=p.outdoor_before)
    )
    add(
        "Выход после занятий",
        location_id(last, p),
        last.end + timedelta(minutes=p.return_delay),
        min(home_return_start, times["return_start"] + timedelta(minutes=p.outdoor_after)),
        True,
    )
    add(
        "Возвращение домой",
        home,
        home_return_start,
        times["home_estimate"],
        True,
    )
    return rows


def hourly_exposure(segments, forecasts):
    """Average only outdoor minutes in each hour, at the point occupied in that interval.

    Linear interpolation between forecast hours is integrated over partial hours.
    Same-point overlapping intervals are merged, so minutes are never counted twice.
    """
    intervals = defaultdict(list)
    for s in segments:
        if s["outdoor"]:
            intervals[s["point"]].append((s["start"], s["end"]))
    totals = defaultdict(lambda: dict(minutes=0, temperature=0, feels=0, rain=0, precipitation=0, wind=0))
    complete = True
    for point, raw in intervals.items():
        merged = []
        for begin, end in sorted(raw):
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((begin, end))
        series = {h.at: h for h in forecasts.get(point, [])}
        for begin, end in merged:
            cursor = begin
            while cursor < end:
                hour = cursor.replace(minute=0, second=0, microsecond=0)
                boundary = hour + timedelta(hours=1)
                stop = min(end, boundary)
                a, b = series.get(hour), series.get(boundary)
                if a is None or b is None:
                    complete = False
                    cursor = stop
                    continue
                start_fraction = (cursor - hour).total_seconds() / 3600
                end_fraction = (stop - hour).total_seconds() / 3600
                midpoint = (start_fraction + end_fraction) / 2
                minutes = (stop - cursor).total_seconds() / 60
                bucket = totals[hour]
                bucket["minutes"] += minutes
                for field in ("temperature", "feels"):
                    value = getattr(a, field) + (getattr(b, field) - getattr(a, field)) * midpoint
                    bucket[field] += value * minutes
                bucket["rain"] = max(bucket["rain"], a.rain_probability, b.rain_probability)
                bucket["precipitation"] = max(bucket["precipitation"], a.precipitation, b.precipitation)
                bucket["wind"] = max(bucket["wind"], a.wind, b.wind)
                cursor = stop
    rows = []
    for at, v in sorted(totals.items()):
        rows.append(
            WeatherHour(
                at=at,
                temperature=v["temperature"] / v["minutes"],
                feels=v["feels"] / v["minutes"],
                rain_probability=v["rain"],
                precipitation=v["precipitation"],
                wind=v["wind"],
            )
        )
    return rows, complete


def before_quiet(moment, p):
    if not quiet(moment, p):
        return moment
    local = moment.astimezone(ZoneInfo(p.timezone))
    start = datetime.combine(local.date(), clock(p.quiet_start), local.tzinfo)
    if start > local:
        start -= timedelta(days=1)
    return start - timedelta(minutes=1)


def extra_notices(items, p, day, events):
    if not p.notifications or p.pause_until and day <= p.pause_until:
        return []
    rows = []
    times = journey_times(items, p) or {}
    sleep = sleep_times(items, p, day)
    if p.sleep.enabled and sleep:
        wake, bed, remind = sleep
        remind = before_quiet(remind, p)
        reason = (
            f"Первая пара {items[0].start:%H:%M}; дорога {p.campus_routes.get(items[0].lesson.campus, p.travel)} мин., до аудитории {p.campus_walk}, запас {p.buffer}, сборы {p.preparation}, время после подъёма {p.sleep.wake_lead}."
            if items
            else "Время подъёма на день без занятий задано тобой."
        )
        rows.append(
            Notice(
                key=f"{day}:sleep:{bed.isoformat()}",
                kind="sleep",
                at=remind,
                expires=bed + timedelta(minutes=1),
                day=day,
                catch_up_seconds=1800,
                text=f"Пора готовиться ко сну.\nЛечь: {bed:%H:%M} ({bed:%d.%m}). Подъём: {wake:%H:%M} ({wake:%d.%m}).\nПо твоей настройке: {p.sleep.hours:g} ч. на сон и {p.sleep.latency_minutes} мин. на засыпание.\n{reason}",
            )
        )
        if p.routine_mode == "full" and not items and day not in p.home_dates:
            at = after_quiet(wake, p)
            if at < wake + timedelta(hours=1):
                rows.append(
                    Notice(
                        key=f"{day}:briefing::",
                        kind="briefing",
                        at=at,
                        expires=wake + timedelta(hours=1),
                        day=day,
                        catch_up_seconds=3600,
                    )
                )
    if p.routine_mode == "sleep_only":
        return rows
    for r in p.reminders:
        if not r.enabled or r.date and r.date != day or r.weekdays and day.weekday() not in r.weekdays:
            continue
        if r.weeks or r.parity != "all":
            if not p.semester_start:
                continue
            week = (day - p.semester_start).days // 7 + 1
            if (
                week < 1
                or r.weeks
                and week not in r.weeks
                or r.parity == "odd"
                and week % 2 != 1
                or r.parity == "even"
                and week % 2 != 0
            ):
                continue
        bases = []
        if r.trigger == "time":
            bases = [datetime.combine(day, clock(r.time), ZoneInfo(p.timezone))]
        elif r.trigger == "home":
            if "home" in events:
                bases = [datetime.fromisoformat(events["home"])]
        elif r.trigger == "condition":
            if "condition:" + r.condition in events:
                bases = [datetime.fromisoformat(events["condition:" + r.condition])]
        elif r.trigger == "wake" and sleep:
            bases = [sleep[0]]
        elif r.trigger == "bedtime" and sleep:
            bases = [sleep[1]]
        elif r.trigger == "lesson_start":
            bases = [x.start for x in items if not r.subject or r.subject in (x.lesson.title, x.lesson.id)]
        elif r.trigger in times:
            bases = [times[r.trigger]]
        for base in bases:
            original = base + timedelta(minutes=r.offset_minutes)
            at = after_quiet(original, p)
            expires = original + timedelta(minutes=r.expires_minutes)
            if at < expires:
                rows.append(
                    Notice(
                        key=f"{day}:reminder:{r.id}:{base.isoformat()}",
                        kind="reminder",
                        at=at,
                        expires=expires,
                        day=day,
                        text=r.text,
                        catch_up_seconds=r.expires_minutes * 60,
                    )
                )
    return sorted(rows, key=lambda x: x.at)
