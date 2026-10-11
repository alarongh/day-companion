from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from .domain import Profile, WeatherHour
from .importers import parse_ics


class Providers:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def cities(self, name):
        r = await self.client.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": name, "count": 5, "language": "ru", "format": "json"},
        )
        r.raise_for_status()
        return r.json().get("results", [])

    async def groups(self, name):
        r = await self.client.get(
            "https://schedule-of.mirea.ru/schedule/api/search", params={"match": name, "limit": 25}
        )
        r.raise_for_status()
        return [x for x in r.json().get("data", []) if x.get("scheduleTarget") == 1][:10]

    async def mirea(self, group_id: int, p: Profile, first: date, last: date):
        r = await self.client.get(
            f"https://schedule-of.mirea.ru/schedule/api/ical/1/{int(group_id)}",
            params={"includeMeta": "true"},
        )
        r.raise_for_status()
        if len(r.content) > 10 * 1024 * 1024:
            raise ValueError("Ответ сервиса расписания слишком большой")
        return parse_ics(r.content, p, first, last)

    async def weather(self, p: Profile, windows):
        if p.latitude is None or p.longitude is None or not windows:
            return []
        r = await self.client.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": p.latitude,
                "longitude": p.longitude,
                "timezone": p.timezone,
                "forecast_days": 3,
                "wind_speed_unit": "kmh",
                "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,wind_speed_10m",
            },
        )
        r.raise_for_status()
        hourly = r.json()["hourly"]
        hours = []
        for index, value in enumerate(hourly["time"]):
            keys = (
                "temperature_2m",
                "apparent_temperature",
                "precipitation_probability",
                "precipitation",
                "wind_speed_10m",
            )
            values = [hourly[k][index] for k in keys]
            if any(v is None for v in values):
                continue
            hours.append(
                WeatherHour(
                    at=datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(p.timezone)),
                    temperature=values[0],
                    feels=values[1],
                    rain_probability=values[2],
                    precipitation=values[3],
                    wind=values[4],
                )
            )
        selected = []
        # Include both adjacent forecast hours: a 07:50 departure must not use just 08:00.
        from datetime import timedelta

        for hour in hours:
            if any(hour.at <= end and hour.at + timedelta(hours=1) > begin for begin, end in windows):
                selected.append(hour)
        return selected
