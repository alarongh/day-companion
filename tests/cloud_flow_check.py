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
workers.Blob = workers.FormData = workers.Request = object
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

    # Exercise the actual upload serialization, not a stubbed Telegram method.
    from email import policy
    from email.parser import BytesParser

    from companion.telegram import CallbackQuery

    acknowledgement = CallbackQuery(bot=bot, id="ack", **{"from": user}, message=message)
    await acknowledgement.answer()
    assert "text" not in cloud_io.telegram.call_args.args[2], (
        "Empty callback acknowledgements must omit text, not send JSON null"
    )
    captured = []

    class UploadRequest:
        def __init__(self, url, **kwargs):
            self.url, self.kwargs = url, kwargs
            captured.append(self)

    cloud_io.Request = UploadRequest
    cloud_io.fetch = AsyncMock(
        return_value=SimpleNamespace(
            status=400,
            text=AsyncMock(return_value='{"ok":false,"description":"Bad Request: chat not found"}'),
        )
    )
    content = "Synthetic unicode file: Привет".encode()
    import base64

    status, raw = await cloud_io._telegram(
        "fake-token",
        "sendDocument",
        {"chat_id": 0, "document": "attach://document"},
        {"document": {"name": "prompt.txt", "data": base64.b64encode(content).decode()}},
    )
    assert status == 400 and "chat not found" in raw
    request = captured[-1]
    assert isinstance(request.kwargs["body"], bytes), "Uploads must use a complete, known-length body"
    headers = (
        "Content-Type: " + request.kwargs["headers"]["Content-Type"] + "\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode()
    multipart = BytesParser(policy=policy.default).parsebytes(headers + request.kwargs["body"])
    parts = list(multipart.iter_parts())
    assert len(parts) == 2
    file = next(part for part in parts if part.get_filename())
    assert file.get_filename() == "prompt.txt" and file.get_payload(decode=True) == content
    assert (
        len([part for part in parts if part.get_param("name", header="content-disposition") == "document"])
        == 1
    )
    print("Real multipart file bytes and callback acknowledgements verified")

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

    # A separate, brand-new user exercises the deployed adapter's whole setup
    # flow. An already configured user's profile must remain unchanged.
    from datetime import timedelta

    from companion.domain import WeatherHour

    existing = service.store.profile(1)
    existing.city, existing.travel = "Existing test city", 99
    service.store.save_profile(1, existing)
    preserved = service.store.export(1)
    deliveries = []

    async def collect(token, method, payload, files=None):
        deliveries.append((method, payload, files))
        return 200, '{"ok":true,"result":true}'

    async def weather(p, windows):
        hour = windows[0][0].replace(minute=0, second=0, microsecond=0)
        return [
            WeatherHour(
                at=hour + timedelta(hours=i),
                temperature=7,
                feels=5,
                rain_probability=20,
                precipitation=0,
                wind=10,
            )
            for i in range(3)
        ]

    cloud_io.telegram = collect
    service.providers.weather = weather
    counter = 100

    async def onboarding(text=None, callback=None, location=None):
        nonlocal counter
        counter += 1
        person = {"id": 8, "is_bot": False, "first_name": "New test user"}
        msg = {"message_id": counter, "from": person, "chat": {"id": 8, "type": "private"}}
        if callback:
            msg["from"] = {"id": 999, "is_bot": True}
            event = {"callback_query": {"id": str(counter), "from": person, "message": msg, "data": callback}}
        else:
            event = {"message": {**msg, "text": text, **({"location": location} if location else {})}}
        request = Request(counter, uid=8)
        request.payload = {"update_id": counter, **event}
        response = await service.fetch(request)
        assert response.status == 200
        assert (
            service.store.db.execute("SELECT status FROM cloud_updates WHERE id=?", (counter,)).fetchone()[0]
            == "done"
        )

    await onboarding("/start")
    await onboarding(callback="cfg:setup")
    document = next(files["document"] for method, payload, files in deliveries if method == "sendDocument")
    assert document["name"] == "ai-setup-prompt.txt"
    assert "Existing test city" not in base64.b64decode(document["data"]).decode()
    assert all("text" not in payload for method, payload, _ in deliveries if method == "answerCallbackQuery")
    await onboarding("Погода")
    assert "Погода ещё не настроена" in deliveries[-1][1]["text"]
    await onboarding(callback="cfg:places")
    await onboarding(callback="place:add")
    await onboarding("New test district")
    await onboarding(location={"latitude": 55.7, "longitude": 37.6})
    assert service.store.profile(8).places[0].latitude == 55.7
    assert "+7.0 °C" in deliveries[-1][1]["text"]
    assert not service.store.lessons(8)
    await onboarding(callback="garment:add")
    await onboarding("Куртка")
    for invalid in ("67", "50 67", "3 67"):
        await onboarding(invalid)
        assert service.store.session(8)["step"] == 1
        assert "Проверь ввод" in deliveries[-1][1]["text"]
        assert "input_value" not in deliveries[-1][1]["text"]
    for value in ("5 10", "да", "нет"):
        await onboarding(value)
    assert service.store.profile(8).garments[0].waterproof
    assert service.store.export(1) == preserved
    assert all(payload.get("chat_id", 8) == 8 for _, payload, _ in deliveries)
    print(
        "Separate new cloud user: prompt file, weather without schedule, wardrobe recovery and existing-profile isolation verified"
    )


asyncio.run(main())
