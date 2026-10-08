from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx

from .domain import choose_clothing
from .journey import hourly_exposure, journey_segments, journey_times, location_id


def temperature_at(hours, moment, field="temperature"):
    hour = moment.replace(minute=0, second=0, microsecond=0)
    values = {x.at: x for x in hours}
    a, b = values.get(hour), values.get(hour + timedelta(hours=1))
    if a is None or b is None:
        return None
    fraction = (moment - hour).total_seconds() / 3600
    return getattr(a, field) + (getattr(b, field) - getattr(a, field)) * fraction


async def route_weather(providers, p, items):
    segments = journey_segments(items, p)
    times = journey_times(items, p)
    if not times:
        return "Погода маршрута: на этот день нет поездок по расписанию."
    points = {x.id: x for x in p.places}
    required = {s["point"] for s in segments}
    required.update(
        [
            p.home_place or p.destination_place,
            location_id(items[0], p),
            location_id(max(items, key=lambda x: x.end), p),
        ]
    )
    forecasts = {}
    missing = []
    begin = min(s["start"] for s in segments) if segments else times["departure"]
    end = max(s["end"] for s in segments) if segments else times["home_estimate"]

    async def fetch(key):
        point = points.get(key)
        if point is None or point.latitude is None:
            return key, None
        copy = p.model_copy(update={"latitude": point.latitude, "longitude": point.longitude})
        try:
            return key, await providers.weather(
                copy, [(begin.replace(minute=0, second=0, microsecond=0), end + timedelta(hours=1))]
            )
        except (httpx.HTTPError, ValueError, KeyError):
            return key, None

    for key, hours in await asyncio.gather(*(fetch(k) for k in required)):
        if hours:
            forecasts[key] = hours
        else:
            missing.append(points[key].label if key in points else "точка маршрута")
    lines = ["Погода по маршруту:"]
    for label, key, at in [
        ("Выезд", p.home_place or p.destination_place, times["departure"]),
        ("Приезд", location_id(items[0], p), times["arrival"]),
        ("Возвращение (расчёт)", p.home_place or p.destination_place, times["home_estimate"]),
    ]:
        value = temperature_at(forecasts.get(key, []), at)
        feels = temperature_at(forecasts.get(key, []), at, "feels")
        name = points[key].label if key in points else "точка не выбрана"
        if value is not None:
            lines.append(f"{label} {at:%d.%m %H:%M} · {name}: {value:+.1f}°, ощущается {feels:+.1f}°.")
        else:
            lines.append(f"{label} {at:%d.%m %H:%M} · {name}: нет прогноза.")
    # Show each destination through the interval of presence, including breaks in that location.
    stays = {}
    for item in items:
        key = location_id(item, p)
        if key in stays:
            stays[key] = (min(stays[key][0], item.start), max(stays[key][1], item.end))
        else:
            stays[key] = (item.start, item.end)
    for key, (start, stop) in stays.items():
        values = [h.temperature for h in forecasts.get(key, []) if start <= h.at <= stop]
        values += [
            v
            for v in [
                temperature_at(forecasts.get(key, []), start),
                temperature_at(forecasts.get(key, []), stop),
            ]
            if v is not None
        ]
        if values:
            lines.append(
                f"Во время занятий {start:%H:%M}–{stop:%H:%M} · {points[key].label}: {min(values):+.1f}…{max(values):+.1f}°."
            )
    hourly, complete = hourly_exposure(segments, forecasts)
    if missing or not complete:
        lines.append(
            "Для полного выбора одежды не хватает прогноза/координат: "
            + ", ".join(missing or ["часть часов"])
            + ". Отметь районы через /config → «Точки погоды»; центр города автоматически не подставляется."
        )
    elif hourly:
        lines.append(
            "На улице по часам (среднее по минутам, "
            + ("по ощущениям" if p.temperature_mode == "feels" else "фактически")
            + "): "
            + "; ".join(
                f"{h.at:%d.%m %H}:00 {(h.feels if p.temperature_mode == 'feels' else h.temperature):+.1f}°"
                for h in hourly
            )
            + "."
        )
        selected = hourly
        if p.hourly_temperature == "coldest":
            selected = []
            for s in segments:
                if s["outdoor"]:
                    selected.extend(
                        h
                        for h in forecasts.get(s["point"], [])
                        if h.at <= s["end"] and h.at + timedelta(hours=1) > s["start"]
                    )
        lines.append(choose_clothing(p, selected))
        lines.append(
            "Выбор по самому прохладному часу на улице; дождь и ветер учитываются для этих участков."
        )
    else:
        lines.append("Время на улице задано как 0 минут: для подбора одежды укажи его в настройках.")
    return "\n".join(lines)
