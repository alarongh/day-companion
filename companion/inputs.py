"""User-facing input validation without Python internals or echoed private values."""

import math

from pydantic import ValidationError


def temperature_range(text):
    parts = text.replace("−", "-").replace(",", ".").split()
    if len(parts) != 2:
        raise ValueError("Нужно два числа в °C: нижняя и верхняя граница. Например: -5 15.")
    try:
        low, high = map(float, parts)
    except ValueError:
        raise ValueError("Температуру запиши двумя числами в °C, например: -5 15.") from None
    if not all(math.isfinite(value) and -60 <= value <= 60 for value in (low, high)):
        raise ValueError("Каждая температура должна быть от -60 до +60 °C. Например: 5 15.")
    if low > high:
        raise ValueError("Сначала укажи меньшую температуру, затем большую. Например: 5 15.")
    return low, high


def input_error(exc):
    if isinstance(exc, ValidationError):
        labels = {
            "minimum": "Нижняя температура",
            "maximum": "Верхняя температура",
            "name": "Название",
            "priority": "Приоритет",
            "wind_min": "Скорость ветра",
            "travel": "Время дороги",
            "preparation": "Время сборов",
            "hours": "Часы сна",
        }
        lines = []
        for error in exc.errors(include_input=False, include_url=False)[:3]:
            field = error["loc"][-1] if error["loc"] else ""
            label = labels.get(field, "Значение настройки")
            context = error.get("ctx", {})
            kind = error["type"]
            if kind in ("less_than_equal", "greater_than_equal"):
                bound = context["le" if kind == "less_than_equal" else "ge"]
                lines.append(f"{label}: {'не больше' if kind == 'less_than_equal' else 'не меньше'} {bound}.")
            elif kind == "missing":
                lines.append(f"{label}: обязательное поле не заполнено.")
            elif kind == "extra_forbidden":
                lines.append("В настройках есть лишнее поле. Получи свежий промпт через /config.")
            elif kind == "value_error" and any("а" <= char.lower() <= "я" for char in error["msg"]):
                lines.append(error["msg"].removeprefix("Value error, "))
            else:
                lines.append(f"{label}: проверь формат и допустимые значения.")
        return "\n".join(lines)
    if isinstance(exc, (KeyError, IndexError, TypeError)):
        return "Не удалось распознать ввод или выбор. Открой нужный раздел заново."
    text = str(exc)
    if text.startswith(("invalid literal", "could not convert")):
        return "Нужно число. Дробную часть можно отделить точкой или запятой."
    if any("а" <= char.lower() <= "я" for char in text):
        return text[:1000]
    return "Проверь формат ввода по примеру в предыдущем сообщении."
