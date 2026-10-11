from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .delivery import NOTICE_CONTEXT
from .domain import plan_notices, quiet
from .journey import extra_notices
from .service import briefing, day_items
from .telegram import TelegramForbiddenError, TelegramRetryAfter

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, store, providers, bot, locks, markup):
        self.store, self.providers, self.bot, self.locks, self.markup = store, providers, bot, locks, markup
        self.refresh_attempts = {}
        self.source_locks = defaultdict(asyncio.Lock)

    async def refresh(self, uid, now, force=False):
        async with self.source_locks[uid]:
            return await self._refresh(uid, now, force)

    async def _refresh(self, uid, now, force):
        p = self.store.profile(uid)
        if p.routine_mode == "sleep_only" or p.source != "mirea" or not p.mirea_id:
            return True
        previous_attempt = self.refresh_attempts.get(uid)
        interval = 1 if force else 300
        if previous_attempt and (now - previous_attempt).total_seconds() < interval:
            return bool(p.source_checked and datetime.fromisoformat(p.source_checked) >= previous_attempt)
        if (
            not force
            and p.source_checked
            and (now - datetime.fromisoformat(p.source_checked)).total_seconds() < 300
        ):
            return True
        self.refresh_attempts[uid] = now
        revision = self.store.revision(uid)
        try:
            first, last = now.date() - timedelta(days=1), now.date() + timedelta(days=120)
            lessons = await self.providers.mirea(p.mirea_id, p, first, last)
            async with self.locks[uid]:
                if not self.store.exists(uid) or self.store.revision(uid) != revision:
                    return False
                previous = self.store.lessons(uid)
                personal_ids = {x.id for x in self.store.personal(uid)}
                changed = {
                    x.id: x.model_dump_json()
                    for x in previous
                    if x.date and first <= x.date <= last and x.id not in personal_ids
                } != {x.id: x.model_dump_json() for x in lessons}
                self.store.replace(uid, lessons)
                p.source_checked, p.source_until = now.isoformat(), last
                self.store.save_profile(uid, p)
                if previous and changed and p.notifications:
                    # Persistent pending flag survives quiet hours and temporary Telegram outages.
                    s = self.store.session(uid)
                    s["source_changed"] = True
                    self.store.set_session(uid, s)
                return True
        except Exception as exc:
            log.warning("Schedule refresh failed: %s", type(exc).__name__)
            return False

    def notices(self, uid, p, day):
        items = day_items(self.store, uid, day)
        return plan_notices(items, p, day) + extra_notices(items, p, day, self.store.events(uid, day))

    @staticmethod
    def due(notice, now, p):
        return (
            notice.at <= now < notice.expires
            and not quiet(now, p)
            and (now - notice.at).total_seconds()
            <= (3600 if notice.kind == "briefing" else notice.catch_up_seconds)
        )

    async def prepare_delivery(self, uid, metadata, utc_now=None):
        """Cloud retries must use the current plan and freshly fetched weather."""
        if not self.store.exists(uid):
            return None
        p = self.store.profile(uid)
        now = (utc_now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(p.timezone))
        checked = await self.refresh(uid, now, force=True)
        async with self.locks[uid]:
            if not self.store.exists(uid):
                return None
            p = self.store.profile(uid)
            day = datetime.fromisoformat(metadata["day"]).date()
            notice = next((n for n in self.notices(uid, p, day) if n.key == metadata["key"]), None)
            if notice and now < notice.at:
                # Briefings deliberately keep one key per day. If the queued
                # briefing moved later, allow that same key to be queued again.
                self.store.retry(uid, notice.key, seconds=0)
            if not notice or not self.due(notice, now, p):
                return None
            text = (
                await briefing(self.store, self.providers, uid, day)
                if notice.kind == "briefing"
                else notice.text
            )
            if not checked and p.source == "mirea" and p.routine_mode == "full":
                text += "\nИсточник расписания недоступен. Использовано последнее сохранённое расписание."
            return text[:4000]

    async def tick_user(self, uid, utc_now):
        p = self.store.profile(uid)
        now = utc_now.astimezone(ZoneInfo(p.timezone))
        await self.refresh(uid, now)
        p = self.store.profile(uid)
        days = [now.date() + timedelta(days=n) for n in (-2, -1, 0, 1, 2)]
        checked = True
        if any(
            self.due(n, now, p) and self.store.delivery_ready(uid, n.key, utc_now.timestamp())
            for day in days
            for n in self.notices(uid, p, day)
        ):
            checked = await self.refresh(uid, now, force=True)
        async with self.locks[uid]:
            if not self.store.exists(uid):
                return
            p = self.store.profile(uid)
            if not p.notifications:
                return
            s = self.store.session(uid)
            paused = p.pause_until and now.date() <= p.pause_until
            if p.routine_mode == "full" and s.get("source_changed") and not quiet(now, p) and not paused:
                try:
                    await self.bot.send_message(
                        uid,
                        "Расписание источника изменилось. Время выхода и напоминания пересчитаны. Проверь «Сегодня» и «Завтра».",
                    )
                    s.pop("source_changed", None)
                    self.store.set_session(uid, s)
                except Exception as exc:
                    log.warning("Change notification failed: %s", type(exc).__name__)
            # Include tomorrow: a very early first lesson can require leaving before midnight.
            for day in days:
                notices = self.notices(uid, p, day)
                for notice in notices:
                    if not self.due(notice, now, p):
                        continue
                    if not self.store.claim(uid, notice.key, utc_now.timestamp()):
                        continue
                    context = None
                    try:
                        deferred = (
                            getattr(getattr(self.bot, "session", None), "deferred_notices", False) is True
                        )
                        text = (
                            await briefing(self.store, self.providers, uid, day)
                            if notice.kind == "briefing" and not deferred
                            else notice.text
                        )
                        if not checked and p.source == "mirea" and p.routine_mode == "full":
                            text += "\nИсточник расписания недоступен. Использовано последнее сохранённое расписание."
                        context = NOTICE_CONTEXT.set({"key": notice.key, "day": day.isoformat()})
                        await self.bot.send_message(
                            uid,
                            text[:4000] or "Сводка готовится.",
                            reply_markup=self.markup(day)
                            if notice.kind in ("briefing", "prepare", "depart")
                            else None,
                        )
                    except TelegramForbiddenError:
                        p.notifications = False
                        self.store.save_profile(uid, p)
                        self.store.sent(uid, notice.key)
                        return
                    except TelegramRetryAfter as exc:
                        self.store.retry(uid, notice.key, exc.retry_after + 1)
                    except Exception as exc:
                        log.warning("Notification delivery failed: %s", type(exc).__name__)
                        self.store.retry(uid, notice.key)
                    else:
                        self.store.sent(uid, notice.key)
                    finally:
                        if context is not None:
                            NOTICE_CONTEXT.reset(context)

    async def run(self):
        while True:
            now = datetime.now(ZoneInfo("UTC"))
            users = self.store.users()
            semaphore = asyncio.Semaphore(8)

            async def one(uid, semaphore=semaphore, now=now):
                async with semaphore:
                    try:
                        await self.tick_user(uid, now)
                    except Exception as exc:
                        log.warning("User scheduler failed: %s", type(exc).__name__)

            await asyncio.gather(*(one(uid) for uid in users))
            heartbeat = Path(self.store.path).parent / "scheduler-heartbeat"
            heartbeat.write_text(str(datetime.now(ZoneInfo("UTC")).timestamp()), encoding="ascii")
            await asyncio.sleep(20)
