"""Small Telegram transport interface for the shared application on Workers.

Only the messages, files, keyboards and filters used by this application are
implemented. Domain validation remains in the shared Pydantic models.
"""

import json
from types import SimpleNamespace


def encode(value):
    if isinstance(value, Object):
        return {key: encode(item) for key, item in vars(value).items() if item is not None and key != "bot"}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


class Object:
    def __init__(self, **values):
        self.__dict__.update(values)

    def model_dump(self, **kwargs):
        return encode(self)


BotCommand = InlineKeyboardButton = InlineKeyboardMarkup = KeyboardButton = ReplyKeyboardMarkup = Object
BaseMiddleware = object


class BufferedInputFile:
    def __init__(self, data, filename):
        self.data, self.filename = data, filename


class Message(Object):
    def __init__(self, bot=None, **values):
        if "from" in values:
            values["from_user"] = values.pop("from")
        super().__init__(bot=bot, text=None, document=None, photo=[], location=None, from_user=None)
        for key, value in values.items():
            if key in ("chat", "document", "location", "from_user") and isinstance(value, dict):
                defaults = {"file_size": None, "file_name": None} if key == "document" else {}
                if key == "location":
                    defaults = {"live_period": None}
                defaults.update(value)
                value = SimpleNamespace(**defaults)
            if key == "photo":
                value = [SimpleNamespace(**{"file_size": None, **x}) for x in value]
            setattr(self, key, value)

    async def answer(self, text, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)

    async def answer_document(self, document, **kwargs):
        return await self.bot.session.enqueue(
            "sendDocument", {"chat_id": self.chat.id, "document": document, **kwargs}
        )


class CallbackQuery(Object):
    def __init__(self, bot=None, **values):
        if "from" in values:
            values["from_user"] = values.pop("from")
        values["from_user"] = SimpleNamespace(**values["from_user"])
        values["message"] = Message(bot=bot, **values["message"]) if values.get("message") else None
        super().__init__(bot=bot, **values)

    async def answer(self, text=None, **kwargs):
        try:
            payload = {"callback_query_id": self.id, **kwargs}
            if text is not None:
                payload["text"] = text
            return await self.bot.api("answerCallbackQuery", payload)
        except Exception:
            # Acknowledgements expire quickly; durable replies still reach the chat.
            return False


class Bot:
    def __init__(self, token, session):
        self.token, self.session = token, session

    async def send_message(self, chat_id, text, **kwargs):
        return await self.session.enqueue("sendMessage", {"chat_id": chat_id, "text": text, **kwargs})

    async def api(self, method, payload):
        from cloud_io import telegram

        status, raw = await telegram(self.token, method, encode(payload))
        data = json.loads(raw)
        if not data.get("ok"):
            raise ValueError("Telegram не принял запрос: " + str(status))
        return data.get("result")

    async def download(self, file, destination):
        from workers import fetch

        result = await self.api("getFile", {"file_id": file.file_id})
        response = await fetch("https://api.telegram.org/file/bot" + self.token + "/" + result["file_path"])
        if response.status != 200:
            raise ValueError("Не удалось скачать файл из Telegram")
        content = await response.bytes()
        if len(content) > 10 * 1024 * 1024:
            raise ValueError("Файл больше 10 МБ")
        destination.write(content)


class Filter:
    def __init__(self, predicate):
        self.predicate = predicate

    def __call__(self, event):
        return self.predicate(event)

    def __or__(self, other):
        return Filter(lambda event: self(event) or other(event))


class Fields:
    def __getattr__(self, name):
        return Filter(lambda event: bool(getattr(event, name, None)))


F = Fields()


class Command(Filter):
    def __init__(self, *commands):
        super().__init__(
            lambda event: (
                bool(event.text)
                and event.text.split()[0].split("@")[0] in {"/" + command for command in commands}
            )
        )


class CommandStart(Command):
    def __init__(self):
        super().__init__("start")


class Observer:
    def __init__(self):
        self.handlers, self.middleware = [], None

    def register(self, handler, *filters):
        self.handlers.append((handler, filters))

    def outer_middleware(self, middleware):
        self.middleware = middleware

    async def dispatch(self, event):
        for handler, filters in self.handlers:
            if all(predicate(event) for predicate in filters):

                async def call(event, data, handler=handler):
                    return await handler(event)

                return await self.middleware(call, event, {}) if self.middleware else await handler(event)


class Router:
    def __init__(self):
        self.message, self.callback_query = Observer(), Observer()


class Dispatcher:
    def include_router(self, router):
        self.router = router

    async def feed_update(self, bot, update):
        if update.get("message"):
            return await self.router.message.dispatch(Message(bot=bot, **update["message"]))
        if update.get("callback_query"):
            return await self.router.callback_query.dispatch(
                CallbackQuery(bot=bot, **update["callback_query"])
            )


class TelegramForbiddenError(Exception):
    pass


class TelegramRetryAfter(Exception):
    pass
