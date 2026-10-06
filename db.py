"""
Работа с базой данных SQLite.

Таблицы:
  users      — пользователи: тариф, до какого числа доступ, настройки
  watches    — бренды, которые отслеживает пользователь
  seen       — объявления, которые бот уже видел (чтобы не слать повторно)
  sent       — что уже отправлено каждому пользователю
  listings   — копия отправленных объявлений (для кнопок под карточкой:
               легит-чек, цена в РФ, избранное — им нужны данные объявления)
  favorites  — избранное пользователя
  usage      — сколько легит-чеков/сравнений цен потрачено в этом месяце
  jobs       — расписание проверок по каждому бренду (умная экономия)
  avito_cache — цены с Авито, чтобы не платить за один и тот же запрос
  feed       — лента: что нашлось для каждого пользователя (сводки и просмотр по разделам)
  payments   — оплаты звёздами (нужны для возвратов и отмены старой подписки)
  activity   — кто и когда заходил (бот / приложение) по дням — для отчёта /report

Старая база (от прошлой версии бота) обновляется сама при запуске:
новые столбцы и таблицы добавляются, старые данные не трогаются.
"""

import json
import logging
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite

import config

log = logging.getLogger("db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    created_at  INTEGER NOT NULL,
    sub_until   INTEGER NOT NULL DEFAULT 0,   -- unix-время окончания доступа (0 = нет)
    paused      INTEGER NOT NULL DEFAULT 0    -- 1 = уведомления на паузе
);

CREATE TABLE IF NOT EXISTS watches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    keyword     TEXT NOT NULL,                -- всегда в нижнем регистре
    price_min   REAL,                         -- в юанях, может быть NULL
    price_max   REAL,                         -- в юанях, может быть NULL
    created_at  INTEGER NOT NULL,
    UNIQUE (user_id, keyword)
);

CREATE TABLE IF NOT EXISTS seen (
    source      TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    PRIMARY KEY (source, keyword, item_id)
);

CREATE TABLE IF NOT EXISTS sent (
    user_id     INTEGER NOT NULL,
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    sent_at     INTEGER NOT NULL,
    PRIMARY KEY (user_id, source, item_id)
);

CREATE TABLE IF NOT EXISTS listings (
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    keyword     TEXT NOT NULL,
    data        TEXT NOT NULL,                -- объявление целиком, JSON
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (source, item_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    user_id     INTEGER NOT NULL,
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (user_id, source, item_id)
);

CREATE TABLE IF NOT EXISTS usage (
    user_id     INTEGER NOT NULL,
    kind        TEXT NOT NULL,                -- legit / price
    period      TEXT NOT NULL,                -- '2026-10'
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, kind, period)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_key     TEXT PRIMARY KEY,             -- 'goofish|gucci'
    last_check  INTEGER NOT NULL DEFAULT 0,
    empty_streak INTEGER NOT NULL DEFAULT 0,  -- сколько проверок подряд без новинок
    max_items   INTEGER NOT NULL DEFAULT 0,   -- сколько объявлений брать в следующий раз
    found_total INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS avito_cache (
    query       TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    charge_id   TEXT PRIMARY KEY,             -- telegram_payment_charge_id
    user_id     INTEGER NOT NULL,
    plan        TEXT NOT NULL,
    months      INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    recurring   INTEGER NOT NULL DEFAULT 0,   -- 1 = подписка с автопродлением
    refunded    INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS feed (
    user_id     INTEGER NOT NULL,
    source      TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    keyword     TEXT NOT NULL,                -- бренд (канонический ключ)
    grp         TEXT NOT NULL,                -- Одежда / Обувь / Сумки / Аксессуары / Другое
    created_at  INTEGER NOT NULL,
    notified    INTEGER NOT NULL DEFAULT 0,   -- 1 = уже попало в сводку или пришло отдельно
    PRIMARY KEY (user_id, source, item_id)
);
CREATE INDEX IF NOT EXISTS idx_feed_user_time ON feed (user_id, created_at);

CREATE TABLE IF NOT EXISTS own_slots (
    slot_id     TEXT PRIMARY KEY,             -- из payload счёта: у продлений подписки он тот же
    user_id     INTEGER NOT NULL,
    until       INTEGER NOT NULL,             -- до какого момента оплачен слот
    charge_id   TEXT                          -- последний платёж за этот слот
);

CREATE INDEX IF NOT EXISTS idx_watches_keyword ON watches (keyword);
CREATE INDEX IF NOT EXISTS idx_sent_time ON sent (sent_at);
CREATE INDEX IF NOT EXISTS idx_seen_time ON seen (first_seen);
CREATE INDEX IF NOT EXISTS idx_listings_time ON listings (created_at);

-- ИИ-помощник по переписке (ELITE): история разговоров, общая для бота и приложения.
-- thread — "goofish:123" (разговор про конкретную вещь) или "general".
-- В data есть "via": "bot" / "app" — откуда пришло сообщение.
CREATE TABLE IF NOT EXISTS assistant_msgs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    thread      TEXT NOT NULL,
    role        TEXT NOT NULL,                -- user / assistant
    data        TEXT NOT NULL,                -- JSON: текст, число фото или ответ ИИ
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assistant_thread ON assistant_msgs (user_id, thread, id);

-- Активность для отчёта /report: одна строка на человека, день и канал.
-- day — дата по Москве ('2026-10-06'), channel — 'bot' (чат) или 'app' (мини-приложение).
-- Строка не растёт от каждого нажатия: увеличивается только счётчик events.
CREATE TABLE IF NOT EXISTS activity (
    user_id     INTEGER NOT NULL,
    day         TEXT NOT NULL,
    channel     TEXT NOT NULL,
    events      INTEGER NOT NULL DEFAULT 0,
    first_at    INTEGER NOT NULL,             -- первое действие за день (unix-время)
    last_at     INTEGER NOT NULL,             -- последнее действие за день
    PRIMARY KEY (user_id, day, channel)
);
CREATE INDEX IF NOT EXISTS idx_activity_day ON activity (day);
"""

# Новые столбцы в старых таблицах: (таблица, столбец, описание)
MIGRATIONS = [
    ("users", "plan", "TEXT"),                                # trial / start / pro / elite
    ("users", "trial_used", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "settings", "TEXT"),                            # JSON с настройками
    ("users", "ref_by", "INTEGER"),                           # кто пригласил
    ("users", "ref_rewarded", "INTEGER NOT NULL DEFAULT 0"),  # бонус за друга уже выдан
    ("users", "home_msg_id", "INTEGER"),                      # id сообщения «пульта»
    ("watches", "paused", "INTEGER NOT NULL DEFAULT 0"),
    ("watches", "found", "INTEGER NOT NULL DEFAULT 0"),       # сколько прислали по бренду
    ("feed", "seen", "INTEGER NOT NULL DEFAULT 0"),           # 1 = уже посмотрел (в чате или приложении)
    ("feed", "hidden", "INTEGER NOT NULL DEFAULT 0"),         # 1 = скрыл «неинтересно»
    ("payments", "slot", "TEXT"),                             # слот «+1 свой бренд», за который платёж
    ("users", "last_seen", "INTEGER"),                        # когда последний раз что-то нажимал (бот или приложение)
    ("users", "trial_at", "INTEGER"),                         # когда включил пробный период (для /report)
    ("users", "source", "TEXT"),                              # откуда пришёл: tt / ig / tg / vk / yt / src_… (ссылка ?start=…)
]

# Настройки пользователя по умолчанию
DEFAULT_SETTINGS = {
    "card": "compact",          # compact / full — вид карточки объявления
    "title": "ru",              # ru — перевод, orig — как на площадке
    "quiet": False,             # тихие часы 00:00–08:00 (без звука)
    "delivery": config.DELIVERY_RUB_PER_KG,   # ₽ за кг
    "fee": config.BUYER_FEE_PCT,              # комиссия байера, %
    "notify": "digest",         # digest — сводкой, instant — каждое сразу, off — только лента
    "every": 30,                # как часто присылать сводку, минут
}


def _now() -> int:
    return int(time.time())


def _period() -> str:
    return datetime.now().strftime("%Y-%m")


# Границы дня для отчёта считаем по Москве: «сегодня» для владельца — это
# московские сутки, а сервер на Railway живёт по UTC (там «сутки» сдвинуты на 3 часа).
try:
    MSK = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    # Если в системе нет базы часовых поясов (пакет tzdata не поставился) —
    # Москва с 2014 года живёт в UTC+3 без перехода на летнее время.
    MSK = timezone(timedelta(hours=3), "MSK")


def msk_today() -> date:
    """Сегодняшняя дата по Москве."""
    return datetime.now(MSK).date()


def msk_day(ts: int | None = None) -> str:
    """Дата по Москве строкой '2026-10-06' (так она лежит в таблице activity)."""
    return datetime.fromtimestamp(ts if ts is not None else _now(), MSK).strftime("%Y-%m-%d")


def day_bounds(first: date, last: date | None = None) -> tuple[int, int]:
    """
    Unix-время начала дня first и конца дня last (не включая) по Москве.
    Нужно, чтобы искать по created_at в users и payments «за этот день».
    """
    last = last or first
    start = datetime.combine(first, dtime(0, 0), MSK)
    end = datetime.combine(last + timedelta(days=1), dtime(0, 0), MSK)
    return int(start.timestamp()), int(end.timestamp())


class Database:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        """Добавляет новые столбцы в таблицы старой версии бота."""
        for table, column, ddl in MIGRATIONS:
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in await cur.fetchall()}
            if column not in columns:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------------- Пользователи ----------------

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> bool:
        """Сохраняет пользователя. True — если это новый пользователь."""
        cur = await self.conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
        exists = await cur.fetchone() is not None
        if exists:
            await self.conn.execute(
                "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
                (username, first_name, user_id),
            )
        else:
            await self.conn.execute(
                "INSERT INTO users (user_id, username, first_name, created_at) VALUES (?, ?, ?, ?)",
                (user_id, username, first_name, _now()),
            )
        await self.conn.commit()
        return not exists

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

    async def has_access(self, user_id: int) -> bool:
        """Админ — всегда. Остальные — пока не закончилась подписка/пробный период."""
        if user_id in config.ADMIN_IDS:
            return True
        user = await self.get_user(user_id)
        return bool(user and user["sub_until"] > _now())

    async def user_plan_code(self, user_id: int) -> str | None:
        if user_id in config.ADMIN_IDS:
            return "admin"
        user = await self.get_user(user_id)
        return user["plan"] if user else None

    async def grant(self, user_id: int, days: int, plan: str | None = None) -> int:
        """Выдать/продлить доступ на N дней. Возвращает новое время окончания."""
        user = await self.get_user(user_id)
        if user is None:
            await self.conn.execute(
                "INSERT INTO users (user_id, created_at) VALUES (?, ?)", (user_id, _now())
            )
            start = _now()
        else:
            # Смена тарифа (в том числе пробный → платный) начинается сегодня,
            # продление того же тарифа — с конца текущего срока
            if plan and user["plan"] and plan != user["plan"]:
                start = _now()
            else:
                start = max(user["sub_until"], _now())
        until = start + days * 86400
        if plan:
            await self.conn.execute(
                "UPDATE users SET sub_until = ?, plan = ? WHERE user_id = ?", (until, plan, user_id)
            )
        else:
            await self.conn.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (until, user_id))
        await self.conn.commit()
        return until

    async def set_access_until(self, user_id: int, until: int, plan: str) -> int:
        """Доступ до конкретной даты (для подписки звёздами: дату даёт Telegram)."""
        user = await self.get_user(user_id)
        if user is None:
            await self.conn.execute("INSERT INTO users (user_id, created_at) VALUES (?, ?)", (user_id, _now()))
            current = 0
        else:
            current = user["sub_until"] if user["plan"] == plan else 0
        until = max(until, current)
        await self.conn.execute("UPDATE users SET sub_until = ?, plan = ? WHERE user_id = ?", (until, plan, user_id))
        await self.conn.commit()
        return until

    async def start_trial(self, user_id: int, days: int) -> int | None:
        """Включает пробный период один раз. None — если уже был."""
        user = await self.get_user(user_id)
        if not user or user["trial_used"] or user["sub_until"] > _now():
            return None
        until = _now() + days * 86400
        # trial_at — когда включил пробный: по нему /report считает «пробных за день» и конверсию
        await self.conn.execute(
            "UPDATE users SET sub_until = ?, plan = 'trial', trial_used = 1, trial_at = ? WHERE user_id = ?",
            (until, _now(), user_id),
        )
        await self.conn.commit()
        return until

    async def set_plan(self, user_id: int, plan: str) -> None:
        await self.conn.execute("UPDATE users SET plan = ? WHERE user_id = ?", (plan, user_id))
        await self.conn.commit()

    async def revoke(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET sub_until = 0 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def set_paused(self, user_id: int, paused: bool) -> None:
        await self.conn.execute(
            "UPDATE users SET paused = ? WHERE user_id = ?", (1 if paused else 0, user_id)
        )
        await self.conn.commit()

    async def set_ref(self, user_id: int, ref_by: int) -> None:
        """Запоминает, кто пригласил (только для новых и только один раз)."""
        await self.conn.execute(
            "UPDATE users SET ref_by = ? WHERE user_id = ? AND ref_by IS NULL AND user_id != ?",
            (ref_by, user_id, ref_by),
        )
        await self.conn.commit()

    async def mark_ref_rewarded(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET ref_rewarded = 1 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def count_refs(self, user_id: int) -> tuple[int, int]:
        """(сколько пришло по ссылке, сколько из них оплатили)"""
        cur = await self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(ref_rewarded), 0) FROM users WHERE ref_by = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row[0], row[1]

    async def set_home_msg(self, user_id: int, message_id: int | None) -> None:
        await self.conn.execute(
            "UPDATE users SET home_msg_id = ? WHERE user_id = ?", (message_id, user_id)
        )
        await self.conn.commit()

    async def get_settings(self, user_id: int) -> dict:
        user = await self.get_user(user_id)
        settings = dict(DEFAULT_SETTINGS)
        if user and user["settings"]:
            try:
                settings.update(json.loads(user["settings"]))
            except ValueError:
                pass
        return settings

    async def update_settings(self, user_id: int, **changes) -> dict:
        settings = await self.get_settings(user_id)
        settings.update(changes)
        await self.conn.execute(
            "UPDATE users SET settings = ? WHERE user_id = ?",
            (json.dumps(settings, ensure_ascii=False), user_id),
        )
        await self.conn.commit()
        return settings

    async def list_users(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """
            SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.user_id = u.user_id) AS brands
            FROM users u ORDER BY u.created_at DESC
            """
        )
        return list(await cur.fetchall())

    async def all_user_ids(self, only_active: bool = False) -> list[int]:
        if only_active:
            cur = await self.conn.execute("SELECT user_id FROM users WHERE sub_until > ?", (_now(),))
        else:
            cur = await self.conn.execute("SELECT user_id FROM users")
        return [row[0] for row in await cur.fetchall()]

    async def count_plan_seats(self, plan: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM users WHERE plan = ? AND sub_until > ?", (plan, _now())
        )
        return (await cur.fetchone())[0]

    # ---------------- Бренды (watches) ----------------

    async def add_watch(
        self, user_id: int, keyword: str, price_min: float | None, price_max: float | None
    ) -> bool:
        """Добавляет бренд. Если такой уже есть — обновляет цены. True = новый."""
        keyword = keyword.strip().lower()
        cur = await self.conn.execute(
            "SELECT id FROM watches WHERE user_id = ? AND keyword = ?", (user_id, keyword)
        )
        existing = await cur.fetchone()
        if existing:
            await self.conn.execute(
                "UPDATE watches SET price_min = ?, price_max = ? WHERE id = ?",
                (price_min, price_max, existing["id"]),
            )
            await self.conn.commit()
            return False
        await self.conn.execute(
            """
            INSERT INTO watches (user_id, keyword, price_min, price_max, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, keyword, price_min, price_max, _now()),
        )
        await self.conn.commit()
        return True

    async def get_watch(self, user_id: int, watch_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM watches WHERE id = ? AND user_id = ?", (watch_id, user_id)
        )
        return await cur.fetchone()

    async def get_watch_by_keyword(self, user_id: int, keyword: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM watches WHERE user_id = ? AND keyword = ?",
            (user_id, keyword.strip().lower()),
        )
        return await cur.fetchone()

    async def list_watches(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM watches WHERE user_id = ? ORDER BY created_at", (user_id,)
        )
        return list(await cur.fetchall())

    async def count_watches(self, user_id: int) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM watches WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0]

    async def update_watch_price(
        self, user_id: int, watch_id: int, price_min: float | None, price_max: float | None
    ) -> None:
        await self.conn.execute(
            "UPDATE watches SET price_min = ?, price_max = ? WHERE id = ? AND user_id = ?",
            (price_min, price_max, watch_id, user_id),
        )
        await self.conn.commit()

    async def set_watch_paused(self, user_id: int, watch_id: int, paused: bool) -> None:
        await self.conn.execute(
            "UPDATE watches SET paused = ? WHERE id = ? AND user_id = ?",
            (1 if paused else 0, watch_id, user_id),
        )
        await self.conn.commit()

    async def bump_watch_found(self, user_id: int, keyword: str) -> None:
        await self.conn.execute(
            "UPDATE watches SET found = found + 1 WHERE user_id = ? AND keyword = ?",
            (user_id, keyword),
        )
        await self.conn.commit()

    async def delete_watch(self, user_id: int, watch_id: int) -> str | None:
        """Удаляет бренд по id. Возвращает название удалённого бренда или None."""
        cur = await self.conn.execute(
            "SELECT keyword FROM watches WHERE id = ? AND user_id = ?", (watch_id, user_id)
        )
        row = await cur.fetchone()
        if not row:
            return None
        await self.conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        await self.conn.commit()
        return row["keyword"]

    async def normalize_keywords(self, canonical) -> int:
        """
        Приводит сохранённые бренды к одному написанию (canonical из brands.py).
        Если у человека бренд записан в нескольких написаниях — оставляем
        самую старую запись (с её бюджетом), остальные удаляем.
        Возвращает, сколько записей поправлено или удалено.
        """
        cur = await self.conn.execute("SELECT id, user_id, keyword FROM watches ORDER BY id")
        groups: dict[tuple[int, str], list] = {}
        for r in await cur.fetchall():
            key = canonical(r["keyword"]) or r["keyword"]
            groups.setdefault((r["user_id"], key), []).append(r)
        fixed = 0
        for (_, key), rows in groups.items():
            keep, extra = rows[0], rows[1:]
            for r in extra:
                await self.conn.execute("DELETE FROM watches WHERE id = ?", (r["id"],))
                fixed += 1
            if keep["keyword"] != key:
                await self.conn.execute("UPDATE watches SET keyword = ? WHERE id = ?", (key, keep["id"]))
                fixed += 1
        await self.conn.commit()
        return fixed

    async def delete_watch_by_keyword(self, user_id: int, keyword: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM watches WHERE user_id = ? AND keyword = ?",
            (user_id, keyword.strip().lower()),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def active_watches(self) -> list[aiosqlite.Row]:
        """
        Все включённые бренды тех, у кого есть доступ и не стоит пауза.
        Вместе с брендом отдаём тариф пользователя — от него зависит,
        как часто проверять бренд.
        """
        admin_ids = config.ADMIN_IDS or [-1]
        placeholders = ",".join("?" for _ in admin_ids)
        cur = await self.conn.execute(
            f"""
            SELECT w.*, u.plan AS plan,
                   CASE WHEN u.user_id IN ({placeholders}) THEN 1 ELSE 0 END AS is_admin
            FROM watches w
            JOIN users u ON u.user_id = w.user_id
            WHERE u.paused = 0 AND w.paused = 0
              AND (u.user_id IN ({placeholders}) OR u.sub_until > ?)
            """,
            (*admin_ids, *admin_ids, _now()),
        )
        return list(await cur.fetchall())

    # ---------------- Уже виденные объявления ----------------

    async def has_any_seen(self, source: str, keyword: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM seen WHERE source = ? AND keyword = ? LIMIT 1", (source, keyword)
        )
        return (await cur.fetchone()) is not None

    async def filter_unseen(self, source: str, keyword: str, item_ids: list[str]) -> set[str]:
        """Из списка id возвращает только те, которых ещё нет в базе."""
        if not item_ids:
            return set()
        placeholders = ",".join("?" for _ in item_ids)
        cur = await self.conn.execute(
            f"SELECT item_id FROM seen WHERE source = ? AND keyword = ? AND item_id IN ({placeholders})",
            (source, keyword, *item_ids),
        )
        already = {row["item_id"] for row in await cur.fetchall()}
        return set(item_ids) - already

    async def mark_seen(self, source: str, keyword: str, item_ids: list[str]) -> None:
        now = _now()
        await self.conn.executemany(
            "INSERT OR IGNORE INTO seen (source, keyword, item_id, first_seen) VALUES (?, ?, ?, ?)",
            [(source, keyword, item_id, now) for item_id in item_ids],
        )
        await self.conn.commit()

    # ---------------- Что уже отправлено пользователю ----------------

    async def was_sent(self, user_id: int, source: str, item_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM sent WHERE user_id = ? AND source = ? AND item_id = ?",
            (user_id, source, item_id),
        )
        return (await cur.fetchone()) is not None

    async def mark_sent(self, user_id: int, source: str, item_id: str) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO sent (user_id, source, item_id, sent_at) VALUES (?, ?, ?, ?)",
            (user_id, source, item_id, _now()),
        )
        await self.conn.commit()

    async def count_sent_since(self, user_id: int, since: int) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM sent WHERE user_id = ? AND sent_at >= ?", (user_id, since)
        )
        return (await cur.fetchone())[0]

    # ---------------- Объявления (для кнопок под карточкой) ----------------

    async def save_listing(self, source: str, item_id: str, keyword: str, data: dict) -> None:
        await self.conn.execute(
            """
            INSERT INTO listings (source, item_id, keyword, data, created_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source, item_id) DO UPDATE SET data = excluded.data
            """,
            (source, item_id, keyword, json.dumps(data, ensure_ascii=False), _now()),
        )
        await self.conn.commit()

    async def get_listing(self, source: str, item_id: str) -> tuple[str, dict] | None:
        cur = await self.conn.execute(
            "SELECT keyword, data FROM listings WHERE source = ? AND item_id = ?", (source, item_id)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return row["keyword"], json.loads(row["data"])

    # ---------------- Избранное ----------------

    async def toggle_favorite(self, user_id: int, source: str, item_id: str) -> bool:
        """Добавляет или убирает из избранного. True — теперь в избранном."""
        cur = await self.conn.execute(
            "DELETE FROM favorites WHERE user_id = ? AND source = ? AND item_id = ?",
            (user_id, source, item_id),
        )
        if cur.rowcount:
            await self.conn.commit()
            return False
        await self.conn.execute(
            "INSERT INTO favorites (user_id, source, item_id, created_at) VALUES (?, ?, ?, ?)",
            (user_id, source, item_id, _now()),
        )
        await self.conn.commit()
        return True

    async def is_favorite(self, user_id: int, source: str, item_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM favorites WHERE user_id = ? AND source = ? AND item_id = ?",
            (user_id, source, item_id),
        )
        return await cur.fetchone() is not None

    async def list_favorites(self, user_id: int, limit: int = 30) -> list[tuple[str, str, dict]]:
        cur = await self.conn.execute(
            """
            SELECT f.source, f.item_id, l.keyword, l.data FROM favorites f
            JOIN listings l ON l.source = f.source AND l.item_id = f.item_id
            WHERE f.user_id = ? ORDER BY f.created_at DESC LIMIT ?
            """,
            (user_id, limit),
        )
        return [(r["source"], r["keyword"], json.loads(r["data"])) for r in await cur.fetchall()]

    # ---------------- Лимиты тарифа ----------------

    # ---------------- ИИ-помощник (общая история бота и приложения) ----------------

    async def add_assistant_msg(self, user_id: int, thread: str, role: str, data: dict) -> int:
        cur = await self.conn.execute(
            "INSERT INTO assistant_msgs (user_id, thread, role, data, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, thread, role, json.dumps(data, ensure_ascii=False), _now()),
        )
        await self.conn.commit()
        return cur.lastrowid

    async def list_assistant_msgs(self, user_id: int, thread: str, limit: int = 60) -> list[dict]:
        """Последние сообщения разговора, от старых к новым."""
        cur = await self.conn.execute(
            "SELECT id, role, data, created_at FROM assistant_msgs WHERE user_id = ? AND thread = ? "
            "ORDER BY id DESC LIMIT ?",
            (user_id, thread, limit),
        )
        rows = list(await cur.fetchall())
        return [{"id": r["id"], "role": r["role"], "at": r["created_at"], **json.loads(r["data"])}
                for r in reversed(rows)]

    async def count_assistant_msgs(self, user_id: int, thread: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM assistant_msgs WHERE user_id = ? AND thread = ?", (user_id, thread)
        )
        return (await cur.fetchone())[0]

    async def list_assistant_threads(self, user_id: int, limit: int = 30) -> list[dict]:
        """
        Разговоры пользователя (и из бота, и из приложения), свежие сверху:
        [{"thread", "count", "last_at", "last": последнее сообщение}].
        """
        cur = await self.conn.execute(
            """
            SELECT a.thread, a.role, a.data, a.created_at, t.cnt
            FROM assistant_msgs a
            JOIN (SELECT thread, MAX(id) AS last_id, COUNT(*) AS cnt
                  FROM assistant_msgs WHERE user_id = ? GROUP BY thread) t ON a.id = t.last_id
            ORDER BY a.id DESC LIMIT ?
            """,
            (user_id, limit),
        )
        return [{"thread": r["thread"], "count": r["cnt"], "last_at": r["created_at"],
                 "last": {"role": r["role"], **json.loads(r["data"])}}
                for r in await cur.fetchall()]

    async def clear_assistant(self, user_id: int, thread: str) -> None:
        await self.conn.execute("DELETE FROM assistant_msgs WHERE user_id = ? AND thread = ?", (user_id, thread))
        await self.conn.commit()

    async def get_usage(self, user_id: int, kind: str) -> int:
        cur = await self.conn.execute(
            "SELECT count FROM usage WHERE user_id = ? AND kind = ? AND period = ?",
            (user_id, kind, _period()),
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def add_usage(self, user_id: int, kind: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO usage (user_id, kind, period, count) VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, kind, period) DO UPDATE SET count = count + 1
            """,
            (user_id, kind, _period()),
        )
        await self.conn.commit()

    # ---------------- Расписание проверок ----------------

    async def get_job(self, job_key: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM jobs WHERE job_key = ?", (job_key,))
        return await cur.fetchone()

    async def save_job(self, job_key: str, empty_streak: int, max_items: int, found: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO jobs (job_key, last_check, empty_streak, max_items, found_total)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_key) DO UPDATE SET
                last_check = excluded.last_check,
                empty_streak = excluded.empty_streak,
                max_items = excluded.max_items,
                found_total = found_total + excluded.found_total
            """,
            (job_key, _now(), empty_streak, max_items, found),
        )
        await self.conn.commit()

    # ---------------- Кэш цен Авито ----------------

    async def get_avito(self, query: str, max_age_sec: int) -> dict | None:
        cur = await self.conn.execute(
            "SELECT data, created_at FROM avito_cache WHERE query = ?", (query,)
        )
        row = await cur.fetchone()
        if not row or _now() - row["created_at"] > max_age_sec:
            return None
        return json.loads(row["data"])

    async def save_avito(self, query: str, data: dict) -> None:
        await self.conn.execute(
            """
            INSERT INTO avito_cache (query, data, created_at) VALUES (?, ?, ?)
            ON CONFLICT(query) DO UPDATE SET data = excluded.data, created_at = excluded.created_at
            """,
            (query, json.dumps(data, ensure_ascii=False), _now()),
        )
        await self.conn.commit()

    # ---------------- Лента ----------------

    async def add_feed(self, user_id: int, source: str, item_id: str, keyword: str,
                       grp: str, notified: bool = False) -> bool:
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO feed (user_id, source, item_id, keyword, grp, created_at, notified)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, source, item_id, keyword, grp, _now(), 1 if notified else 0),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def users_with_pending(self) -> list[int]:
        cur = await self.conn.execute("SELECT DISTINCT user_id FROM feed WHERE notified = 0")
        return [row[0] for row in await cur.fetchall()]

    async def pending_feed(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM feed WHERE user_id = ? AND notified = 0 ORDER BY created_at DESC", (user_id,)
        )
        return list(await cur.fetchall())

    async def mark_notified(self, user_id: int) -> None:
        await self.conn.execute("UPDATE feed SET notified = 1 WHERE user_id = ? AND notified = 0", (user_id,))
        await self.conn.commit()

    @staticmethod
    def _feed_where(brand: str | None, grp: str | None, since: int | None,
                    view: str = "all") -> tuple[str, list]:
        """
        Условие для ленты. Всегда прячем скрытые вручную и проданные/снятые вещи.
        view: all — всё, new — только непросмотренные.
        """
        sql = (" AND f.hidden = 0 AND (json_extract(l.data, '$.status') IS NULL"
               " OR json_extract(l.data, '$.status') = '')")
        args: list = []
        if view == "new":
            sql += " AND f.seen = 0"
        if brand:
            sql += " AND f.keyword = ?"
            args.append(brand)
        if grp:
            sql += " AND f.grp = ?"
            args.append(grp)
        if since:
            sql += " AND f.created_at >= ?"
            args.append(since)
        return sql, args

    _FEED_FROM = "FROM feed f JOIN listings l ON l.source = f.source AND l.item_id = f.item_id"

    async def feed_page(self, user_id: int, brand: str | None = None, grp: str | None = None,
                        since: int | None = None, limit: int = 30, offset: int = 0,
                        view: str = "all") -> list[dict]:
        """Лента пользователя (новые сверху) вместе с данными объявлений."""
        where, args = self._feed_where(brand, grp, since, view)
        cur = await self.conn.execute(
            f"""
            SELECT f.source, f.item_id, f.keyword, f.grp, f.created_at, f.seen, l.data,
                   EXISTS(SELECT 1 FROM favorites v WHERE v.user_id = f.user_id
                          AND v.source = f.source AND v.item_id = f.item_id) AS fav
            {self._FEED_FROM}
            WHERE f.user_id = ?{where}
            ORDER BY f.created_at DESC, f.rowid DESC LIMIT ? OFFSET ?
            """,
            (user_id, *args, limit, offset),
        )
        rows = []
        for r in await cur.fetchall():
            rows.append({"source": r["source"], "item_id": r["item_id"], "keyword": r["keyword"],
                         "grp": r["grp"], "found_at": r["created_at"], "fav": bool(r["fav"]),
                         "seen": bool(r["seen"]), "data": json.loads(r["data"])})
        return rows

    async def feed_count(self, user_id: int, brand: str | None = None, grp: str | None = None,
                         since: int | None = None, view: str = "all") -> int:
        where, args = self._feed_where(brand, grp, since, view)
        cur = await self.conn.execute(f"SELECT COUNT(*) {self._FEED_FROM} WHERE f.user_id = ?{where}",
                                      (user_id, *args))
        return (await cur.fetchone())[0]

    async def feed_facets(self, user_id: int, since: int | None = None, view: str = "all") -> dict:
        """Сколько объявлений по брендам и разделам — для фильтров."""
        where, args = self._feed_where(None, None, since, view)
        result = {"brands": {}, "groups": {}}
        for column, key in (("keyword", "brands"), ("grp", "groups")):
            cur = await self.conn.execute(
                f"SELECT f.{column}, COUNT(*) {self._FEED_FROM} WHERE f.user_id = ?{where}"
                f" GROUP BY f.{column} ORDER BY 2 DESC",
                (user_id, *args),
            )
            result[key] = {row[0]: row[1] for row in await cur.fetchall()}
        return result

    async def mark_feed_seen(self, user_id: int, items: list[tuple[str, str]]) -> None:
        """Отметить объявления просмотренными (листал в чате или видел в приложении)."""
        if not items:
            return
        await self.conn.executemany(
            "UPDATE feed SET seen = 1 WHERE user_id = ? AND source = ? AND item_id = ?",
            [(user_id, s, str(i)) for s, i in items],
        )
        await self.conn.commit()

    async def mark_all_seen(self, user_id: int) -> None:
        await self.conn.execute("UPDATE feed SET seen = 1 WHERE user_id = ? AND seen = 0", (user_id,))
        await self.conn.commit()

    async def set_feed_hidden(self, user_id: int, source: str, item_id: str, hidden: bool) -> bool:
        cur = await self.conn.execute(
            "UPDATE feed SET hidden = ?, seen = 1 WHERE user_id = ? AND source = ? AND item_id = ?",
            (1 if hidden else 0, user_id, source, str(item_id)),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    # ---------------- Оплаты ----------------

    async def add_payment(self, charge_id: str, user_id: int, plan: str, months: int,
                          stars: int, recurring: bool, slot: str | None = None) -> bool:
        """Сохраняет оплату. False — если такую уже сохраняли (Telegram прислал повтор)."""
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO payments (charge_id, user_id, plan, months, stars, recurring, created_at, slot)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (charge_id, user_id, plan, months, stars, 1 if recurring else 0, _now(), slot),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def has_paid(self, user_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM payments WHERE user_id = ? AND refunded = 0 LIMIT 1", (user_id,)
        )
        return await cur.fetchone() is not None

    async def other_subscriptions(self, user_id: int, keep_charge_id: str) -> list[str]:
        """Платежи-подписки пользователя, кроме указанного (их нужно отменить при смене тарифа)."""
        cur = await self.conn.execute(
            "SELECT charge_id FROM payments WHERE user_id = ? AND recurring = 1 AND refunded = 0 "
            "AND charge_id != ? AND plan != 'own'",
            (user_id, keep_charge_id),
        )
        return [row[0] for row in await cur.fetchall()]

    async def stop_recurring(self, charge_id: str) -> None:
        await self.conn.execute("UPDATE payments SET recurring = 0 WHERE charge_id = ?", (charge_id,))
        await self.conn.commit()

    async def get_payment(self, charge_id: str) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM payments WHERE charge_id = ?", (charge_id,))
        return await cur.fetchone()

    async def mark_refunded(self, charge_id: str) -> None:
        await self.conn.execute("UPDATE payments SET refunded = 1, recurring = 0 WHERE charge_id = ?", (charge_id,))
        await self.conn.commit()

    async def list_payments(self, limit: int = 20) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,))
        return list(await cur.fetchall())

    # ---------------- Докупленные «свои» бренды ----------------

    async def extend_own_slot(self, slot_id: str, user_id: int, until: int, charge_id: str) -> None:
        """Слот «+1 свой бренд» оплачен до until (первая оплата или продление подписки)."""
        await self.conn.execute(
            """
            INSERT INTO own_slots (slot_id, user_id, until, charge_id) VALUES (?, ?, ?, ?)
            ON CONFLICT(slot_id) DO UPDATE SET until = MAX(until, excluded.until), charge_id = excluded.charge_id
            """,
            (slot_id, user_id, until, charge_id),
        )
        await self.conn.commit()

    async def own_slots(self, user_id: int) -> int:
        """Сколько слотов «+1 свой бренд» сейчас оплачено."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM own_slots WHERE user_id = ? AND until > ?", (user_id, _now())
        )
        return (await cur.fetchone())[0]

    async def own_slots_all(self) -> dict[int, int]:
        """user_id -> сколько слотов оплачено (для проверки лимитов в мониторинге)."""
        cur = await self.conn.execute(
            "SELECT user_id, COUNT(*) FROM own_slots WHERE until > ? GROUP BY user_id", (_now(),)
        )
        return {row[0]: row[1] for row in await cur.fetchall()}

    async def own_slot_until(self, user_id: int) -> int | None:
        """Когда закончится ближайший оплаченный слот."""
        cur = await self.conn.execute(
            "SELECT MIN(until) FROM own_slots WHERE user_id = ? AND until > ?", (user_id, _now())
        )
        return (await cur.fetchone())[0]

    async def own_slot_exists(self, slot_id: str) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM own_slots WHERE slot_id = ?", (slot_id,))
        return await cur.fetchone() is not None

    async def expire_own_slot(self, charge_id: str) -> str | None:
        """
        Возврат за слот: закрываем слот, к которому относится этот платёж
        (первый или любое продление). Возвращает последний charge_id слота —
        по нему отменяем подписку, — или None, если слот не нашёлся.
        """
        cur = await self.conn.execute("SELECT slot FROM payments WHERE charge_id = ?", (charge_id,))
        row = await cur.fetchone()
        if not row or not row["slot"]:
            return None
        cur = await self.conn.execute("SELECT charge_id FROM own_slots WHERE slot_id = ?", (row["slot"],))
        slot = await cur.fetchone()
        if not slot:
            return None
        await self.conn.execute("UPDATE own_slots SET until = 0 WHERE slot_id = ?", (row["slot"],))
        await self.conn.commit()
        return slot["charge_id"] or charge_id

    async def own_subscription_charges(self, user_id: int) -> list[str]:
        """Последние платежи действующих слотов (чтобы отменить подписки при возврате тарифа)."""
        cur = await self.conn.execute(
            "SELECT charge_id FROM own_slots WHERE user_id = ? AND until > ? AND charge_id IS NOT NULL",
            (user_id, _now()),
        )
        return [r[0] for r in await cur.fetchall()]

    async def expire_all_own_slots(self, user_id: int) -> None:
        await self.conn.execute("UPDATE own_slots SET until = 0 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def all_payments(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM payments ORDER BY created_at")
        return list(await cur.fetchall())

    async def stars_since(self, since: int) -> int:
        cur = await self.conn.execute(
            "SELECT COALESCE(SUM(stars), 0) FROM payments WHERE refunded = 0 AND created_at >= ?", (since,)
        )
        return (await cur.fetchone())[0]

    # ---------------- Уборка ----------------

    async def cleanup_seen(self, older_than_days: int = 60) -> None:
        """Удаляет очень старые записи, чтобы база не разрасталась."""
        border = _now() - older_than_days * 86400
        await self.conn.execute("DELETE FROM seen WHERE first_seen < ?", (border,))
        await self.conn.execute("DELETE FROM sent WHERE sent_at < ?", (border,))
        await self.conn.execute("DELETE FROM feed WHERE created_at < ?", (_now() - 14 * 86400,))
        await self.conn.execute("DELETE FROM assistant_msgs WHERE created_at < ?", (_now() - 30 * 86400,))
        # Объявления из избранного не удаляем
        await self.conn.execute(
            """
            DELETE FROM listings WHERE created_at < ? AND NOT EXISTS (
                SELECT 1 FROM favorites f
                WHERE f.source = listings.source AND f.item_id = listings.item_id
            )
            """,
            (border,),
        )
        await self.conn.commit()

    # ---------------- Отчётность (/report) ----------------
    # Всё ниже нужно только владельцу: учёт заходов и запросы для отчёта.
    # Дни считаем по Москве (см. msk_day / day_bounds вверху файла).
    # Админов в цифрах не считаем — иначе свои тесты раздувают статистику.
    # Исключение — деньги: оплаты считаем все (как /stats и /payments),
    # кроме возвращённых.

    @staticmethod
    def _no_admins(column: str) -> tuple[str, list]:
        """Кусок SQL «column не админ» и его параметры."""
        if not config.ADMIN_IDS:
            return "1 = 1", []
        placeholders = ",".join("?" for _ in config.ADMIN_IDS)
        return f"{column} NOT IN ({placeholders})", list(config.ADMIN_IDS)

    async def touch_activity(self, user_id: int, channel: str) -> None:
        """
        Отметить, что человек сегодня что-то делал в боте (channel='bot')
        или в приложении ('app'). Вызывается на КАЖДОЕ нажатие/запрос,
        поэтому это один дешёвый UPSERT: новая строка появляется раз в день,
        дальше только растёт счётчик. Ошибка здесь не должна ломать бота —
        статистика не важнее того, чтобы человек получил ответ.
        """
        now = _now()
        try:
            await self.conn.execute(
                """
                INSERT INTO activity (user_id, day, channel, events, first_at, last_at)
                VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(user_id, day, channel) DO UPDATE SET
                    events = events + 1, last_at = excluded.last_at
                """,
                (user_id, msk_day(now), channel, now, now),
            )
            await self.conn.execute("UPDATE users SET last_seen = ? WHERE user_id = ?", (now, user_id))
            await self.conn.commit()
        except Exception:
            log.exception("touch_activity %s %s", user_id, channel)

    async def report_activity(self, first: date, last: date) -> dict:
        """
        Уникальные активные люди за дни first..last: всего, только бот,
        только приложение, и там и там. Один человек считается один раз,
        даже если заходил каждый день.
        """
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"""
            SELECT COUNT(*) AS total,
                   COALESCE(SUM(b > 0 AND a = 0), 0) AS bot_only,
                   COALESCE(SUM(a > 0 AND b = 0), 0) AS app_only,
                   COALESCE(SUM(a > 0 AND b > 0), 0) AS both_ch
            FROM (
                SELECT user_id,
                       SUM(channel = 'bot') AS b,
                       SUM(channel = 'app') AS a
                FROM activity
                WHERE day BETWEEN ? AND ? AND {no_adm}
                GROUP BY user_id
            )
            """,
            (first.isoformat(), last.isoformat(), *args),
        )
        r = await cur.fetchone()
        return {"total": r["total"], "bot_only": r["bot_only"], "app_only": r["app_only"],
                "both": r["both_ch"], "bot": r["bot_only"] + r["both_ch"], "app": r["app_only"] + r["both_ch"]}

    async def report_dau(self, first: date, last: date) -> dict[str, int]:
        """Сколько разных людей заходило в каждый из дней ('2026-10-06' -> 12). Пустые дни = 0."""
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT day, COUNT(DISTINCT user_id) FROM activity "
            f"WHERE day BETWEEN ? AND ? AND {no_adm} GROUP BY day",
            (first.isoformat(), last.isoformat(), *args),
        )
        found = {row[0]: row[1] for row in await cur.fetchall()}
        result, d = {}, first
        while d <= last:
            result[d.isoformat()] = found.get(d.isoformat(), 0)
            d += timedelta(days=1)
        return result

    async def report_new_users(self, first: date, last: date) -> int:
        """Сколько новых людей впервые нажали /start (users.created_at) за эти дни."""
        start, end = day_bounds(first, last)
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT COUNT(*) FROM users WHERE created_at >= ? AND created_at < ? AND {no_adm}",
            (start, end, *args),
        )
        return (await cur.fetchone())[0]

    async def set_source(self, user_id: int, source: str) -> None:
        """
        Запоминает, откуда человек пришёл (ссылка t.me/<бот>?start=tt и т.п.).
        Пишем только ПЕРВЫЙ источник: если человек потом перейдёт по другой
        ссылке, «первое касание» не перезатрётся — так честнее считать соцсети.
        """
        await self.conn.execute(
            "UPDATE users SET source = ? WHERE user_id = ? AND (source IS NULL OR source = '')",
            (source, user_id),
        )
        await self.conn.commit()

    async def report_sources(self, first: date, last: date) -> list[tuple[str, int]]:
        """Новые люди за эти дни по источникам: [('tt', 5), ('ig', 3), ('—', 10)]."""
        start, end = day_bounds(first, last)
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT COALESCE(NULLIF(source, ''), '—') AS src, COUNT(*) AS n FROM users "
            f"WHERE created_at >= ? AND created_at < ? AND {no_adm} GROUP BY src ORDER BY n DESC",
            (start, end, *args),
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    async def report_trials_started(self, first: date, last: date) -> int:
        """
        Сколько человек включили пробный период за эти дни.
        Время включения (trial_at) пишется только с версии с отчётами —
        пробные, включённые раньше, здесь не видны.
        """
        start, end = day_bounds(first, last)
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT COUNT(*) FROM users WHERE trial_at >= ? AND trial_at < ? AND {no_adm}",
            (start, end, *args),
        )
        return (await cur.fetchone())[0]

    async def report_trials_ending(self, hours: int = 24) -> list[aiosqlite.Row]:
        """У кого пробный период кончится в ближайшие N часов (им можно написать и предложить тариф)."""
        now = _now()
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT user_id, username, first_name, sub_until FROM users "
            f"WHERE plan = 'trial' AND sub_until > ? AND sub_until <= ? AND {no_adm} "
            f"ORDER BY sub_until",
            (now, now + hours * 3600, *args),
        )
        return list(await cur.fetchall())

    async def report_payments(self, first: date, last: date) -> dict:
        """
        Оплаты за дни first..last (без возвращённых): сколько, сколько звёзд,
        по тарифам, и сколько из них первые оплаты человека, а сколько — повторные
        (продление подписки, смена тарифа, докупка бренда).
        «Первая» = у этого человека нет более ранней невозвращённой оплаты.
        """
        start, end = day_bounds(first, last)
        cur = await self.conn.execute(
            """
            SELECT p.plan, p.stars, p.months, p.created_at, p.user_id,
                   u.username, u.first_name,
                   EXISTS(
                       SELECT 1 FROM payments q
                       WHERE q.user_id = p.user_id AND q.refunded = 0
                         AND (q.created_at < p.created_at
                              OR (q.created_at = p.created_at AND q.rowid < p.rowid))
                   ) AS repeat
            FROM payments p
            LEFT JOIN users u ON u.user_id = p.user_id
            WHERE p.refunded = 0 AND p.created_at >= ? AND p.created_at < ?
            ORDER BY p.created_at DESC
            """,
            (start, end),
        )
        # people — кто именно заплатил (свежие сверху), чтобы в /report было видно имена
        result = {"count": 0, "stars": 0, "first": 0, "repeat": 0, "by_plan": {}, "people": []}
        for r in await cur.fetchall():
            result["people"].append(r)
            result["count"] += 1
            result["stars"] += r["stars"]
            result["repeat" if r["repeat"] else "first"] += 1
            count, stars = result["by_plan"].get(r["plan"], (0, 0))
            result["by_plan"][r["plan"]] = (count + 1, stars + r["stars"])
        return result

    async def report_active_subs(self) -> dict[str, int]:
        """Сколько людей с доступом прямо сейчас, по тарифам ('pro' -> 5). Пробный — тоже тут."""
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT COALESCE(plan, 'pro') AS plan, COUNT(*) FROM users "
            f"WHERE sub_until > ? AND {no_adm} GROUP BY 1 ORDER BY 2 DESC",
            (_now(), *args),
        )
        return {row[0]: row[1] for row in await cur.fetchall()}

    async def report_expiring(self, days: int = 3) -> list[aiosqlite.Row]:
        """
        Платные подписки, которые кончаются в ближайшие N дней.
        auto = 1 — у человека есть подписка с автопродлением: Telegram, скорее
        всего, сам спишет звёзды, писать ему не обязательно.
        """
        now = _now()
        no_adm, args = self._no_admins("u.user_id")
        cur = await self.conn.execute(
            f"""
            SELECT u.user_id, u.username, u.first_name, u.plan, u.sub_until,
                   EXISTS(SELECT 1 FROM payments p WHERE p.user_id = u.user_id
                          AND p.recurring = 1 AND p.refunded = 0 AND p.plan != 'own') AS auto
            FROM users u
            WHERE COALESCE(u.plan, 'pro') != 'trial' AND u.sub_until > ? AND u.sub_until <= ? AND {no_adm}
            ORDER BY u.sub_until
            """,
            (now, now + days * 86400, *args),
        )
        return list(await cur.fetchall())

    async def report_conversion(self, days: int = 30) -> tuple[int, int]:
        """
        Конверсия пробный → оплата: (сколько включили пробный за N дней,
        сколько из них потом хоть раз заплатили за тариф).
        Для старых пользователей без trial_at берём дату регистрации —
        обычно пробный включают в первый же день.
        """
        no_adm, args = self._no_admins("u.user_id")
        cur = await self.conn.execute(
            f"""
            SELECT COUNT(*),
                   COALESCE(SUM(EXISTS(SELECT 1 FROM payments p WHERE p.user_id = u.user_id
                                       AND p.refunded = 0 AND p.plan != 'own')), 0)
            FROM users u
            WHERE u.trial_used = 1 AND COALESCE(u.trial_at, u.created_at) >= ? AND {no_adm}
            """,
            (_now() - days * 86400, *args),
        )
        row = await cur.fetchone()
        return row[0], row[1]

    async def report_usage(self, first: date, last: date) -> dict:
        """
        Расход ИИ-функций.
        • За дни first..last — сообщения ИИ-помощнику и в боте, и в приложении:
          история у них общая и хранится с временем (таблица assistant_msgs).
          Сообщения бота до этого обновления не сохранялись — их здесь нет.
        • Таблица usage считает по месяцам (period '2026-10'), поэтому легит-чеки,
          сравнения цен и помощник (бот + приложение) — итогом за месяц дня last.
        """
        start, end = day_bounds(first, last)
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT user_id) FROM assistant_msgs "
            f"WHERE role = 'user' AND created_at >= ? AND created_at < ? AND {no_adm}",
            (start, end, *args),
        )
        msgs, people = await cur.fetchone()
        cur = await self.conn.execute(
            f"SELECT kind, SUM(count), COUNT(DISTINCT user_id) FROM usage "
            f"WHERE period = ? AND {no_adm} GROUP BY kind",
            (last.strftime("%Y-%m"), *args),
        )
        month = {row[0]: (row[1], row[2]) for row in await cur.fetchall()}
        return {"app_msgs": msgs, "app_users": people, "month": month}

    async def report_top_brands(self, limit: int = 10) -> list[tuple[str, int]]:
        """Самые отслеживаемые бренды: (бренд, сколько человек его отслеживают)."""
        no_adm, args = self._no_admins("user_id")
        cur = await self.conn.execute(
            f"SELECT keyword, COUNT(*) FROM watches WHERE {no_adm} "
            f"GROUP BY keyword ORDER BY 2 DESC, keyword LIMIT ?",
            (*args, limit),
        )
        return [(row[0], row[1]) for row in await cur.fetchall()]

    async def report_active_users(self, day: date, limit: int = 50) -> list[aiosqlite.Row]:
        """
        Кто заходил в этот день: имя, тариф, был ли в боте / приложении,
        время последнего действия. Свежие сверху. is_new = 1 — пришёл в этот же день.
        """
        start, end = day_bounds(day)
        no_adm, args = self._no_admins("a.user_id")
        cur = await self.conn.execute(
            f"""
            SELECT a.user_id, u.username, u.first_name, u.plan, u.sub_until,
                   MAX(a.channel = 'bot') AS bot, MAX(a.channel = 'app') AS app,
                   MAX(a.last_at) AS last_at, SUM(a.events) AS events,
                   (u.created_at >= ? AND u.created_at < ?) AS is_new
            FROM activity a
            LEFT JOIN users u ON u.user_id = a.user_id
            WHERE a.day = ? AND {no_adm}
            GROUP BY a.user_id
            ORDER BY last_at DESC
            LIMIT ?
            """,
            (start, end, day.isoformat(), *args, limit),
        )
        return list(await cur.fetchall())
