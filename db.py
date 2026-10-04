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

Старая база (от прошлой версии бота) обновляется сама при запуске:
новые столбцы и таблицы добавляются, старые данные не трогаются.
"""

import json
import time
from datetime import datetime

import aiosqlite

import config

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

CREATE INDEX IF NOT EXISTS idx_watches_keyword ON watches (keyword);
CREATE INDEX IF NOT EXISTS idx_sent_time ON sent (sent_at);
CREATE INDEX IF NOT EXISTS idx_seen_time ON seen (first_seen);
CREATE INDEX IF NOT EXISTS idx_listings_time ON listings (created_at);
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
]

# Настройки пользователя по умолчанию
DEFAULT_SETTINGS = {
    "card": "compact",          # compact / full — вид карточки объявления
    "title": "ru",              # ru — перевод, orig — как на площадке
    "quiet": False,             # тихие часы 00:00–08:00 (без звука)
    "delivery": config.DELIVERY_RUB_PER_KG,   # ₽ за кг
    "fee": config.BUYER_FEE_PCT,              # комиссия байера, %
}


def _now() -> int:
    return int(time.time())


def _period() -> str:
    return datetime.now().strftime("%Y-%m")


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
            # Пробный период не суммируем с платным: платный начинается сегодня
            if user["plan"] == "trial" and plan and plan != "trial":
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

    async def start_trial(self, user_id: int, days: int) -> int | None:
        """Включает пробный период один раз. None — если уже был."""
        user = await self.get_user(user_id)
        if not user or user["trial_used"] or user["sub_until"] > _now():
            return None
        until = _now() + days * 86400
        await self.conn.execute(
            "UPDATE users SET sub_until = ?, plan = 'trial', trial_used = 1 WHERE user_id = ?",
            (until, user_id),
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

    # ---------------- Уборка ----------------

    async def cleanup_seen(self, older_than_days: int = 60) -> None:
        """Удаляет очень старые записи, чтобы база не разрасталась."""
        border = _now() - older_than_days * 86400
        await self.conn.execute("DELETE FROM seen WHERE first_seen < ?", (border,))
        await self.conn.execute("DELETE FROM sent WHERE sent_at < ?", (border,))
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
