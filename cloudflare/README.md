# Размещение на Cloudflare Workers

Python Worker принимает Telegram webhook. SQLite Durable Object хранит профили, расписания и очереди; Alarms запускают напоминания без работающего ноутбука. Общие модули приложения копируются из `../companion` командой `prepare.py`; облачный адаптер заменяет только Telegram-интерфейс. aiogram в Worker не устанавливается.

## Подготовка

Нужны Node.js, [uv](https://docs.astral.sh/uv/), аккаунт Cloudflare с **Workers Free** и локальное Python-окружение проекта. Бесплатный вариант работает в пределах [квот Cloudflare](https://developers.cloudflare.com/durable-objects/platform/pricing/); скрипт не подключает платные услуги.

Из корня репозитория создай `.env` по `.env.example` и настрой `BOT_TOKEN`. Затем:

```powershell
cd cloudflare
npm install
npx wrangler login
uv sync
uv run pywrangler sync
uv run python prepare.py
cd ..
```

`wrangler.jsonc` задаёт имя Worker `day-companion-bot`. Скрипт развёртывания ожидает это имя. Для нескольких аккаунтов укажи `CLOUDFLARE_ACCOUNT_ID` локально.

## Развёртывание и перенос

Сначала запусти бота локально и убедись, что SQLite-база создана. Скрипт переноса рассчитан на Windows, стандартный путь `data/bot.sqlite3` и окружение `.venv` в корне репозитория. При другом `DB_PATH` или размещении процесс переноса нужно адаптировать.

Останови локальный бот перед переносом. Если он запускался через `run.ps1`, заверши его в том терминале. Вспомогательный `scripts/cloudflare_local.ps1` управляет только процессом, который сам запустил и сохранил в `data/bot.pid`.

```powershell
.\.venv\Scripts\python.exe scripts/cloudflare_deploy.py stage
.\.venv\Scripts\python.exe scripts/cloudflare_deploy.py cutover
.\.venv\Scripts\python.exe scripts/cloudflare_deploy.py status
```

- `stage` проверяет тариф, разворачивает неактивный сервис, загружает секреты и проверяет расчёты с искусственными данными.
- `cutover` сохраняет резервную копию в `data/backups/`, переносит таблицы SQLite, сравнивает SHA-256 содержимого, устанавливает webhook и включает Alarms. При ошибке пытается восстановить локальную работу.
- `status` проверяет health, webhook, очередь и состояние облачного приложения. Его вывод может содержать служебную статистику; он предназначен для оператора.

Если CLI не имеет прав читать тариф, открой страницу Workers → Plans в панели Cloudflare и проверь текущий план. Только после фактической проверки создай локальный `data/cloudflare-plan-verification.json`:

```json
{
  "account": "YOUR_CLOUDFLARE_ACCOUNT_ID",
  "plan": "free",
  "source": "https://dash.cloudflare.com/YOUR_CLOUDFLARE_ACCOUNT_ID/workers/plans",
  "verified_at": 0
}
```

Вместо `0` укажи текущее Unix-время в секундах. Подтверждение действительно один час. Это файл оператора, он исключён из Git вместе со всей папкой `data/`.

После переноса проверь новые команды `/start` и `/config` и получение уведомления. Один токен должен обслуживаться одним активным экземпляром: локальный polling после переключения оставляют остановленным.

## Секреты и данные

`BOT_TOKEN`, случайные `WEBHOOK_SECRET` и `DEPLOY_SECRET` загружаются в Cloudflare Secrets. Локальное состояние скрипта хранится в игнорируемом `data/cloudflare-runtime.json`. Административные маршруты требуют отдельного секрета; webhook проверяет заголовок Telegram. Встроенные журналы Worker отключены.

Не добавляй в Git `.env`, `.dev.vars`, `data/`, резервные копии и журналы. Точки погоды, расписания и личные напоминания хранятся в облачной базе. `/delete` удаляет профиль из рабочей базы, ранее созданные резервные копии удаляются оператором отдельно.

## Отличия от локального запуска

В Worker доступны JSON/TXT, ICS, CSV и XLSX. PDF и фотографии сначала преобразуй внешним ИИ в JSON по промпту `/config`: локальные PyMuPDF и OCR здесь недоступны. Настройка через промпты не требует встроенного платного API ИИ.

Ответы команд отправляются при обработке webhook; Alarms нужны для напоминаний и повторных попыток. Отправки сериализуются по пользователю, повторный `update_id` не обрабатывается параллельно. Погода и Telegram ограничены таймаутом одного запроса. Доставка до устройства зависит также от Telegram и сети пользователя.
