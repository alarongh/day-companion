import asyncio
import hmac
import json
import time
from collections import defaultdict
from urllib.parse import urlparse

import httpx
from cloud_io import IMMEDIATE_REPLIES, FetchTransport, TelegramSession
from cloud_store import CloudStore
from workers import DurableObject, Response, WorkerEntrypoint

from companion.app import Application, PrivateAccess, day_buttons
from companion.providers import Providers
from companion.telegram import Bot, Dispatcher
from companion.worker import Worker


def setting(env, name):
    value = getattr(env, name, None)
    return str(value) if value else ""


def authorized(request, env):
    path = urlparse(request.url).path
    if path == "/health" and request.method == "GET":
        return True
    if path == "/telegram" and request.method == "POST":
        secret = setting(env, "WEBHOOK_SECRET")
        return bool(secret) and hmac.compare_digest(
            request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "", secret
        )
    if path.startswith("/_admin/"):
        secret = setting(env, "DEPLOY_SECRET")
        return bool(secret) and hmac.compare_digest(request.headers.get("X-Deployment-Secret") or "", secret)
    return False


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if not authorized(request, self.env):
            return Response("Not found", status=404)
        stub = self.env.COMPANION.get(self.env.COMPANION.idFromName("daily-routine"))
        try:
            return await stub.fetch(request)
        except Exception as exc:
            detail = type(exc).__name__
            if urlparse(request.url).path.startswith("/_admin/"):
                detail += ": " + str(exc)
                for name in ("BOT_TOKEN", "WEBHOOK_SECRET", "DEPLOY_SECRET"):
                    secret = setting(self.env, name)
                    if secret:
                        detail = detail.replace(secret, "<REDACTED>")
            return Response.from_json({"ok": False, "runtime_error": detail}, status=503)


class Companion(DurableObject):
    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        self.initialized = False

    def initialize(self):
        if self.initialized:
            return
        self.store = CloudStore(self.ctx.storage)
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS cloud_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_updates (id INTEGER PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cloud_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,method TEXT NOT NULL,payload TEXT NOT NULL,files TEXT NOT NULL,next_at REAL NOT NULL,created REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0);
        """)
        self.locks = defaultdict(asyncio.Lock)
        self.bot = Bot(
            setting(self.env, "BOT_TOKEN"),
            session=TelegramSession(self.store, setting(self.env, "BOT_TOKEN")),
        )
        self.client = httpx.AsyncClient(
            transport=FetchTransport(), timeout=25, headers={"User-Agent": "DayCompanion/0.2"}
        )
        self.providers = Providers(self.client)
        self.application = Application(self.store, self.providers, self.locks)
        self.application.storage_description = "Облачная версия: настройки, координаты районов, расписание, напоминания и очередь доставки хранятся в Cloudflare Durable Objects (SQLite). Ноутбук пользователя для работы бота не нужен. /delete удаляет личные данные из рабочей облачной базы."

        async def synchronous_import(function, *args):
            return function(*args)

        self.application.import_runner = synchronous_import
        allowed = {int(x) for x in setting(self.env, "ALLOWED_USER_IDS").split(",") if x.strip()}
        guard = PrivateAccess(allowed, self.store, self.locks)
        self.application.router.message.outer_middleware(guard)
        self.application.router.callback_query.outer_middleware(guard)
        self.dispatcher = Dispatcher()
        self.dispatcher.include_router(self.application.router)
        self.worker = Worker(self.store, self.providers, self.bot, self.locks, day_buttons)
        self.mutex = asyncio.Lock()
        self.active_updates = set()
        self.initialized = True

    def meta(self, key, default=""):
        row = self.store.db.execute("SELECT value FROM cloud_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        self.store.db.execute("INSERT OR REPLACE INTO cloud_meta VALUES (?,?)", (key, str(value)))

    async def schedule(self, delay=20000):
        await self.ctx.storage.setAlarm(time.time() * 1000 + delay)

    async def process_update(self, ident):
        if ident in self.active_updates:
            return
        row = self.store.db.execute(
            "SELECT payload,attempts,created FROM cloud_updates WHERE id=? AND status='pending'", (ident,)
        ).fetchone()
        if row is None:
            return
        payload, attempts, created = row
        self.active_updates.add(ident)
        context = IMMEDIATE_REPLIES.set(True)
        try:
            await self.dispatcher.feed_update(self.bot, json.loads(payload))
            self.store.db.execute("UPDATE cloud_updates SET status='done',payload='' WHERE id=?", (ident,))
            self.set_meta("last_update_ms", round((time.time() - created) * 1000))
        except Exception as exc:
            attempts += 1
            self.store.db.execute(
                "UPDATE cloud_updates SET attempts=?,status=? WHERE id=?",
                (attempts, "failed" if attempts >= 5 else "pending", ident),
            )
            self.set_meta("last_error", type(exc).__name__)
        finally:
            IMMEDIATE_REPLIES.reset(context)
            self.active_updates.discard(ident)

    async def process_updates(self):
        rows = self.store.db.execute(
            "SELECT id FROM cloud_updates WHERE status='pending' ORDER BY id LIMIT 5"
        ).fetchall()
        await asyncio.gather(*(self.process_update(row[0]) for row in rows))

    async def fetch(self, request):
        if not authorized(request, self.env):
            return Response("Not found", status=404)
        if not setting(self.env, "BOT_TOKEN"):
            return Response.from_json({"ok": False, "configured": False}, status=503)
        self.initialize()
        path = urlparse(request.url).path
        if path == "/health":
            return Response.from_json(
                {"ok": True, "service": "day-companion-bot", "active": self.meta("active") == "true"}
            )
        if path == "/telegram":
            if self.meta("active") != "true":
                return Response("Not active", status=503)
            body = await request.text()
            if len(body) > 1024 * 1024:
                return Response("Too large", status=413)
            update = json.loads(body)
            ident = int(update["update_id"])
            self.store.db.execute(
                "INSERT OR IGNORE INTO cloud_updates(id,payload,created) VALUES (?,?,?)",
                (ident, body, time.time()),
            )
            # Durable fallback is armed before handling, but normal replies run now.
            await self.schedule(1000)
            await self.process_update(ident)
            return Response("OK")
        async with self.mutex:
            if path == "/_admin/status":
                return Response.from_json(self.status())
            if path == "/_admin/diagnostics":
                missing = sum(
                    1
                    for uid in self.store.users()
                    if (p := self.store.profile(uid)).places
                    and any(point.latitude is None for point in p.places)
                )
                failed = dict(
                    self.store.db.execute(
                        "SELECT method,COUNT(*) FROM cloud_outbox WHERE failed=1 GROUP BY method"
                    ).fetchall()
                )
                return Response.from_json(
                    {"profiles_missing_weather_points": missing, "failed_by_method": failed}
                )
            if path == "/_admin/digest":
                import hashlib

                tables = {
                    table: sorted(
                        [
                            json.dumps(list(row), ensure_ascii=False, separators=(",", ":"))
                            for row in self.store.db.execute(f"SELECT * FROM {table}")
                        ]
                    )
                    for table in (
                        "users",
                        "schedules",
                        "personal",
                        "drafts",
                        "sessions",
                        "overrides",
                        "deliveries",
                        "events",
                    )
                }
                digest = hashlib.sha256(
                    json.dumps(tables, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                ).hexdigest()
                return Response.from_json({"sha256": digest})
            if path == "/_admin/import" and request.method == "POST":
                if self.meta("active") == "true" or self.store.users():
                    return Response("Migration requires an inactive empty service", status=409)
                raw = await request.text()
                if len(raw) > 10 * 1024 * 1024:
                    return Response("Too large", status=413)
                counts = self.import_database(json.loads(raw))
                return Response.from_json({"ok": True, "counts": counts})
            if path == "/_admin/activate" and request.method == "POST":
                await request.text()
                self.set_meta("active", "true")
                await self.schedule(1000)
                return Response.from_json({"ok": True})
            if path == "/_admin/pause" and request.method == "POST":
                await request.text()
                self.set_meta("active", "false")
                await self.ctx.storage.deleteAlarm()
                return Response.from_json({"ok": True})
            if path == "/_admin/probe" and request.method == "POST":
                from datetime import date

                from companion.domain import Lesson, Profile, occurrences
                from companion.forecast import route_weather
                from companion.journey import journey_times, sleep_times

                data = json.loads(await request.text())
                if data.get("transport"):
                    import base64

                    from cloud_io import telegram

                    # chat_id=0 cannot deliver a message. Only synthetic bytes are sent.
                    if not isinstance(data.get("transport"), bool):
                        return Response("Invalid probe", status=400)
                    status, raw = await telegram(
                        self.bot.token,
                        "sendDocument",
                        {"chat_id": 0, "document": "attach://document"},
                        {
                            "document": {
                                "name": "probe.txt",
                                "data": base64.b64encode(b"Synthetic file probe").decode(),
                            }
                        },
                    )
                    try:
                        result = json.loads(raw)
                        description = result.get("description", "")
                    except ValueError:
                        description = "Non-JSON Telegram response"
                    return Response.from_json({"http": status, "description": description, "bytes": len(raw)})
                p = Profile.model_validate(data["profile"])
                day = date.fromisoformat(data["day"])
                items = occurrences([Lesson.model_validate(x) for x in data["lessons"]], p, day)
                times = journey_times(items, p)
                sleep = sleep_times(items, p, day)
                weather = await route_weather(self.providers, p, items) if data.get("weather") else ""
                return Response.from_json(
                    {
                        "ok": True,
                        "home": times["home_estimate"].isoformat() if times else None,
                        "wake": sleep[0].isoformat() if sleep else None,
                        "bed": sleep[1].isoformat() if sleep else None,
                        "weather_complete": bool(weather)
                        and "не хватает" not in weather
                        and "нет прогноза" not in weather,
                    }
                )
        return Response("Not found", status=404)

    def status(self):
        return {
            "configured": True,
            "active": self.meta("active") == "true",
            "counts": {
                table: self.store.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "users",
                    "schedules",
                    "personal",
                    "drafts",
                    "sessions",
                    "overrides",
                    "deliveries",
                    "events",
                )
            },
            "outbox_pending": self.store.db.execute(
                "SELECT COUNT(*) FROM cloud_outbox WHERE failed=0"
            ).fetchone()[0],
            "outbox_failed": self.store.db.execute(
                "SELECT COUNT(*) FROM cloud_outbox WHERE failed=1"
            ).fetchone()[0],
            "updates_pending": self.store.db.execute(
                "SELECT COUNT(*) FROM cloud_updates WHERE status='pending'"
            ).fetchone()[0],
            "updates_failed": self.store.db.execute(
                "SELECT COUNT(*) FROM cloud_updates WHERE status='failed'"
            ).fetchone()[0],
            "scheduler_at": self.meta("scheduler_at"),
            "last_error": self.meta("last_error"),
            "last_update_ms": self.meta("last_update_ms"),
            "reply_mode": "immediate",
        }

    def import_database(self, data):
        from companion.domain import Lesson, Profile

        columns = {
            "users": ["uid", "profile", "revision"],
            "schedules": ["uid", "content"],
            "personal": ["uid", "content"],
            "drafts": ["uid", "content", "meta", "nonce"],
            "sessions": ["uid", "content"],
            "overrides": ["uid", "day", "lesson_id", "content"],
            "deliveries": ["uid", "key", "status", "attempts", "next_at"],
            "events": ["uid", "day", "name", "happened"],
        }
        for row in data.get("users", []):
            Profile.model_validate_json(row[1])
        for table in ("schedules", "personal"):
            for row in data.get(table, []):
                for item in json.loads(row[1]):
                    Lesson.model_validate(item)

        def commit():
            for table, keys in columns.items():
                for row in data.get(table, []):
                    if len(row) != len(keys):
                        raise ValueError("Invalid migration row")
                    self.store.db.execute(
                        f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", row
                    )
            self.set_meta("migration_at", time.time())

        self.ctx.storage.transactionSync(commit)
        return {k: len(data.get(k, [])) for k in columns}

    async def alarm(self):
        self.initialize()
        if self.meta("active") != "true":
            return
        from datetime import datetime, timezone

        try:
            await self.process_updates()
            now = datetime.now(timezone.utc)
            for uid in self.store.users():
                try:
                    await self.worker.tick_user(uid, now)
                except Exception as exc:
                    self.set_meta("last_error", type(exc).__name__)
            await self.bot.session.flush()
            self.set_meta("scheduler_at", time.time())
            self.store.db.execute(
                "DELETE FROM cloud_updates WHERE status='done' AND created<?", (time.time() - 7 * 86400,)
            )
        finally:
            await self.schedule()
