import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage
from aiogram.types import CallbackQuery, Chat, Location, Message, Update, User

from companion.app import Application, PrivateAccess, item_key
from companion.configuration import snapshot
from companion.domain import Garment
from companion.store import Store


class TelegramStub(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, SendDocument)):
            return Message(
                message_id=len(self.calls),
                date=datetime.now(timezone.utc),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        raise AssertionError(type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        yield b""


def message(uid, text):
    return Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="User"),
        text=text,
    )


async def test_real_dispatcher_onboarding_rules_and_private_drafts(tmp_path):
    store = Store(tmp_path / "bot.sqlite3")
    providers = AsyncMock()
    providers.cities.return_value = [
        {
            "name": "Город",
            "latitude": 55.7,
            "longitude": 37.6,
            "timezone": "Europe/Moscow",
            "country": "Россия",
        }
    ]
    locks = defaultdict(asyncio.Lock)
    app = Application(store, providers, locks)
    guard = PrivateAccess(set(), store, locks)
    app.router.message.outer_middleware(guard)
    app.router.callback_query.outer_middleware(guard)
    dp = Dispatcher()
    dp.include_router(app.router)
    session = TelegramStub()
    bot = Bot(("123456:" + "A" * 35), session=session)
    counter = 0

    async def text(uid, value):
        nonlocal counter
        counter += 1
        await dp.feed_update(bot, Update(update_id=counter, message=message(uid, value)))

    async def click(uid, value):
        nonlocal counter
        counter += 1
        await dp.feed_update(
            bot,
            Update(
                update_id=counter,
                callback_query=CallbackQuery(
                    id=str(counter),
                    from_user=User(id=uid, is_bot=False, first_name="User"),
                    chat_instance="test",
                    message=message(uid, "bot"),
                    data=value,
                ),
            ),
        )

    await text(1, "/start")
    await text(1, "/setup")
    await text(1, "Город")
    nonce = store.session(1)["nonce"]
    await click(1, f"citypick:{nonce}:0")
    await text(1, "50 40 10 10")
    assert store.profile(1).travel == 50
    assert not store.profile(1).notifications
    await click(1, "garment:add")
    for value in ("Куртка", "-5 15", "да", "да"):
        await text(1, value)
    assert store.profile(1).garments[0].waterproof
    await click(1, "rule:add")
    nonce = store.session(1)["nonce"]
    await click(1, f"rulepick:{nonce}:{item_key('Куртка')}")
    for value in ("Дождь", "-5 14.9", "дождь", "0", "учёба", "100"):
        await text(1, value)
    assert store.profile(1).rules[0].priority == 100
    p = store.profile(1)
    p.garments.append(Garment(name="Пальто", minimum=0, maximum=10))
    store.save_profile(1, p)
    await click(1, f"garment:delete:{item_key('Куртка')}")
    await click(1, f"garment:delete:{item_key('Куртка')}")
    assert [g.name for g in store.profile(1).garments] == ["Пальто"]
    await click(1, "upload")
    await text(1, "2026-10-07 | 09:00-10:30 | Личная математика | 312 | В-78")
    with store.connect() as db:
        draft_nonce = db.execute("SELECT nonce FROM drafts WHERE uid=1").fetchone()[0]
    await text(2, "/start")
    await click(2, f"preview:{draft_nonce}")
    assert not any(isinstance(x, SendDocument) and x.chat_id == 2 for x in session.calls)
    assert not store.lessons(2)
    await click(1, f"commit:{draft_nonce}")
    assert store.lessons(1)[0].title == "Личная математика"
    await text(1, "/config")
    await click(1, "cfg:patch")
    assert any(
        isinstance(x, SendDocument) and x.document.filename == "ai-patch-prompt.txt" for x in session.calls
    )
    state = snapshot(store, 1)
    await text(
        1,
        json.dumps(
            {
                "format": "day-companion/v2",
                "mode": "patch",
                "base_revision": state["base_revision"],
                "profile": {
                    "sleep": {"enabled": True, "hours": 8.5},
                    "home_place": "home",
                    "destination_place": "uni",
                },
                "places": {
                    "upsert": [{"id": "home", "label": "Дом, район"}, {"id": "uni", "label": "Вуз, район"}]
                },
            }
        ),
    )
    with store.connect() as db:
        cfg_nonce = db.execute("SELECT nonce FROM drafts WHERE uid=1").fetchone()[0]
    assert not store.profile(1).sleep.enabled  # Preview has not changed live settings.
    await click(2, f"cfgapply:{cfg_nonce}")
    assert not store.profile(1).sleep.enabled
    await click(1, f"cfgapply:{cfg_nonce}")
    assert store.profile(1).sleep.hours == 8.5 and store.profile(1).sleep.enabled
    assert store.profile(1).travel == 50 and len(store.lessons(1)) == 1
    await click(1, "place:home")
    counter += 1
    await dp.feed_update(
        bot,
        Update(
            update_id=counter,
            message=message(1, "").model_copy(
                update={"text": None, "location": Location(latitude=55.7, longitude=37.6)}
            ),
        ),
    )
    assert store.profile(1).places[0].latitude == 55.7
    await text(1, "Я дома")
    assert "home" in store.events(1, app.local_now(1).date())
    await text(1, "/delete")
    nonce = store.session(1)["nonce"]
    await click(1, f"delete:{nonce}")
    assert not store.exists(1)
    await click(1, f"preview:{draft_nonce}")
    assert not store.exists(1)
    await bot.session.close()


async def test_group_messages_do_not_create_user(tmp_path):
    store = Store(tmp_path / "bot.sqlite3")
    handler = AsyncMock()
    guard = PrivateAccess(set(), store, defaultdict(asyncio.Lock))
    m = message(1, "/start").model_copy(update={"chat": Chat(id=-10, type="group")})
    session = TelegramStub()
    bot = Bot(("123456:" + "A" * 35), session=session)
    m = m.as_(bot)
    await guard(handler, m, {})
    handler.assert_not_awaited()
    assert not store.exists(1)
