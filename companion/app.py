from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

from .configuration import Configuration, decode_config, prepare, prompt, snapshot
from .domain import Garment, Lesson, Place, Profile, Rule, choose_clothing
from .importers import import_file, parse_text
from .inputs import input_error, temperature_range
from .journey import journey_times
from .providers import Providers
from .runtime import process_lock
from .service import agenda, briefing, day_items, import_warnings, sleep_report, weather_report
from .store import Store
from .telegram import (
    BaseMiddleware,
    Bot,
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    Command,
    CommandStart,
    Dispatcher,
    F,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    Router,
)
from .templates import SCHEDULE_CSV
from .worker import Worker

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
        [KeyboardButton(text="Погода"), KeyboardButton(text="Точки погоды")],
        [KeyboardButton(text="Настроить через ИИ"), KeyboardButton(text="Сон")],
        [KeyboardButton(text="Я дома"), KeyboardButton(text="Условия")],
        [KeyboardButton(text="Расписание"), KeyboardButton(text="Одежда")],
        [KeyboardButton(text="Сценарии"), KeyboardButton(text="Настройки")],
        [KeyboardButton(text="Подобрать одежду"), KeyboardButton(text="Уведомления")],
        [KeyboardButton(text="Режим")],
    ],
    resize_keyboard=True,
)

SLEEP_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Сон"), KeyboardButton(text="Сегодня"), KeyboardButton(text="Завтра")],
        [KeyboardButton(text="Настроить через ИИ"), KeyboardButton(text="Расписание сна")],
        [KeyboardButton(text="Уведомления"), KeyboardButton(text="Режим")],
    ],
    resize_keyboard=True,
)


def inline(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows
        ]
    )


def item_key(value):
    content = value if isinstance(value, str) else value.model_dump_json()
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def find_item(values, key, garment=False):
    for i, value in enumerate(values):
        if item_key(value.name if garment else value) == key:
            return i
    raise ValueError("Эта кнопка устарела: элемент уже удалён или изменён")


def day_buttons(day):
    return inline(
        [
            [("Уже вышел", f"left:{day}"), ("Сегодня дома", f"home:{day}")],
            [("Я дома", f"arrived:{day}")],
            [
                ("Холодно", f"feedback:{day}:cold"),
                ("Нормально", f"feedback:{day}:ok"),
                ("Жарко", f"feedback:{day}:hot"),
            ],
        ]
    )


SETTINGS = {
    "travel": ("Дорога до места занятий, мин.", int),
    "preparation": ("Сборы, мин.", int),
    "buffer": ("Запас до занятия, мин.", int),
    "campus_walk": ("От входа до аудитории, мин.", int),
    "lesson_lead": ("Напоминание перед парой, мин.", int),
    "departure_lead": ("Напомнить до выхода, мин. (0 = выключить)", int),
    "briefing_lead": ("Сводка за сколько минут до сборов", int),
    "short_break": ("Короткая перемена до скольких минут", int),
    "default_transfer": ("Переход между корпусами по умолчанию, мин.", int),
    "rain_threshold": ("Вероятность осадков для правила «дождь», %", int),
    "comfort_offset": ("Поправка комфорта: +1 = одеваться теплее, −1 = легче", float),
    "subgroup": ("Подгруппа: название или - для всех", str),
    "scenario": ("Название текущего сценария: учёба, работа, прогулка или своё", str),
    "semester_start": ("Понедельник первой учебной недели: ГГГГ-ММ-ДД или -", date.fromisoformat),
    "semester_end": ("Последний день семестра: ГГГГ-ММ-ДД или -", date.fromisoformat),
    "fixed_briefing": ("Сводка в фиксированное время ЧЧ:ММ или авто", str),
    "quiet_start": ("Начало тихих часов, ЧЧ:ММ", str),
    "quiet_end": ("Конец тихих часов, ЧЧ:ММ (одинаковые значения выключают режим)", str),
}


class PrivateAccess(BaseMiddleware):
    def __init__(self, allowed, store, locks):
        self.allowed, self.store, self.locks = allowed, store, locks

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        user = event.from_user
        if message is None or not hasattr(message, "chat"):
            return
        if message.chat.type != "private":
            if isinstance(event, Message):
                await message.answer("Настраивай личное расписание в личном чате с ботом.")
            return
        if not user or user.is_bot or self.allowed and user.id not in self.allowed:
            if isinstance(event, CallbackQuery):
                await event.answer("Этот бот пока доступен участникам тестирования.", show_alert=True)
            else:
                await message.answer("Этот бот пока доступен участникам тестирования.")
            return
        async with self.locks[user.id]:
            if not self.store.exists(user.id) and not (
                isinstance(event, Message) and event.text and event.text.split()[0].split("@")[0] == "/start"
            ):
                if isinstance(event, CallbackQuery):
                    await event.answer("Нажми /start для создания профиля", show_alert=True)
                else:
                    await message.answer("Нажми /start для создания личного профиля.")
                return
            try:
                return await handler(event, data)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                text = input_error(exc)
                if isinstance(event, CallbackQuery):
                    await event.answer("Не получилось. Подробности в чате.")
                await message.answer("Проверь ввод: " + text + "\nМожно повторить или нажать /cancel.")
            except httpx.HTTPError:
                if isinstance(event, CallbackQuery):
                    await event.answer("Сервис сейчас недоступен.")
                if self.store.session(user.id).get("action") in ("group", "grouppick"):
                    await message.answer(
                        "Сервис расписания МИРЭА временно недоступен. Можно загрузить расписание файлом или текстом; погода, одежда и настройки работают отдельно. Сохранённое расписание осталось на месте.",
                        reply_markup=inline([[("Загрузить расписание вручную", "upload")]]),
                    )
                else:
                    await message.answer(
                        "Сервис сейчас недоступен. Попробуй позже. Сохранённые настройки и расписание остались на месте."
                    )


class Application:
    def __init__(self, store, providers, locks, tesseract=""):
        self.store, self.providers, self.locks, self.tesseract = store, providers, locks, tesseract
        self.import_runner = asyncio.to_thread
        self.storage_description = "Настройки и расписание хранятся в базе на сервере бота."
        self.router = Router()
        self.router.message.register(self.start, CommandStart())
        self.router.message.register(self.help, Command("help"))
        self.router.message.register(self.privacy, Command("privacy"))
        self.router.message.register(self.cancel_flow, Command("cancel"))
        self.router.message.register(self.setup, Command("setup"))
        self.router.message.register(self.config_menu, Command("config"))
        self.router.message.register(self.weather, Command("weather"))
        self.router.message.register(self.sleep_menu, Command("sleep"))
        self.router.message.register(self.mode_menu, Command("mode"))
        self.router.message.register(self.arrived, Command("home"))
        self.router.message.register(self.conditions, Command("conditions"))
        self.router.message.register(self.location, F.location)
        self.router.message.register(self.today, Command("today"))
        self.router.message.register(self.tomorrow, Command("tomorrow"))
        self.router.message.register(self.schedule_menu, Command("schedule", "import"))
        self.router.message.register(self.group_prompt, Command("mirea"))
        self.router.message.register(self.settings, Command("settings"))
        self.router.message.register(self.garments, Command("wardrobe"))
        self.router.message.register(self.rules, Command("rules"))
        self.router.message.register(self.notifications, Command("notifications"))
        self.router.message.register(self.export, Command("export"))
        self.router.message.register(self.delete, Command("delete"))
        self.router.message.register(self.documents, F.document | F.photo)
        self.router.callback_query.register(self.callback)
        self.router.message.register(self.text, F.text)

    def uid(self, event):
        return event.from_user.id

    def menu(self, uid):
        return SLEEP_MENU if self.store.profile(uid).routine_mode == "sleep_only" else MENU

    async def mode_menu(self, m, uid=None):
        uid = self.uid(m) if uid is None else uid
        p = self.store.profile(uid)
        await m.answer(
            "Режим: "
            + ("только сон" if p.routine_mode == "sleep_only" else "полный")
            + ".\nВ режиме сна используются только статичное расписание сна, дорога, сборы и цель сна. "
            "Группа МИРЭА, погода и остальные напоминания не участвуют. "
            "Расписание и часы сна задай через «Настроить через ИИ»; поле profile.routine_mode = sleep_only.",
            reply_markup=inline([[("Только сон", "mode:sleep_only"), ("Полный режим", "mode:full")]]),
        )

    async def static_sleep_schedule(self, m):
        uid = self.uid(m)
        p = self.store.profile(uid)
        await self.say(
            m,
            "Статичное расписание сна:\n"
            + agenda(day_items(self.store, uid, self.local_now(uid).date()))
            + "\n\nЗагрузи расписание файлом или измени profile.sleep_schedule через /config. "
            "Для одинакового подъёма ежедневно задай sleep.free_day_wake. "
            f"Часовой пояс: {p.timezone}.",
            reply_markup=self.menu(uid),
        )

    def local_now(self, uid):
        return datetime.now(ZoneInfo(self.store.profile(uid).timezone))

    def flow(self, uid, **data):
        previous = self.store.session(uid)
        # Keep background change notification pending while switching forms.
        self.store.set_session(
            uid, {**({"source_changed": True} if previous.get("source_changed") else {}), **data}
        )

    async def say(self, message, text, **kwargs):
        for i in range(0, len(text), 3900):
            await message.answer(text[i : i + 3900], **(kwargs if i == 0 else {}))

    async def start(self, m):
        uid = self.uid(m)
        self.store.ensure(uid)
        await m.answer(
            "Я помогу выбрать верхнюю одежду, рассчитать выход, подъём и сон, напомнить об аудиториях и личных делах. У каждого здесь своё расписание и настройки.\n\nНачни с «Настроить через ИИ». Если нужен источник МИРЭА, подключи группу через /mirea.\n\n/ privacy — данные и приватность".replace(
                "/ privacy", "/privacy"
            ),
            reply_markup=self.menu(uid),
        )
        await self.config_menu(m)

    async def help(self, m):
        await m.answer(
            "/config — промпты для настройки и изменения через ИИ\n/weather — погода без расписания\n/sleep — расчёт сна\n/home — подтвердить возвращение домой\n/conditions — подтвердить своё условие"
        )
        await m.answer(
            "Основные действия — кнопками внизу.\n/setup — город и время дороги\n/schedule — источник и загрузка\n/wardrobe — твои вещи\n/rules — условия выбора одежды\n/settings — все настройки\n/notifications — включение, пауза\n/today /tomorrow — сводка\n/export — выгрузить свои данные\n/delete — удалить профиль и данные\n/cancel — выйти из заполнения формы\n/privacy — что хранит бот\n\nМожно отправить расписание файлом. Для разовой отмены или переноса открой «Расписание» → «Изменить день».",
            reply_markup=MENU,
        )

    async def privacy(self, m):
        await m.answer(self.storage_description)
        await m.answer(
            "Бот хранит твой Telegram ID, координаты выбранных районов, расписание, настройки дороги и сна, вещи, правила, напоминания, подтверждения условий и статусы доставки. Расписания других пользователей тебе недоступны.\n\nПогода: Open-Meteo получает выбранные координаты. При поиске города отправляется его название. При подключении МИРЭА сервис получает выбранную группу; Telegram ID ему не передаётся.\n\nБот не отправляет данные нейросетям автоматически. Промпт настройки содержит твои настройки и расписание; ты сам решаешь, передавать ли его выбранному ИИ. Можно отмечать центр района вместо адреса дома.\n\nФайлы обрабатываются на сервере бота. В Telegram отправляются сводки, файлы и напоминания. /export — получить данные. /delete — удалить данные из рабочей базы бота. Сообщения в Telegram и резервные копии сервера удаляются отдельно. Владелец сервера имеет технический доступ к базе."
        )

    async def cancel_flow(self, m):
        self.flow(self.uid(m))
        await m.answer(
            "Заполнение отменено. Сохранённые данные на месте.", reply_markup=self.menu(self.uid(m))
        )

    async def setup(self, m):
        self.flow(self.uid(m), action="city")
        await m.answer("Напиши город для прогноза погоды. Точный адрес дома не нужен.")

    async def weather_points(self, m, uid=None):
        uid = self.uid(m) if uid is None else uid
        p = self.store.profile(uid)
        rows = []
        for point in p.places:
            role = " · дом" if point.id == p.home_place else ""
            role += " · учёба/работа" if point.id == p.destination_place else ""
            rows.append(
                [
                    (
                        f"{point.label}{role} · {'✓' if point.latitude is not None else 'нужна точка'}"[:100],
                        "place:" + point.id,
                    )
                ]
            )
            rows.append([("Это дом", "placehome:" + point.id), ("Это учёба/работа", "placedest:" + point.id)])
        if len(p.places) < 12:
            rows.append([("Добавить район на карте", "place:add")])
        rows.append([("Погода сейчас", "weather")])
        await m.answer(
            "Погода определяется автоматически по отмеченным районам. Вводить градусы не нужно.\n"
            "Нажми район и отправь точку на карте: скрепка → Геопозиция. Точный адрес не обязателен.\n"
            + (
                "Районы пока не добавлены. Начни с кнопки ниже — ИИ и расписание для этого не нужны."
                if not p.places
                else "Отметь дом и место учёбы/работы; они могут находиться в разных городах."
            ),
            reply_markup=inline(rows),
        )

    async def weather(self, m, uid=None):
        uid = self.uid(m) if uid is None else uid
        p = self.store.profile(uid)
        if p.routine_mode == "sleep_only":
            await m.answer("В режиме только сна погода отключена. Выбрать полный режим: /mode.")
            return
        await self.say(
            m,
            await weather_report(self.providers, p, self.local_now(uid).date()),
            reply_markup=inline([[("Настроить районы", "cfg:places")]]),
        )

    async def config_menu(self, m):
        uid = self.uid(m)
        self.flow(uid)
        sleeping = self.store.profile(uid).routine_mode == "sleep_only"
        await m.answer(
            "Настройка через ИИ:\n1. Скачай нужный промпт и отправь его в свой чат с ИИ.\n2. Ответь на вопросы; получи JSON с настройками.\n3. Отправь JSON сюда текстом или файлом .json/.txt.\n4. Проверь изменения и нажми «Применить».\n\nДля правок всегда бери свежий промпт «Изменить настройки»: он содержит текущее состояние. Бот сам не обращается к ИИ и не требует ключа ИИ. В промпте могут быть твои расписание и координаты; передавай его выбранному ИИ по своему решению.",
            reply_markup=self.menu(uid),
        )
        await m.answer(
            "Выбери действие:",
            reply_markup=inline(
                [
                    [("Первичная настройка", "cfg:setup"), ("Изменить настройки", "cfg:patch")],
                    [("Точки погоды", "cfg:places"), ("Текущие данные", "cfg:state")],
                    [("Обычная настройка города", "city"), ("Группа МИРЭА", "group")],
                ]
                if not sleeping
                else [
                    [("Первичная настройка", "cfg:setup"), ("Изменить настройки", "cfg:patch")],
                    [("Текущие данные", "cfg:state")],
                ]
            ),
        )

    async def sleep_menu(self, m):
        uid = self.uid(m)
        texts = []
        for n in (0, 1):
            day = self.local_now(uid).date() + timedelta(days=n)
            texts.append(f"{day:%d.%m}:\n" + sleep_report(self.store, uid, day))
        await m.answer(
            "\n\n".join(texts)
            + "\n\nИзменить часы сна, сборы и свободные дни: /config → «Изменить настройки». "
            "Режим только сна: /mode.",
            reply_markup=self.menu(uid),
        )

    async def confirm_home(self, m, uid, day):
        now = self.local_now(uid)
        if day != now.date():
            journey = journey_times(day_items(self.store, uid, day), self.store.profile(uid))
            if (
                day != now.date() - timedelta(days=1)
                or journey is None
                or journey["home_estimate"].date() != now.date()
                or not journey["last_lesson"] <= now <= journey["home_estimate"] + timedelta(hours=12)
            ):
                raise ValueError("Подтверди возвращение кнопкой «Я дома» в нижнем меню")
        self.store.record_event(uid, day, "home", now)
        await m.answer(
            f"Возвращение подтверждено для поездки {day:%d.%m}. Напоминания после дома отсчитываются от первого нажатия для этой поездки.",
            reply_markup=MENU,
        )

    async def arrived(self, m):
        uid = self.uid(m)
        now = self.local_now(uid)
        p = self.store.profile(uid)
        day = now.date()
        previous = journey_times(day_items(self.store, uid, day - timedelta(days=1)), p)
        today = journey_times(day_items(self.store, uid, day), p)
        if (
            previous
            and previous["home_estimate"].date() == day
            and previous["last_lesson"] <= now <= previous["home_estimate"] + timedelta(hours=12)
            and (today is None or today["last_lesson"] > now)
        ):
            day -= timedelta(days=1)
        await self.confirm_home(m, uid, day)

    async def conditions(self, m):
        names = sorted(
            {
                r.condition
                for r in self.store.profile(self.uid(m)).reminders
                if r.enabled and r.trigger == "condition"
            }
        )
        await m.answer(
            "Нажми, когда условие выполнено. Подтверждение учитывается один раз за день."
            if names
            else "Своих условий пока нет. Добавь напоминание через /config, например «через 20 минут после тренировки».",
            reply_markup=inline([[(name, "condition:" + item_key(name))] for name in names])
            if names
            else MENU,
        )

    async def location(self, m):
        uid = self.uid(m)
        s = self.store.session(uid)
        if s.get("action") != "place_location":
            await m.answer("Сначала выбери точку: /config → «Точки погоды».")
            return
        if m.location.live_period:
            raise ValueError("Нужна обычная точка на карте, не трансляция геопозиции")
        p = self.store.profile(uid)
        point = next((x for x in p.places if x.id == s.get("place_id")), None)
        if point is None:
            raise ValueError("Точка уже изменена; открой список заново")
        point.latitude, point.longitude = m.location.latitude, m.location.longitude
        self.store.save_profile(uid, p)
        self.flow(uid)
        await m.answer(
            f"Район отмечен: {point.label}. Прогноз будет по этим координатам. Это прогноз погодной модели, а не измерение на конкретной улице.",
            reply_markup=MENU,
        )
        await self.weather(m, uid)

    async def stage_config(self, m, uid, config):
        _, (summary, details) = prepare(self.store, uid, config)
        nonce = secrets.token_hex(5)
        self.store.draft(
            uid,
            [],
            {"type": "configuration", "config": config.model_dump(mode="json", exclude_unset=True)},
            nonce,
        )
        self.flow(uid)
        await self.say(
            m,
            "Изменения перед сохранением:\n" + summary,
            reply_markup=inline([[("Применить", "cfgapply:" + nonce), ("Отменить", "cfgdiscard:" + nonce)]]),
        )
        await m.answer_document(
            BufferedInputFile(
                json.dumps(details, ensure_ascii=False, indent=2).encode(), filename="changes-preview.json"
            ),
            caption="Полные значения изменённых настроек и расписания для проверки.",
        )

    async def today(self, m):
        uid = self.uid(m)
        day = self.local_now(uid).date()
        await self.say(
            m,
            await briefing(self.store, self.providers, uid, day),
            reply_markup=self.menu(uid)
            if self.store.profile(uid).routine_mode == "sleep_only"
            else day_buttons(day),
        )

    async def tomorrow(self, m):
        uid = self.uid(m)
        day = self.local_now(uid).date() + timedelta(days=1)
        await self.say(
            m,
            await briefing(self.store, self.providers, uid, day),
            reply_markup=self.menu(uid)
            if self.store.profile(uid).routine_mode == "sleep_only"
            else day_buttons(day),
        )

    async def schedule_menu(self, m):
        uid = self.uid(m)
        p = self.store.profile(uid)
        if p.routine_mode == "sleep_only":
            await m.answer(
                f"Статичное расписание сна: {len(p.sleep_schedule)} записей. "
                "Загрузи файл вручную или настрой profile.sleep_schedule через /config.",
                reply_markup=inline(
                    [
                        [("Загрузить вручную", "upload"), ("Шаблон файла", "template")],
                        [("Изменить через ИИ", "cfg:patch")],
                    ]
                ),
            )
            return
        status = f"МИРЭА: {p.mirea_name}" if p.source == "mirea" else "Ручная загрузка"
        await m.answer(
            f"Источник: {status}. Сохранено занятий: {len(self.store.lessons(uid))}.\n\nМожно загрузить ICS, CSV, XLSX, JSON или текст. PDF и изображения распознаются локально, но сложную таблицу может понадобиться преобразовать в шаблон.\n\nНовое расписание сначала показывается для проверки.",
            reply_markup=inline(
                [
                    [("Группа МИРЭА", "group"), ("Загрузить вручную", "upload")],
                    [("Обновить из МИРЭА", "refresh"), ("Шаблон файла", "template")],
                    [("Изменить день", "editday"), ("Добавить событие", "addlesson")],
                ]
            ),
        )

    async def group_prompt(self, m):
        if self.store.profile(self.uid(m)).routine_mode == "sleep_only":
            await m.answer(
                "В режиме сна используется статичное расписание. Настрой его через /config или /schedule."
            )
            return
        self.flow(self.uid(m), action="group")
        await m.answer("Напиши название группы МИРЭА, например БСБО-11-23. Затем выбери точное совпадение.")

    async def settings(self, m):
        p = self.store.profile(self.uid(m))
        lines = [
            f"Город: {p.city or 'не выбран'} ({p.timezone})",
            f"Дорога {p.travel} мин., сборы {p.preparation}, запас {p.buffer}, до аудитории {p.campus_walk}.",
            f"Сводка: {p.fixed_briefing or 'перед сборами'}. Тихие часы: {p.quiet_start}–{p.quiet_end}.",
            f"Сценарий: {p.scenario}. Подгруппа: {p.subgroup or 'все'}.",
            f"Температура: {'по ощущениям' if p.temperature_mode == 'feels' else 'фактическая'}. Поправка комфорта {p.comfort_offset:+g}°.",
        ]
        buttons = [[("Город", "city"), ("Температура: переключить", "temp_mode")]]
        keys = list(SETTINGS)
        buttons += [
            [(SETTINGS[k][0].split(",")[0][:38], "set:" + k) for k in keys[i : i + 2]]
            for i in range(0, len(keys), 2)
        ]
        buttons += [[("Дорога до разных корпусов", "routes"), ("Переходы между корпусами", "transfers")]]
        await m.answer("\n".join(lines), reply_markup=inline(buttons))

    async def garments(self, m):
        p = self.store.profile(self.uid(m))
        lines = ["Твоя верхняя одежда:"]
        for g in p.garments:
            lines.append(
                f"• {g.name}: {g.minimum:+g}…{g.maximum:+g}°, дождь {'да' if g.waterproof else 'нет'}, ветер {'да' if g.windproof else 'нет'}. {'Доступна' if g.enabled else 'Выключена'}"
            )
        if not p.garments:
            lines.append("Пока пусто. Добавь вещи, которые у тебя действительно есть.")
        rows = [[("Добавить вещь", "garment:add")]]
        rows += [
            [
                (
                    f"{'Выключить' if g.enabled else 'Включить'} {g.name}",
                    f"garment:toggle:{item_key(g.name)}",
                ),
                ("Удалить", f"garment:delete:{item_key(g.name)}"),
            ]
            for i, g in enumerate(p.garments)
        ]
        await self.say(m, "\n".join(lines), reply_markup=inline(rows))

    async def rules(self, m):
        p = self.store.profile(self.uid(m))
        lines = [
            f"Сейчас сценарий: {p.scenario}.",
            "Если подходят несколько правил, выбирается больший приоритет. При равенстве — более узкий диапазон температуры.",
        ]
        for i, r in enumerate(p.rules):
            rain = {"any": "любые осадки", "yes": "дождь/осадки", "no": "сухо"}[r.rain]
            lines.append(
                f"{i + 1}. {r.name}: {r.minimum:+g}…{r.maximum:+g}°, {rain}, ветер от {r.wind_min:g} км/ч, сценарий {r.scenario}, приоритет {r.priority} → {r.garment}."
            )
        rows = [[("Создать правило", "rule:add"), ("Выбрать сценарий", "set:scenario")]]
        rows += [[(f"Удалить: {r.name}", f"rule:delete:{item_key(r)}")] for r in p.rules]
        await self.say(m, "\n\n".join(lines), reply_markup=inline(rows))

    async def notifications(self, m):
        p = self.store.profile(self.uid(m))
        status = "включены" if p.notifications else "выключены"
        await m.answer(
            f"Уведомления {status}. Пауза: {p.pause_until or 'нет'}.\n\nСводка, напоминание о выходе и следующая аудитория рассчитываются автоматически. Тихие часы задаются в настройках.",
            reply_markup=inline(
                [
                    [("Выключить" if p.notifications else "Включить", "notify:toggle")],
                    [("Пауза сегодня", "notify:pause"), ("Возобновить", "notify:resume")],
                    [
                        ("Сегодня дома", f"home:{self.local_now(self.uid(m)).date()}"),
                        ("Вернуть занятия сегодня", "home:undo"),
                    ],
                ]
            ),
        )

    async def export(self, m):
        content = json.dumps(self.store.export(self.uid(m)), ensure_ascii=False, indent=2).encode()
        await m.answer_document(BufferedInputFile(content, filename="my-companion-data.json"))

    async def delete(self, m):
        nonce = secrets.token_hex(5)
        self.flow(self.uid(m), action="delete", nonce=nonce)
        await m.answer(
            "Удалить твой профиль, расписание, вещи, правила и статусы уведомлений из базы бота?",
            reply_markup=inline([[("Удалить мои данные", "delete:" + nonce), ("Оставить", "abort")]]),
        )

    async def stage(self, m, uid, lessons, meta=None):
        p = self.store.profile(uid)
        nonce = secrets.token_hex(5)
        self.store.draft(uid, lessons, meta or {}, nonce)
        action_label = (
            "События добавятся к расписанию."
            if (meta or {}).get("operation") == "append"
            else "Это заменит основное расписание. Личные события сохранятся."
        )
        lines = [f"Найдено занятий: {len(lessons)}. {action_label}"]
        for lesson in sorted(lessons, key=lambda x: (str(x.date or x.weekday), x.start))[:18]:
            label = (
                str(lesson.date)
                if lesson.date
                else ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"][lesson.weekday]
            )
            lines.append(
                f"{label} {lesson.start}–{lesson.end} · {lesson.title} · {lesson.room or '?'} ({lesson.campus or '?'})"
                + (f" · {lesson.parity}, недели {lesson.weeks or 'все'}" if lesson.date is None else "")
                + (f" · подгр. {lesson.subgroup}" if lesson.subgroup else "")
            )
        if len(lessons) > 18:
            lines.append("Остальные занятия доступны кнопкой «Весь результат».")
        lines += import_warnings(lessons, p)
        await self.say(
            m,
            "\n".join(lines),
            reply_markup=inline(
                [
                    [("Сохранить расписание", "commit:" + nonce), ("Отменить", "discard:" + nonce)],
                    [("Весь результат", "preview:" + nonce)],
                ]
            ),
        )

    async def documents(self, m):
        uid = self.uid(m)
        p = self.store.profile(uid)
        file = m.document or m.photo[-1]
        if file.file_size and file.file_size > 10 * 1024 * 1024:
            await m.answer("Файл больше 10 МБ. Уменьши его или разбей на части.")
            return
        name = m.document.file_name if m.document else "schedule.jpg"
        stream = io.BytesIO()
        await m.bot.download(file, destination=stream)
        if Path(name or "").suffix.lower() in (".json", ".txt"):
            try:
                config = decode_config(stream.getvalue().decode("utf-8-sig"))
            except UnicodeDecodeError:
                config = None
            if config is not None:
                await self.stage_config(m, uid, config)
                return
        await m.answer("Читаю расписание. Покажу результат перед сохранением.")
        lessons = await self.import_runner(
            import_file,
            name or "schedule.txt",
            stream.getvalue(),
            p,
            self.local_now(uid).date(),
            self.tesseract,
        )
        self.flow(uid)
        await self.stage(m, uid, lessons)

    async def callback(self, c: CallbackQuery):
        uid, m, value = self.uid(c), c.message, c.data or ""
        p, s = self.store.profile(uid), self.store.session(uid)
        await c.answer()
        if value.startswith("mode:"):
            mode = value.split(":", 1)[1]
            if mode not in ("full", "sleep_only"):
                raise ValueError("Неизвестный режим")
            p.routine_mode = mode
            if mode == "sleep_only":
                p.sleep.enabled = True
            self.store.save_profile(uid, p)
            self.flow(uid)
            await m.answer(
                "Режим сохранён. Настройки: /config. Уведомления: /notifications.",
                reply_markup=self.menu(uid),
            )
        elif value.startswith("cfg:"):
            kind = value.split(":")[1]
            if kind in ("setup", "patch"):
                text = prompt(self.store, uid, patch=kind == "patch")
                await m.answer_document(
                    BufferedInputFile(text.encode(), filename=f"ai-{kind}-prompt.txt"),
                    caption="Отправь содержимое файла в свой чат с ИИ. Готовый JSON верни сюда.",
                )
            elif kind == "state":
                await m.answer_document(
                    BufferedInputFile(
                        json.dumps(snapshot(self.store, uid), ensure_ascii=False, indent=2).encode(),
                        filename="current-settings.json",
                    )
                )
            elif kind == "places":
                await self.weather_points(m, uid)
        elif value == "weather":
            await self.weather(m, uid)
        elif value == "place:add":
            if len(p.places) >= 12:
                raise ValueError("Можно добавить до 12 районов.")
            self.flow(uid, action="place_name")
            await m.answer("Как назвать район? Например: Дом, район станции или Работа, центр города.")
        elif value.startswith(("placehome:", "placedest:")):
            role, point_id = value.split(":", 1)
            if not any(point.id == point_id for point in p.places):
                raise ValueError("Точка уже удалена. Открой список районов заново.")
            if role == "placehome":
                p.home_place = point_id
            else:
                p.destination_place = point_id
            self.store.save_profile(uid, p)
            await self.weather_points(m, uid)
        elif value.startswith("place:"):
            point = next((x for x in p.places if x.id == value.split(":")[1]), None)
            if point is None:
                raise ValueError("Точка не найдена")
            self.flow(uid, action="place_location", place_id=point.id)
            await m.answer(
                f"Прикрепи точку района для «{point.label}»: скрепка → Геопозиция → выбери район на карте.\n"
                "Если сейчас находишься в этом районе, можно нажать кнопку ниже. Трансляция геопозиции не нужна.",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="Отправить мою геопозицию", request_location=True)],
                        [KeyboardButton(text="/cancel")],
                    ],
                    resize_keyboard=True,
                ),
            )
        elif value.startswith(("cfgapply:", "cfgdiscard:")):
            action, nonce = value.split(":")
            draft = self.store.get_draft(uid, nonce)
            if not draft or draft[1].get("type") != "configuration":
                raise ValueError("Этот результат уже применён или отменён")
            if action == "cfgdiscard":
                self.store.clear_draft(uid)
                await m.answer("Изменения отменены.")
            else:
                config = Configuration.model_validate(draft[1]["config"])
                result, _ = prepare(self.store, uid, config)
                self.store.apply_config(uid, *result)
                missing = [x.label for x in result[0].places if x.latitude is None]
                await m.answer(
                    "Настройки сохранены. Расписание, выход, сон и напоминания пересчитаны."
                    + (
                        "\nОтметь районы через /config → «Точки погоды»: " + ", ".join(missing)
                        if missing and result[0].routine_mode == "full"
                        else ""
                    )
                    + (
                        "\nУведомления включены."
                        if result[0].notifications
                        else "\nУведомления пока выключены: /notifications."
                    ),
                    reply_markup=self.menu(uid),
                )
        elif value.startswith("arrived:"):
            await self.confirm_home(m, uid, date.fromisoformat(value.split(":")[1]))
        elif value.startswith("condition:"):
            key = value.split(":")[1]
            name = next(
                (
                    r.condition
                    for r in p.reminders
                    if r.enabled and r.trigger == "condition" and item_key(r.condition) == key
                ),
                None,
            )
            if name is None:
                raise ValueError("Условие уже удалено или изменено")
            now = self.local_now(uid)
            self.store.record_event(uid, now.date(), "condition:" + name, now)
            await m.answer(
                f"Условие подтверждено: {name}. Напоминания отсчитываются от первого подтверждения за сегодня."
            )
        elif value == "abort":
            self.flow(uid)
            await m.answer("Отменено.")
        elif value.startswith("delete:"):
            if s.get("action") != "delete" or value.split(":")[1] != s.get("nonce"):
                raise ValueError("Кнопка устарела. Открой /delete заново.")
            self.store.delete(uid)
            await m.answer("Твои данные удалены из рабочей базы. /start создаст новый пустой профиль.")
        elif value in ("city", "group", "upload"):
            if value == "city":
                self.flow(uid, action="city")
                await m.answer("Напиши город.")
            elif value == "group":
                self.flow(uid, action="group")
                await m.answer("Напиши название группы МИРЭА.")
            else:
                self.flow(uid, action="import")
                await m.answer(
                    "Пришли файл или текст. Пример строки:\nПн | 09:00-10:30 | Математика | Б-312 | В-78 | все\n\nДля замены нескольких дней загрузи полный файл. Разовые изменения — через «Изменить день»."
                )
        elif value.startswith(("citypick:", "grouppick:")):
            kind, nonce, index = value.split(":")
            if s.get("nonce") != nonce:
                raise ValueError("Выбор устарел, начни поиск заново")
            chosen = s["choices"][int(index)]
            if kind == "citypick":
                p.city = chosen["name"]
                p.latitude, p.longitude = chosen["latitude"], chosen["longitude"]
                p.timezone = chosen["timezone"]
                self.store.save_profile(uid, p)
                self.flow(uid, action="timings")
                await m.answer(
                    f"Выбран город {p.city}.\nТеперь напиши четыре числа через пробел: дорога, сборы, запас до пары, путь от входа до аудитории (в минутах).\nНапример: 45 40 10 10"
                )
            else:
                first, last = self.local_now(uid).date(), self.local_now(uid).date() + timedelta(days=120)
                lessons = await self.providers.mirea(chosen["id"], p, first, last)
                self.flow(uid)
                await self.stage(
                    m,
                    uid,
                    lessons,
                    {
                        "source": "mirea",
                        "mirea_id": chosen["id"],
                        "mirea_name": chosen["targetTitle"],
                        "source_checked": self.local_now(uid).isoformat(),
                        "source_until": str(last),
                    },
                )
        elif value.startswith(("commit:", "discard:", "preview:")):
            action, nonce = value.split(":")
            draft = self.store.get_draft(uid, nonce)
            if not draft:
                raise ValueError("Этот результат уже сохранён или отменён")
            lessons, meta = draft
            if action == "discard":
                self.store.clear_draft(uid)
                await m.answer("Загрузка отменена. Текущее расписание осталось.")
            elif action == "preview":
                await m.answer_document(
                    BufferedInputFile(
                        json.dumps(
                            [x.model_dump(mode="json") for x in lessons], ensure_ascii=False, indent=2
                        ).encode(),
                        filename="schedule-preview.json",
                    )
                )
            else:
                if any(x.weeks or x.parity != "all" for x in lessons) and not (
                    p.sleep.week_start if p.routine_mode == "sleep_only" else p.semester_start
                ):
                    raise ValueError(
                        "Сначала задай понедельник первой учебной недели в настройках. Результат загрузки сохранён, затем нажми эту кнопку ещё раз."
                    )
                if meta.get("operation") == "append":
                    if p.routine_mode == "sleep_only":
                        raise ValueError(
                            "В режиме сна загрузи статичное расписание без добавления личных событий"
                        )
                    self.store.add_personal(uid, lessons)
                    self.store.clear_draft(uid)
                    await m.answer("Личные события добавлены. Обновление группы их сохранит.")
                    return
                if p.routine_mode == "sleep_only":
                    if meta.get("source") == "mirea":
                        raise ValueError("Для режима сна нужно статичное расписание, а не источник МИРЭА")
                    p.sleep_schedule = lessons
                    self.store.save_profile(uid, p)
                    self.store.clear_draft(uid)
                    await m.answer(
                        "Статичное расписание сна сохранено. Время сна пересчитано.",
                        reply_markup=self.menu(uid),
                    )
                    return
                updates = meta or {
                    "source": "manual",
                    "mirea_id": None,
                    "mirea_name": "",
                    "source_checked": None,
                    "source_until": None,
                }
                p = Profile.model_validate({**p.model_dump(), **updates})
                self.store.replace(uid, lessons)
                self.store.save_profile(uid, p)
                self.store.clear_draft(uid)
                await m.answer(
                    "Расписание сохранено. Напоминания пересчитаны. Проверь «Сегодня» и «Завтра».",
                    reply_markup=MENU,
                )
        elif value == "refresh":
            if p.routine_mode == "sleep_only":
                raise ValueError("Статичное расписание сна не обновляется из МИРЭА")
            if p.source != "mirea" or not p.mirea_id:
                raise ValueError("Сначала выбери группу МИРЭА")
            first, last = self.local_now(uid).date(), self.local_now(uid).date() + timedelta(days=120)
            lessons = await self.providers.mirea(p.mirea_id, p, first, last)
            await self.stage(
                m,
                uid,
                lessons,
                {
                    "source": "mirea",
                    "mirea_id": p.mirea_id,
                    "mirea_name": p.mirea_name,
                    "source_checked": self.local_now(uid).isoformat(),
                    "source_until": str(last),
                },
            )
        elif value == "template":
            await m.answer_document(
                BufferedInputFile(SCHEDULE_CSV, filename="schedule-template.csv"),
                caption="Замените пример своими занятиями. Дни недели: Пн…Вс. Даты: ГГГГ-ММ-ДД. Недели: все / чет / нечет.",
            )
        elif value.startswith("set:"):
            key = value.split(":", 1)[1]
            if key not in SETTINGS:
                raise ValueError("Неизвестная настройка")
            self.flow(uid, action="setting", key=key)
            await m.answer(SETTINGS[key][0] + f". Сейчас: {getattr(p, key)}")
        elif value == "temp_mode":
            p.temperature_mode = "actual" if p.temperature_mode == "feels" else "feels"
            self.store.save_profile(uid, p)
            await m.answer(
                "Теперь температура учитывается "
                + ("по ощущениям." if p.temperature_mode == "feels" else "по термометру.")
            )
        elif value in ("routes", "transfers"):
            self.flow(uid, action=value)
            await m.answer(
                "Введи по одной строке: корпус=минуты. Например:\nВ-78=45\nС-20=70\n\nЭто заменит список. - очищает его."
                if value == "routes"
                else "Введи по одной строке: откуда>куда=минуты. Например:\nВ-78>С-20=40\nС-20>В-78=45\n\nЭто заменит список. - очищает его."
            )
        elif value.startswith("garment:"):
            parts = value.split(":")
            if parts[1] == "add":
                self.flow(uid, action="garment", step=0, values={})
                await m.answer("Как называется вещь? Например: чёрная непромокаемая куртка.")
            else:
                index = find_item(p.garments, parts[2], garment=True)
                if parts[1] == "toggle":
                    p.garments[index].enabled = not p.garments[index].enabled
                elif parts[1] == "delete":
                    removed = p.garments.pop(index)
                    p.rules = [r for r in p.rules if r.garment != removed.name]
                self.store.save_profile(uid, p)
                await self.garments_for(m, uid)
        elif value.startswith("rule:"):
            parts = value.split(":")
            if parts[1] == "add":
                if not p.garments:
                    raise ValueError("Сначала добавь свою одежду")
                nonce = secrets.token_hex(4)
                self.flow(uid, action="rule", step=-1, values={}, nonce=nonce)
                await m.answer(
                    "Для какой вещи создаём правило?",
                    reply_markup=inline(
                        [[(g.name, f"rulepick:{nonce}:{item_key(g.name)}")] for g in p.garments]
                    ),
                )
            else:
                p.rules.pop(find_item(p.rules, parts[2]))
                self.store.save_profile(uid, p)
                await m.answer("Правило удалено.")
        elif value.startswith("rulepick:"):
            _, nonce, index = value.split(":")
            if s.get("action") != "rule" or s.get("nonce") != nonce:
                raise ValueError("Кнопка устарела")
            self.flow(
                uid,
                action="rule",
                step=0,
                values={"garment": p.garments[find_item(p.garments, index, garment=True)].name},
            )
            await m.answer("Название правила. Например: дождь перед учёбой.")
        elif value.startswith("notify:"):
            action = value.split(":")[1]
            if action == "toggle":
                if p.routine_mode == "full" and not p.notifications and not p.city and not p.places:
                    raise ValueError("Сначала настрой город и часовой пояс через /setup")
                p.notifications = not p.notifications
            elif action == "pause":
                p.pause_until = self.local_now(uid).date()
            elif action == "resume":
                p.notifications, p.pause_until = True, None
                if p.routine_mode == "full" and not p.city and not p.places:
                    raise ValueError("Сначала настрой город через /setup")
            self.store.save_profile(uid, p)
            await m.answer(
                "Уведомления включены."
                if p.notifications and not p.pause_until
                else "Настройка уведомлений сохранена."
            )
        elif value.startswith(("home:", "left:")):
            action, raw = value.split(":")
            if raw == "undo":
                p.home_dates = [d for d in p.home_dates if d != self.local_now(uid).date()]
            else:
                day = date.fromisoformat(raw)
                if abs((day - self.local_now(uid).date()).days) > 1:
                    raise ValueError("Кнопка относится к старому дню")
                key = "home_dates" if action == "home" else "departed_dates"
                setattr(
                    p,
                    key,
                    sorted(
                        set(
                            [
                                d
                                for d in getattr(p, key)
                                if d >= self.local_now(uid).date() - timedelta(days=7)
                            ]
                            + [day]
                        )
                    ),
                )
            self.store.save_profile(uid, p)
            await m.answer(
                "Занятия на сегодня возвращены."
                if raw == "undo"
                else "Принято: напоминания о выходе отменены."
                if action == "left"
                else "Режим «Дома»: уведомления об этом учебном дне отменены."
            )
        elif value.startswith("feedback:"):
            _, raw, feedback = value.split(":")
            if date.fromisoformat(raw) != self.local_now(uid).date():
                raise ValueError("Отзыв можно оставить для сегодняшнего дня")
            if not self.store.claim(uid, f"feedback:{raw}"):
                await m.answer("Оценка за сегодня уже сохранена.")
                return
            delta = {"cold": 1, "ok": 0, "hot": -1}[feedback]
            p.comfort_offset = max(-10, min(10, p.comfort_offset + delta))
            self.store.save_profile(uid, p)
            self.store.sent(uid, f"feedback:{raw}")
            await m.answer(
                f"Учёл. Поправка комфорта: {p.comfort_offset:+g}°. Её можно изменить в настройках."
            )
        elif value in ("editday", "addlesson"):
            self.flow(uid, action=value)
            await m.answer(
                "Напиши дату ГГГГ-ММ-ДД, которую нужно изменить."
                if value == "editday"
                else "Пришли событие строкой:\n2026-10-15 | 18:00-19:00 | Тренировка | Зал | Спортцентр\n\nОно добавится к текущему расписанию и сохранится при обновлении группы МИРЭА."
            )
        elif value.startswith("editpick:"):
            _, nonce, lesson_id = value.split(":")
            if s.get("action") != "editpick" or s.get("nonce") != nonce or lesson_id not in s.get("ids", []):
                raise ValueError("Выбор устарел")
            self.flow(uid, action="editlesson", day=s["day"], lesson_id=lesson_id)
            await m.answer(
                "Напиши «отмена», «вернуть» или новое время, аудиторию и корпус:\n10:40-12:10 | Б-214 | В-78\n\nИзменение действует только на выбранную дату."
            )
        else:
            raise ValueError("Кнопка устарела. Открой нужный раздел заново.")

    async def garments_for(self, m, uid):
        await m.answer("Гардероб обновлён. Открой «Одежда», чтобы посмотреть список.")

    async def text(self, m: Message):
        uid, text = self.uid(m), m.text.strip()
        config = decode_config(text)
        if config is not None:
            await self.stage_config(m, uid, config)
            return
        navigation = {
            "Настроить через ИИ": self.config_menu,
            "Погода": self.weather,
            "Точки погоды": self.weather_points,
            "Сон": self.sleep_menu,
            "Режим": self.mode_menu,
            "Расписание сна": self.static_sleep_schedule,
            "Я дома": self.arrived,
            "Условия": self.conditions,
            "Сегодня": self.today,
            "Завтра": self.tomorrow,
            "Расписание": self.schedule_menu,
            "Одежда": self.garments,
            "Сценарии": self.rules,
            "Настройки": self.settings,
            "Уведомления": self.notifications,
        }
        if text in navigation:
            self.flow(uid)
            await navigation[text](m)
            return
        if text == "Подобрать одежду":
            self.flow(uid, action="outfit")
            await m.answer(
                "Напиши сценарий, время начала и конца прогулки/дороги:\nпрогулка | 18:00 | 20:00\n\nПрогноз на сегодня, расписание не меняется."
            )
            return
        p, s = self.store.profile(uid), self.store.session(uid)
        action = s.get("action")
        if action == "place_name":
            if len(p.places) >= 12 or not 1 <= len(text) <= 100:
                raise ValueError("Название района: от 1 до 100 символов. Всего можно до 12 районов.")
            point = Place(id="p" + secrets.token_hex(4), label=text)
            p.places.append(point)
            if not p.home_place:
                p.home_place = point.id
            if not p.destination_place:
                p.destination_place = point.id
            self.store.save_profile(uid, p)
            self.flow(uid, action="place_location", place_id=point.id)
            await m.answer("Теперь прикрепи точку этого района: скрепка → Геопозиция → выбери на карте.")
        elif action in ("city", "group"):
            choices = await (self.providers.cities(text) if action == "city" else self.providers.groups(text))
            if not choices:
                await m.answer("Совпадений не нашёл. Уточни название.")
                return
            nonce = secrets.token_hex(4)
            self.flow(uid, action=action + "pick", nonce=nonce, choices=choices)
            rows = []
            for i, x in enumerate(choices):
                label = (
                    f"{x['name']}, {x.get('admin1', '')}, {x.get('country', '')}"
                    if action == "city"
                    else x["targetTitle"]
                )
                rows.append([(label[:100], f"{action}pick:{nonce}:{i}")])
            await m.answer("Выбери нужное:", reply_markup=inline(rows))
        elif action == "timings":
            values = [int(x) for x in text.replace(",", " ").split()]
            if len(values) != 4:
                raise ValueError("Нужно четыре числа: дорога, сборы, запас, путь до аудитории")
            p = Profile.model_validate(
                {
                    **p.model_dump(),
                    **dict(zip(("travel", "preparation", "buffer", "campus_walk"), values, strict=True)),
                }
            )
            self.store.save_profile(uid, p)
            self.flow(uid)
            await m.answer(
                "Город и дорога настроены. Теперь выбери расписание и добавь одежду. Когда всё проверишь, включи уведомления.",
                reply_markup=MENU,
            )
        elif action == "setting":
            key = s["key"]
            if key not in SETTINGS:
                raise ValueError("Неизвестная настройка")
            value = (
                None
                if text in ("-", "авто") and key in ("fixed_briefing", "semester_start", "semester_end")
                else ""
                if text == "-" and key == "subgroup"
                else SETTINGS[key][1](text)
            )
            p = Profile.model_validate({**p.model_dump(), key: value})
            self.store.save_profile(uid, p)
            self.flow(uid)
            await m.answer("Настройка сохранена. Расчёты обновлены.")
        elif action in ("routes", "transfers"):
            values = (
                {}
                if text == "-"
                else {
                    k.strip(): int(v.strip()) for k, v in (line.rsplit("=", 1) for line in text.splitlines())
                }
            )
            p = Profile.model_validate(
                {**p.model_dump(), "campus_routes" if action == "routes" else "transfers": values}
            )
            self.store.save_profile(uid, p)
            self.flow(uid)
            await m.answer("Время маршрутов сохранено.")
        elif action == "import":
            lessons = parse_text(text)
            self.flow(uid)
            await self.stage(m, uid, lessons)
        elif action == "garment":
            step, values = s["step"], s["values"]
            if step == 0:
                if any(g.name == text for g in p.garments):
                    raise ValueError("Вещь с таким названием уже есть")
                values["name"] = text
                prompt = "При какой температуре на улице комфортно в этой вещи? Укажи диапазон в °C: два числа через пробел, например -5 15. Это свойства вещи, а не ввод текущей погоды — прогноз бот получает сам."
            elif step == 1:
                values["minimum"], values["maximum"] = temperature_range(text)
                Garment.model_validate(values)
                prompt = "Защищает от дождя? Напиши да или нет."
            elif step in (2, 3):
                if text.lower() not in ("да", "нет"):
                    raise ValueError("Ответь да или нет")
                values["waterproof" if step == 2 else "windproof"] = text.lower() == "да"
                prompt = "Защищает от ветра? Напиши да или нет."
                if step == 3:
                    p.garments.append(Garment.model_validate(values))
                    self.store.save_profile(uid, p)
                    self.flow(uid)
                    await m.answer(
                        "Вещь добавлена. Теперь можно создать для неё сценарии или использовать подбор по комфортной температуре."
                    )
                    return
            self.flow(uid, action="garment", step=step + 1, values=values)
            await m.answer(prompt)
        elif action == "rule":
            step, values = s["step"], s["values"]
            prompts = [
                "Диапазон температуры, включительно: два числа, например -10 15. Учитывается выбранная температура и твоя поправка комфорта.",
                "Осадки: дождь / сухо / любые.",
                "Минимальная скорость ветра, км/ч. 0 — любое значение.",
                "Для какого сценария? Например учёба, работа, прогулка. * — для всех.",
                "Приоритет 0–1000. Чем выше, тем важнее правило. Например 100 для дождя, 50 для обычной температуры.",
            ]
            if step == 0:
                values["name"] = text
            elif step == 1:
                values["minimum"], values["maximum"] = temperature_range(text)
            elif step == 2:
                if text.lower() not in ("дождь", "сухо", "любые"):
                    raise ValueError("Выбери осадки: дождь, сухо или любые.")
                values["rain"] = {"дождь": "yes", "сухо": "no", "любые": "any"}[text.lower()]
            elif step == 3:
                values["wind_min"] = float(text.replace(",", "."))
            elif step == 4:
                values["scenario"] = text
            elif step == 5:
                values["priority"] = int(text)
                rule = Rule.model_validate(values)
                if rule.garment not in [g.name for g in p.garments]:
                    raise ValueError("Вещь удалена. Создай правило заново")
                p.rules.append(rule)
                self.store.save_profile(uid, p)
                self.flow(uid)
                await m.answer("Правило сохранено. Подбор использует его при совпадении условий.")
                return
            else:
                raise ValueError("Сначала выбери вещь кнопкой")
            # Validate partially filled ranges before advancing the form.
            Rule.model_validate({"name": values.get("name", "правило"), **values})
            self.flow(uid, action="rule", step=step + 1, values=values)
            await m.answer(prompts[step])
        elif action == "outfit":
            scenario, first, last = [x.strip() for x in text.split("|")]
            from .domain import clock

            day = self.local_now(uid).date()
            start = datetime.combine(day, clock(first), ZoneInfo(p.timezone))
            end = datetime.combine(day, clock(last), ZoneInfo(p.timezone))
            if end == start:
                raise ValueError("Начало и конец совпадают. Укажи разные часы")
            if end < start:
                end += timedelta(days=1)
            custom = Profile.model_validate({**p.model_dump(), "scenario": scenario})
            if p.places:
                point = next((x for x in p.places if x.id == (p.home_place or p.destination_place)), None)
                if point is None or point.latitude is None:
                    raise ValueError("Отметь домашний район: /config → «Точки погоды»")
                custom = custom.model_copy(update={"latitude": point.latitude, "longitude": point.longitude})
            hours = await self.providers.weather(custom, [(start, end)])
            await m.answer(
                f"Сценарий: {scenario}. {start:%d.%m %H:%M}–{end:%d.%m %H:%M}.\n"
                + choose_clothing(custom, hours),
                reply_markup=day_buttons(day),
            )
            self.flow(uid)
        elif action == "editday":
            day = date.fromisoformat(text)
            items = day_items(self.store, uid, day)
            # Include cancelled lessons so that they can be restored.
            from .domain import occurrences

            original = occurrences(self.store.lessons(uid), p, day)
            if not original:
                raise ValueError("На эту дату нет занятий")
            nonce = secrets.token_hex(4)
            self.flow(uid, action="editpick", nonce=nonce, day=str(day), ids=[x.lesson.id for x in original])
            await self.say(
                m,
                agenda(items),
                reply_markup=inline(
                    [
                        [(f"{x.start:%H:%M} {x.lesson.title}"[:90], f"editpick:{nonce}:{x.lesson.id}")]
                        for x in original
                    ]
                ),
            )
        elif action == "editlesson":
            if text.lower() == "отмена":
                change = {"cancelled": True}
            elif text.lower() == "вернуть":
                change = {}
            else:
                timing, room, campus = [x.strip() for x in text.split("|")]
                first, last = timing.split("-")
                original = next(x for x in self.store.lessons(uid) if x.id == s["lesson_id"])
                change = {"start": first.strip(), "end": last.strip(), "room": room, "campus": campus}
                Lesson.model_validate({**original.model_dump(), **change})
            self.store.override(uid, s["day"], s["lesson_id"], change)
            self.flow(uid)
            await m.answer("Разовое изменение сохранено. Время выхода и напоминания пересчитаны.")
        elif action == "addlesson":
            added = parse_text(text)
            await self.stage(m, uid, added, {"operation": "append"})
            self.flow(uid)
        else:
            await m.answer(
                "Выбери действие кнопкой внизу или нажми /help. Чтобы загрузить расписание текстом, открой «Расписание» → «Загрузить вручную».",
                reply_markup=MENU,
            )


async def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Заполни BOT_TOKEN в .env. Пример настроек — .env.example. Для проверки без токена: python -m companion.demo"
        )
    allowed = {int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
    store = Store(os.getenv("DB_PATH", "data/bot.sqlite3"))
    locks = defaultdict(asyncio.Lock)
    async with (
        httpx.AsyncClient(timeout=25, headers={"User-Agent": "DayCompanion/0.1"}) as client,
        Bot(token) as bot,
    ):
        providers = Providers(client)
        app = Application(store, providers, locks, os.getenv("TESSERACT_CMD", ""))
        dp = Dispatcher()
        guard = PrivateAccess(allowed, store, locks)
        app.router.message.outer_middleware(guard)
        app.router.callback_query.outer_middleware(guard)
        dp.include_router(app.router)
        await bot.set_my_commands(
            [
                BotCommand(command=c, description=d)
                for c, d in [
                    ("start", "Начать"),
                    ("config", "Настройка и изменения через ИИ"),
                    ("sleep", "Сон и подъём"),
                    ("home", "Я вернулся домой"),
                    ("conditions", "Подтвердить условие"),
                    ("setup", "Город и дорога"),
                    ("today", "Сегодня"),
                    ("tomorrow", "Завтра"),
                    ("schedule", "Расписание"),
                    ("wardrobe", "Одежда"),
                    ("rules", "Сценарии"),
                    ("settings", "Настройки"),
                    ("notifications", "Уведомления"),
                    ("cancel", "Отменить ввод"),
                    ("export", "Выгрузить данные"),
                    ("delete", "Удалить мои данные"),
                    ("privacy", "Приватность"),
                    ("help", "Помощь"),
                ]
            ]
        )
        # Detect an existing webhook instead of silently taking over another deployment.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise SystemExit(
                "У бота установлен webhook. Перед запуском polling отключи прежний экземпляр и удали webhook через Bot API."
            )
        worker = Worker(store, providers, bot, locks, day_buttons)
        task = asyncio.create_task(worker.run())
        try:
            await dp.start_polling(
                bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False
            )
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    load_dotenv()
    with process_lock(Path(os.getenv("DB_PATH", "data/bot.sqlite3")).with_suffix(".lock")):
        asyncio.run(main())
