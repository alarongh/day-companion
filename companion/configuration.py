"""Versioned declarative configuration; uploaded AI output is data, never code."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal, get_args

from pydantic import Field, create_model, model_validator

from .domain import Garment, Lesson, Model, Place, Profile, Reminder, Rule

FORMAT = "day-companion/v2"
COLLECTIONS = {
    "places": (Place, "id"),
    "garments": (Garment, "name"),
    "rules": (Rule, "name"),
    "reminders": (Reminder, "id"),
}
RUNTIME = {
    "pause_until",
    "home_dates",
    "departed_dates",
    "source_checked",
    "source_until",
    "source",
    "mirea_id",
    "mirea_name",
}
EDITABLE = set(Profile.model_fields) - RUNTIME - set(COLLECTIONS)
# Partial fields; final Profile validation checks bounds after merging with current settings.
Settings = create_model(
    "Settings",
    __base__=Model,
    **{
        k: (
            (
                dict
                if k in {"campus_routes", "transfers", "campus_places", "lesson_notes"}
                else Profile.model_fields[k].annotation
            )
            | None,
            Field(
                default=None,
                **{"description": "Merge only supplied fields; maps merge by key; null deletes a map entry."},
            ),
        )
        for k in EDITABLE
        if k != "sleep"
    },
    sleep=(dict, Field(default_factory=dict)),
)


class Operations(Model):
    upsert: list[dict] = Field(default_factory=list, max_length=1000)
    remove: list[str] = Field(default_factory=list, max_length=1000)


class ScheduleChange(Model):
    mode: Literal["upsert", "replace"] = "upsert"
    lessons: list[Lesson] = Field(default_factory=list, max_length=2000)
    remove: list[str] = Field(default_factory=list, max_length=2000)


class DayChange(Model):
    day: date
    lesson_id: str
    change: dict | None = None


class Configuration(Model):
    format: Literal["day-companion/v2"]
    mode: Literal["setup", "patch"]
    base_revision: str
    profile: Settings = Field(default_factory=Settings)
    places: Operations = Field(default_factory=Operations)
    garments: Operations = Field(default_factory=Operations)
    rules: Operations = Field(default_factory=Operations)
    reminders: Operations = Field(default_factory=Operations)
    schedule: ScheduleChange | None = None
    personal: ScheduleChange | None = None
    overrides: list[DayChange] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def check(self):
        for key, (model, _) in COLLECTIONS.items():
            for value in getattr(self, key).upsert:
                model.model_validate(value)
        return self


def decode_config(text):
    text = text.strip().lstrip("\ufeff")
    if text.startswith("```"):
        text = "\n".join(text.splitlines()[1:-1])
    if not text.startswith("{"):
        return None
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    return Configuration.model_validate(data)


def snapshot(store, uid):
    p = store.profile(uid)
    profile = p.model_dump(mode="json")
    state = {
        "profile": {k: profile[k] for k in sorted(EDITABLE)},
        **{k: profile[k] for k in COLLECTIONS},
        "source": {"kind": p.source, "group": p.mirea_name, "id": p.mirea_id},
        "schedule": [x.model_dump(mode="json") for x in store.base_lessons(uid)]
        if p.source == "manual"
        else [],
        "personal": [x.model_dump(mode="json") for x in store.personal(uid)],
        "overrides": store.export(uid)["overrides"],
    }
    revision = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    return {"format": FORMAT, "base_revision": revision, **state}


def merge_map(old, new):
    result = dict(old)
    for k, v in new.items():
        if v is None:
            result.pop(k, None)
        elif isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = merge_map(result[k], v)
        else:
            result[k] = v
    return result


def apply_collection(old, op, model, key):
    rows = {getattr(x, key): x for x in old}
    for name in op.remove:
        if name not in rows:
            raise ValueError(f"Удаляемый элемент не найден: {name}")
        del rows[name]
    seen = set()
    for raw in op.upsert:
        # A supplied item is complete. Unmentioned items remain unchanged.
        value = model.model_validate(raw)
        ident = getattr(value, key)
        if ident in seen:
            raise ValueError(f"Повторный элемент: {ident}")
        seen.add(ident)
        rows[ident] = value
    return list(rows.values())


def prepare(store, uid, config):
    current = snapshot(store, uid)
    if config.base_revision != current["base_revision"]:
        raise ValueError(
            "Настройки изменились после выдачи промпта. Получи новый промпт /config и повтори нужную правку."
        )
    old = store.profile(uid)
    data = old.model_dump(mode="json")
    changes = config.profile.model_dump(mode="json", exclude_unset=True)
    for key, value in changes.items():
        if value is None and type(None) not in get_args(Profile.model_fields[key].annotation):
            raise ValueError(f"Поле {key} не может быть null")
        if key == "sleep" and any(v is None and k != "free_day_wake" for k, v in value.items()):
            raise ValueError("В настройках сна только free_day_wake может быть null")
        data[key] = merge_map(data[key], value) if isinstance(value, dict) else value
    for key, (model, ident) in COLLECTIONS.items():
        data[key] = [
            x.model_dump(mode="json")
            for x in apply_collection(getattr(old, key), getattr(config, key), model, ident)
        ]
    p = Profile.model_validate(data)
    lessons = store.base_lessons(uid)
    personal = store.personal(uid)
    for op, values, which in [
        (config.schedule, lessons, "schedule"),
        (config.personal, personal, "personal"),
    ]:
        if op is None:
            continue
        base = [] if op.mode == "replace" else values
        changed = apply_collection(
            base,
            Operations(upsert=[x.model_dump(mode="json") for x in op.lessons], remove=op.remove),
            Lesson,
            "id",
        )
        if which == "schedule":
            lessons = changed
            p.source = "manual"
            p.mirea_id = None
            p.mirea_name = ""
            p.source_checked = None
        else:
            personal = changed
    all_items = lessons + personal
    if len({x.id for x in all_items}) != len(all_items):
        raise ValueError("Идентификаторы расписания и личных событий должны отличаться")
    known = {x.id for x in p.places}
    if any(x.place_id and x.place_id not in known for x in all_items):
        raise ValueError("Занятие ссылается на неизвестную погодную точку")
    if (
        any(x.parity != "all" or x.weeks for x in all_items)
        or any(x.parity != "all" or x.weeks for x in p.reminders)
    ) and not p.semester_start:
        raise ValueError(
            "Для чётности/номеров недель нужен semester_start: понедельник первой учебной недели"
        )
    overrides = {(x["day"], x["lesson_id"]): x for x in current["overrides"]}
    by_id = {x.id: x for x in all_items}
    allowed = set(Lesson.model_fields) - {
        "id",
        "date",
        "weekday",
        "weeks",
        "parity",
        "valid_from",
        "valid_until",
    }
    for op in config.overrides:
        key = (op.day.isoformat(), op.lesson_id)
        if op.change is None:
            overrides.pop(key, None)
            continue
        if op.lesson_id not in by_id or set(op.change) - allowed - {"cancelled"}:
            raise ValueError("Неизвестное занятие или неподдерживаемое поле изменения дня")
        if "cancelled" in op.change and not isinstance(op.change["cancelled"], bool):
            raise ValueError("cancelled должно быть true/false")
        previous = overrides.get(key, {}).get("change", {})
        change = {**previous, **op.change}
        Lesson.model_validate(
            {**by_id[op.lesson_id].model_dump(), **{k: v for k, v in change.items() if k != "cancelled"}}
        )
        if change.get("place_id") and change["place_id"] not in known:
            raise ValueError("Изменение дня ссылается на неизвестную погодную точку")
        overrides[key] = {"day": key[0], "lesson_id": key[1], "change": change}
    result = (p, lessons, personal, list(overrides.values()))
    return result, difference(
        old,
        p,
        store.base_lessons(uid),
        lessons,
        store.personal(uid),
        personal,
        current["overrides"],
        result[3],
    )


LABELS = {
    "sleep": "Сон",
    "travel": "Дорога",
    "preparation": "Сборы",
    "buffer": "Запас",
    "places": "Точки погоды",
    "garments": "Верхняя одежда",
    "rules": "Правила одежды",
    "reminders": "Напоминания",
    "notifications": "Уведомления",
    "lesson_notes": "Заметки к занятиям",
    "campus_places": "Погода по корпусам",
    "home_place": "Точка дома",
    "destination_place": "Точка учёбы",
    "outdoor_before": "Улица возле дома",
    "outdoor_after": "Улица возле места учёбы",
}


def difference(old, new, base, changed, personal, new_personal, overrides, new_overrides):
    lines = []
    a, b = old.model_dump(mode="json"), new.model_dump(mode="json")
    for k in a:
        if a[k] == b[k]:
            continue
        if isinstance(b[k], list):
            before = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in a[k]}
            after = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in b[k]}
            lines.append(
                f"{LABELS.get(k, k)}: добавлено/изменено {len(after - before)}, удалено/заменено {len(before - after)}; всего {len(after)}"
            )
        else:
            lines.append(
                f"{LABELS.get(k, k)}: {json.dumps(a[k], ensure_ascii=False)} → {json.dumps(b[k], ensure_ascii=False)}"
            )
    for label, left, right in [("Расписание", base, changed), ("Личные события", personal, new_personal)]:
        if left != right:
            lines.append(f"{label}: {len(left)} → {len(right)} записей")
    if overrides != new_overrides:
        lines.append(f"Изменения отдельных дней: {len(overrides)} → {len(new_overrides)}")
    details = {
        "profile": {k: b[k] for k in b if a[k] != b[k]},
        "schedule": [x.model_dump(mode="json") for x in changed] if base != changed else None,
        "personal": [x.model_dump(mode="json") for x in new_personal] if personal != new_personal else None,
        "overrides": new_overrides if overrides != new_overrides else None,
    }
    return "\n".join(lines) or "Изменений нет.", details


def schema():
    result = Configuration.model_json_schema()
    # Make collection payload schemas explicit for external AIs and editors.
    for key, (model, _) in COLLECTIONS.items():
        definition = model.model_json_schema()
        result.setdefault("$defs", {}).update(definition.pop("$defs", {}))
        result["properties"][key] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "upsert": {"type": "array", "items": definition},
                "remove": {"type": "array", "items": {"type": "string"}},
            },
        }
    return result


def prompt(store, uid, patch=False):
    state = snapshot(store, uid)
    context = ""
    p = store.profile(uid)
    if p.source == "mirea":
        # Read-only context, not part of preference revision: refreshed lessons must not stale an AI patch.
        context = "\nРасписание источника (контекст, не заменять без просьбы):\n" + json.dumps(
            [x.model_dump(mode="json") for x in store.base_lessons(uid)][:150], ensure_ascii=False
        )
    return f'''Помоги настроить Telegram-бота Daily routine. Общайся со мной по-русски.
Режим: {"изменить только то, что я попрошу; не опрашивай заново и не меняй остальное" if patch else "сначала опроси меня простыми вопросами, затем сформируй настройки"}.
Выясни города и районы дома/учёбы/работы, длительность дороги, сборов, запас, путь от входа до аудитории, время на улице возле дома и точки назначения, задержку после последнего занятия. Можно несколько точек и campus_places для разных корпусов. Никогда не выдумывай координаты: оставь latitude/longitude null, я отмечу район на карте в боте. Не нужен точный домашний адрес.
Уточни мои вещи верхней одежды, пороги фактической/ощущаемой температуры, дождь, ветер, предпочтения и приоритеты. Правила могут зависеть от scenario. Средняя температура считается по минутам на улице отдельно для каждого часа; выбирается самый прохладный из этих часов. Не усредняй весь день, маскируя холод утром. hourly_temperature=coldest выбирает минимум прогнозных значений вместо среднего часа. outdoor_before/outdoor_after — минуты на улице, не вся дорога.
Сон: sleep.enabled, hours (моя цель сна), latency_minutes на засыпание, reminder_minutes до сна, wake_lead до сборов, free_day_wake для свободных дней. Подъём = первая пара - дорога - campus_walk - buffer - preparation - wake_lead. Отбой = подъём - hours - latency_minutes. Напоминание о сне переносится раньше начала тихих часов; утренняя сводка во время тихих часов откладывается до их конца. Для сводки точно при подъёме нужно согласовать тихие часы или отключить их равными quiet_start/quiet_end. fixed_briefing=null означает автоматическую сводку. Не используй allowed_lateness для сдвига выхода, это только заметка.
Расписание: weekday 0=понедельник..6=воскресенье ИЛИ date YYYY-MM-DD; start/end HH:MM; parity all/odd/even, weeks номера от 1; semester_start понедельник первой учебной недели, semester_end конец. Уточни подгруппу. Укажи стабильные id, title, room, campus, place_id, note, allowed_lateness. lesson_notes по точному названию или id дополняет расписание МИРЭА. МИРЭА подключается кнопкой /mirea в боте, не логином/паролем; существующий источник не заменять без просьбы.
Напоминания: time, wake, departure, arrival, last_lesson, bedtime, lesson_start (subject точное название/id либо все), home, condition. offset_minutes отрицательный = до, положительный = после. Для home/condition только >=0. home срабатывает ТОЛЬКО после кнопки «Я дома», никогда по расчётному возвращению. condition — после кнопки подтверждения названного условия; не обещай автоматическое определение геопозиции/задач. date, weekdays, parity, weeks ограничивают дни. enabled и expires_minutes задают активность и срок актуальности. notifications включает все уведомления, уточни согласие пользователя.
Профиль объединяется по указанным полям, вложенные словари по ключам; null удаляет ключ словаря (для необязательных полей означает пустое значение). Списки в profile заменяются целиком, не присылай их без нужды. В коллекциях upsert содержит ПОЛНЫЙ объект с прежним id/name, незатронутые объекты сохраняются; remove удаляет только перечисленное. schedule/personal mode=upsert изменяет по id; mode=replace заменяет весь соответствующий список только при явной просьбе. overrides задаёт разовые правки day+lesson_id+change, change=null удаляет разовую правку. Все неуказанные данные сохраняются. Не присылай runtime поля, токены и код.
После согласования выдай ОДИН JSON (можно блок ```json) строго по схеме, без неизвестных полей. Обязательные format="{FORMAT}", mode="{"patch" if patch else "setup"}", base_revision="{state["base_revision"]}". Значения schema defaults не нужно дублировать. Я загружу ответ файлом .json/.txt или текстом в бот и проверю изменения.
Текущее состояние:\n{json.dumps(state, ensure_ascii=False, indent=2)}{context}
JSON Schema:\n{json.dumps(schema(), ensure_ascii=False, indent=2)}'''
