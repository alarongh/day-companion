"""Run in a fresh interpreter against the generated cloud application."""
# ruff: noqa: E402

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloudflare" / "src"))
workers = ModuleType("workers")
workers.Blob = workers.FormData = object
workers.fetch = AsyncMock()
workers.DurableObject = workers.WorkerEntrypoint = object


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    @classmethod
    def from_json(cls, body, status=200):
        return cls(body, status)


workers.Response = Response
sys.modules["workers"] = workers

import cloud_io

from companion.app import Application, PrivateAccess
from companion.telegram import Bot, Dispatcher


async def main():
    import importlib.util

    from cloud_store import CloudStore

    spec = importlib.util.spec_from_file_location(
        "cloud_store_tests", Path(__file__).with_name("test_cloud_store.py")
    )
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    store = CloudStore(helpers.Storage())
    store.db.execute(
        "CREATE TABLE cloud_outbox(id INTEGER PRIMARY KEY,uid INTEGER,method TEXT,payload TEXT,files TEXT,next_at REAL,created REAL)"
    )
    locks = defaultdict(asyncio.Lock)
    app = Application(store, AsyncMock(), locks)
    guard = PrivateAccess(set(), store, locks)
    app.router.message.outer_middleware(guard)
    app.router.callback_query.outer_middleware(guard)
    dp = Dispatcher()
    dp.include_router(app.router)
    bot = Bot("fake-token", cloud_io.TelegramSession(store))
    cloud_io.telegram = AsyncMock(return_value=(200, '{"ok":true,"result":true}'))
    user = {"id": 1, "is_bot": False, "first_name": "Test"}
    # Use Telegram's actual wire field, not aiogram's normalized from_user.
    message = {"message_id": 1, "chat": {"id": 1, "type": "private"}, "from": user}
    await dp.feed_update(bot, {"message": {**message, "text": "/start"}})
    assert store.exists(1)
    await dp.feed_update(bot, {"message": {**message, "text": "/config"}})
    payloads = [json.loads(row[0]) for row in store.db.execute("SELECT payload FROM cloud_outbox")]
    assert any("reply_markup" in value for value in payloads)
    await dp.feed_update(
        bot,
        {
            "callback_query": {
                "id": "test",
                "from": user,
                "message": {**message, "from": {"id": 123456, "is_bot": True, "first_name": "Bot"}},
                "data": "cfg:setup",
            }
        },
    )
    files = [json.loads(row[0]) for row in store.db.execute("SELECT files FROM cloud_outbox")]
    assert any(value for value in files), "AI setup prompt must be sent as a real file"
    await dp.feed_update(
        bot,
        {
            "message": {
                **message,
                "chat": {"id": -1, "type": "group"},
                "from": {"id": 2, "is_bot": False},
                "text": "/start",
            }
        },
    )
    assert not store.exists(2)
    print("Cloud Telegram flow passed: commands, private access, keyboards, callback, file outbox")

    # Exercise the real webhook and alarm paths, not just the dispatcher.
    from entry import Companion

    storage = helpers.Storage()
    storage.setAlarm = AsyncMock()
    service = object.__new__(Companion)
    service.ctx = SimpleNamespace(storage=storage)
    service.env = SimpleNamespace(BOT_TOKEN="fake-token", WEBHOOK_SECRET="test", ALLOWED_USER_IDS="")
    service.initialized = False
    service.initialize()
    service.set_meta("active", "true")
    sent = []
    sending = asyncio.Event()
    release = asyncio.Event()

    async def telegram(token, method, payload, files=None):
        if payload.get("chat_id") == 3 and not release.is_set():
            sending.set()
            await release.wait()
        sent.append((method, payload))
        return 200, '{"ok":true,"result":true}'

    cloud_io.telegram = telegram

    class Request:
        url = "https://test/telegram"
        method = "POST"
        headers = {"X-Telegram-Bot-Api-Secret-Token": "test"}

        def __init__(self, ident, uid=1):
            self.payload = {
                "update_id": ident,
                "message": {
                    "message_id": ident,
                    "from": {"id": uid, "is_bot": False},
                    "chat": {"id": uid, "type": "private"},
                    "text": "/start",
                },
            }

        async def text(self):
            return json.dumps(self.payload)

    response = await service.fetch(Request(1))
    assert response.status == 200 and sent, (
        "Reply must be delivered before webhook completion without any alarm"
    )
    assert service.store.db.execute("SELECT COUNT(*) FROM cloud_outbox").fetchone()[0] == 0
    count = len(sent)
    await service.fetch(Request(1))
    assert len(sent) == count, "Repeated webhook must not send again"

    # An in-flight outgoing request for one user must not block another user,
    # and an overlapping alarm/webhook must not process the same update twice.
    slow = asyncio.create_task(service.fetch(Request(2, uid=3)))
    await asyncio.wait_for(sending.wait(), timeout=1)
    await service.fetch(Request(2, uid=3))
    await asyncio.wait_for(service.fetch(Request(3, uid=4)), timeout=1)
    assert any(payload.get("chat_id") == 4 for _, payload in sent)
    release.set()
    await slow
    assert len([payload for _, payload in sent if payload.get("chat_id") == 3]) == count

    # Failed sends remain durable and retry on a later alarm.
    cloud_io.telegram = AsyncMock(side_effect=TimeoutError())
    await service.fetch(Request(4, uid=5))
    pending = service.store.db.execute("SELECT attempts,failed FROM cloud_outbox WHERE uid=5").fetchone()
    assert pending == (1, 0)
    service.store.db.execute("UPDATE cloud_outbox SET next_at=0 WHERE uid=5")
    cloud_io.telegram = AsyncMock(return_value=(200, '{"ok":true,"result":true}'))
    service.worker.tick_user = AsyncMock()
    await service.alarm()
    assert service.store.db.execute("SELECT COUNT(*) FROM cloud_outbox").fetchone()[0] == 0
    print(
        "Cloud webhook speed, in-flight deduplication, per-user delivery and durable timeout retry verified"
    )


asyncio.run(main())
