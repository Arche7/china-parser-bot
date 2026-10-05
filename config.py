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
# Владелец бота — только ему доступна выгрузка денег (/export).
# По умолчанию — первый ID из ADMIN_IDS. Можно задать отдельно переменной OWNER_ID.
OWNER_ID: int | None = (_int_list(os.getenv("OWNER_ID", "")) or ADMIN_IDS or [None])[0]

# --- Мониторинг ---
# Интервал проверки теперь зависит от тарифа (plans.py: 30/15/10 мин).
# Эта настройка — интервал для старых пользователей без тарифа и
# нижняя граница: чаще неё бот не проверит ни один бренд.
CHECK_INTERVAL_MIN: int = int(os.getenv("CHECK_INTERVAL_MIN", "10"))
# Сколько свежих объявлений забирать за ОДИН поисковый запрос.
# У брендов из brands.py обычно 2 запроса (латиница + по-китайски),
# поэтому за одну проверку бренда приходит до 2 × MAX_ITEMS объявлений.
# Больше = меньше шанс пропустить, но дороже на Apify.
MAX_ITEMS: int = int(os.getenv("MAX_ITEMS", "5"))
# Лимит брендов теперь задаётся тарифом (plans.py). Эта настройка
# осталась для совместимости и больше ни на что не влияет.
MAX_BRANDS_PER_USER: int = int(os.getenv("MAX_BRANDS_PER_USER", "10"))
# Курс юаня к рублю. Бот сам берёт свежий курс ЦБ раз в несколько часов,
# а это значение — запасное, если сайт ЦБ не ответил.
CNY_RUB_RATE: float = float(os.getenv("CNY_RUB_RATE", "11.5"))

# --- Apify ---
APIFY_ACTOR_ID: str = os.getenv(
    "APIFY_ACTOR_ID", "unitbytes/goofish-xianyu-search-scraper"
).strip()
# Страна резидентного прокси для актора (HK = Гонконг, работает стабильнее всего)
APIFY_PROXY_COUNTRY: str = os.getenv("APIFY_PROXY_COUNTRY", "HK").strip()
# Примерная цена Apify за 1000 объявлений в режиме summary (для команды /stats)
APIFY_PRICE_PER_1000: float = float(os.getenv("APIFY_PRICE_PER_1000", "1.6"))

# --- Бренд и ссылки ---
# Как называется продукт в сообщениях
BRAND_NAME: str = os.getenv("BRAND_NAME", "HUNTR").strip()
# Ссылка на мини-приложение HUNTR (https://...). Пока пусто — кнопки нет.
# Если не задано, но у сервиса на Railway есть публичный домен — берём его сами
WEBAPP_URL: str = os.getenv("WEBAPP_URL", "").strip() or (
    f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN').strip()}/app" if os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip() else ""
)
# Порт веб-сервера приложения (Railway подставляет PORT сам)
WEB_PORT: int = int(os.getenv("PORT", "8080"))
# Ник поддержки без @ — куда писать по оплате и вопросам
SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "").strip().lstrip("@")
# Сколько дней длится бесплатный пробный период (0 — выключить)
TRIAL_DAYS: int = int(os.getenv("TRIAL_DAYS", "2"))
# Сколько дней лента хранит находки (старше — уходят в архив, остаётся только избранное)
FEED_DAYS: int = int(os.getenv("FEED_DAYS", "3"))
# Сколько дней дарим пригласившему, когда друг оплатил подписку
REFERRAL_BONUS_DAYS: int = int(os.getenv("REFERRAL_BONUS_DAYS", "7"))

# --- Оплата звёздами Telegram ---
# 1 — кнопки «Оплатить ⭐» работают, 0 — вместо оплаты бот отправляет в поддержку
PAYMENTS_ENABLED: bool = os.getenv("PAYMENTS_ENABLED", "1").strip() not in ("0", "false", "no", "")
# Сколько примерно стоит пользователю 1 звезда в рублях (для подсказки «≈ ₽»)
STAR_RUB_BUY: float = float(os.getenv("STAR_RUB_BUY", "1.35"))
# Сколько примерно получаешь ты за 1 звезду при выводе, в долларах (для /stats)
STAR_USD_PAYOUT: float = float(os.getenv("STAR_USD_PAYOUT", "0.013"))
# Бренды, которые бот один раз сам добавит админам (через запятую)
ADMIN_SEED_BRANDS: list[str] = [
    b.strip().lower() for b in os.getenv("ADMIN_SEED_BRANDS", "goyard,tom ford,dior").split(",") if b.strip()
]

# --- Расписание проверок ---
# Как часто монитор «просыпается» и смотрит, какие бренды пора проверить.
# Сам интервал проверки бренда зависит от тарифа (см. plans.py).
TICK_MIN: int = int(os.getenv("TICK_MIN", "5"))
# Умная экономия: если по бренду несколько проверок подряд не было новинок,
# проверяем его реже (максимум в столько раз). 1 — выключить.
ADAPTIVE_MAX_FACTOR: float = float(os.getenv("ADAPTIVE_MAX_FACTOR", "3"))

# --- ИИ (перевод заголовков и легит-чек) ---
# Подходит любой сервис с OpenAI-совместимым API: OpenAI, OpenRouter,
# DeepSeek, ProxyAPI и т. п. Пустой ключ — функции ИИ выключены,
# заголовки переводятся встроенным словарём.
AI_API_KEY: str = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL: str = os.getenv("AI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
# Дешёвая модель для перевода заголовков
AI_MODEL: str = os.getenv("AI_MODEL", "gpt-4o-mini").strip()
# Модель, которая «видит» фото — для легит-чека
AI_VISION_MODEL: str = os.getenv("AI_VISION_MODEL", "gpt-4o").strip()
# Модель ИИ-помощника по переписке с продавцом (ELITE): видит скрины и пишет по-китайски.
# По умолчанию та же «зрячая» модель, что и для легит-чека.
AI_ASSISTANT_MODEL: str = os.getenv("AI_ASSISTANT_MODEL", "").strip() or AI_VISION_MODEL

# --- Сравнение цен с Авито ---
# Официальный API Авито не умеет искать чужие объявления, поэтому
# цены берём через актор на Apify (тот же APIFY_TOKEN).
AVITO_ENABLED: bool = os.getenv("AVITO_ENABLED", "1").strip() not in ("0", "false", "no", "")
AVITO_ACTOR_ID: str = os.getenv("AVITO_ACTOR_ID", "ahaham_bytiz/avito-scraper").strip()
AVITO_MAX_ITEMS: int = int(os.getenv("AVITO_MAX_ITEMS", "30"))
# Сколько раз пробовать основной актор (каждый раз — новый IP), если Авито ответил 429
AVITO_TRIES: int = int(os.getenv("AVITO_TRIES", "2"))
# Запасной актор, если основной не справился. Пусто — без запасного.
AVITO_FALLBACK_ACTOR_ID: str = os.getenv("AVITO_FALLBACK_ACTOR_ID", "logiover/avito-ru-scraper").strip()
# Сколько часов помнить цены Авито по одному запросу (экономит деньги)
AVITO_CACHE_HOURS: int = int(os.getenv("AVITO_CACHE_HOURS", "24"))

# --- Калькулятор себестоимости (значения по умолчанию, каждый может поменять у себя) ---
# Доставка Китай → Россия, ₽ за кг
DELIVERY_RUB_PER_KG: int = int(os.getenv("DELIVERY_RUB_PER_KG", "1000"))
# Комиссия байера/посредника, %
BUYER_FEE_PCT: float = float(os.getenv("BUYER_FEE_PCT", "5"))
# Курс доллара — только для подсказок о расходах в /stats
USD_RUB_RATE: float = float(os.getenv("USD_RUB_RATE", "82"))

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
