# fedor-bot

Discord-клиент для [fedor](https://github.com/kotomafia/fedor): принимает сообщения, пересылает текст и изображения в API модерации.

Сабмодуль в мета-репозитории: `bot/` → этот репозиторий.

## Зависимости

```powershell
pip install -r requirements.txt
```

## Запуск

Обычно бот запускают из **корня** [fedor](https://github.com/kotomafia/fedor) (там лежит `.env`):

```powershell
git clone --recurse-submodules https://github.com/kotomafia/fedor.git
cd fedor
copy .env.example .env
# DISCORD_BOT_TOKEN, MODERATION_API_URL
python -m bot
```

Отдельно только этот репозиторий:

```powershell
git clone https://github.com/kotomafia/fedor-bot.git
cd fedor-bot
pip install -r requirements.txt
# .env в текущей папке или в родительском fedor/
python -m bot
```

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `DISCORD_BOT_TOKEN` | Токен из [Discord Developer Portal](https://discord.com/developers/applications) |
| `MODERATION_API_URL` | URL API (по умолчанию `http://localhost:8000`) |
| `LOG_LEVEL` | Уровень логов |
| `TEST_GUILD_ID` | Опционально: ID сервера для slash-команд в dev |

Полный список — в `.env.example` мета-репозитория `fedor`.

## Связанные репозитории

- [fedor](https://github.com/kotomafia/fedor) — docker-compose, alembic, общий `.env`
- [fedor-api](https://github.com/kotomafia/fedor-api) — FastAPI и Celery
- [fedor-ml](https://github.com/kotomafia/fedor-ml) — модели (сабмодуль `api/ml`)
