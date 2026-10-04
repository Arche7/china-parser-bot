"""
Настройки бота.

Все секреты (токены) и параметры берутся из переменных окружения.
Локально они читаются из файла .env (см. .env.example),
на Railway — задаются во вкладке Variables сервиса.
"""

import os

from dotenv import load_dotenv

# Читаем файл .env, если он есть (на сервере его нет — и это нормально)
load_dotenv()


def _int_list(value: str) -> list[int]:
    """Превращает строку '123, 456' в список чисел [123, 456]."""
    result = []
    for part in value.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result


def _default_db_path() -> str:
    # На Railway база должна лежать на «томе» (Volume) — это диск, который
    # не стирается при каждом новом деплое. Railway сам сообщает, куда
    # смонтирован том, через переменную RAILWAY_VOLUME_MOUNT_PATH.
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume:
        return os.path.join(volume, "bot.db")
    if os.path.isdir("/data"):
        return "/data/bot.db"
    # Локально (на Mac) база будет лежать рядом с кодом
    return "bot.db"


# --- Обязательные ---
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
APIFY_TOKEN: str = os.getenv("APIFY_TOKEN", "").strip()

# --- Доступ ---
# Telegram ID администраторов через запятую. У админов полный доступ
# и команды /grant, /revoke, /users, /stats.
ADMIN_IDS: list[int] = _int_list(os.getenv("ADMIN_IDS", ""))

# --- Мониторинг ---
# Как часто проверять новые объявления (в минутах)
CHECK_INTERVAL_MIN: int = int(os.getenv("CHECK_INTERVAL_MIN", "30"))
# Сколько свежих объявлений забирать за ОДИН поисковый запрос.
# У брендов из brands.py обычно 2 запроса (латиница + по-китайски),
# поэтому за одну проверку бренда приходит до 2 × MAX_ITEMS объявлений.
# Больше = меньше шанс пропустить, но дороже на Apify.
MAX_ITEMS: int = int(os.getenv("MAX_ITEMS", "5"))
# Сколько брендов может добавить один пользователь
MAX_BRANDS_PER_USER: int = int(os.getenv("MAX_BRANDS_PER_USER", "10"))
# Курс юаня к рублю — только для подсказки «≈ ₽» в сообщении
CNY_RUB_RATE: float = float(os.getenv("CNY_RUB_RATE", "11.5"))

# --- Apify ---
APIFY_ACTOR_ID: str = os.getenv(
    "APIFY_ACTOR_ID", "unitbytes/goofish-xianyu-search-scraper"
).strip()
# Страна резидентного прокси для актора (HK = Гонконг, работает стабильнее всего)
APIFY_PROXY_COUNTRY: str = os.getenv("APIFY_PROXY_COUNTRY", "HK").strip()
# Примерная цена Apify за 1000 объявлений в режиме summary (для команды /stats)
APIFY_PRICE_PER_1000: float = float(os.getenv("APIFY_PRICE_PER_1000", "1.6"))

# --- База данных ---
DB_PATH: str = os.getenv("DB_PATH", "").strip() or _default_db_path()


def check_config() -> None:
    """Проверяет, что обязательные настройки заданы. Иначе — понятная ошибка."""
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not APIFY_TOKEN:
        missing.append("APIFY_TOKEN")
    if missing:
        raise SystemExit(
            "Не заданы обязательные переменные: "
            + ", ".join(missing)
            + ".\nСоздай файл .env по образцу .env.example (локально) "
            "или добавь переменные во вкладке Variables на Railway."
        )
    if not ADMIN_IDS:
        print(
            "ВНИМАНИЕ: ADMIN_IDS пуст — ботом никто не сможет пользоваться.\n"
            "Напиши боту /start, он покажет твой ID, и впиши его в ADMIN_IDS."
        )
