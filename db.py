"""
Работа с базой данных SQLite.

Таблицы:
  users   — пользователи бота (и до какого числа у них подписка)
  watches — бренды/запросы, которые отслеживает пользователь
  seen    — объявления, которые бот уже видел (чтобы не слать повторно)
"""

import time

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
    source      TEXT NOT NULL,                -- goofish / fen95
    keyword     TEXT NOT NULL,
    item_id     TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    PRIMARY KEY (source, keyword, item_id)
);

CREATE INDEX IF NOT EXISTS idx_watches_keyword ON watches (keyword);
CREATE INDEX IF NOT EXISTS idx_seen_time ON seen (first_seen);
"""


def _now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: str = config.DB_PATH):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    # ---------------- Пользователи ----------------

    async def upsert_user(self, user_id: int, username: str | None, first_name: str | None) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (user_id, username, first_name, _now()),
        )
        await self.conn.commit()

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

    async def has_access(self, user_id: int) -> bool:
        """Админ — всегда. Остальные — пока не закончилась подписка."""
        if user_id in config.ADMIN_IDS:
            return True
        user = await self.get_user(user_id)
        return bool(user and user["sub_until"] > _now())

    async def grant(self, user_id: int, days: int) -> int:
        """Выдать/продлить доступ на N дней. Возвращает новое время окончания."""
        user = await self.get_user(user_id)
        if user is None:
            await self.conn.execute(
                "INSERT INTO users (user_id, created_at) VALUES (?, ?)", (user_id, _now())
            )
            start = _now()
        else:
            # Если подписка ещё идёт — продлеваем от её конца, иначе от сегодня
            start = max(user["sub_until"], _now())
        until = start + days * 86400
        await self.conn.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (until, user_id))
        await self.conn.commit()
        return until

    async def revoke(self, user_id: int) -> None:
        await self.conn.execute("UPDATE users SET sub_until = 0 WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def set_paused(self, user_id: int, paused: bool) -> None:
        await self.conn.execute(
            "UPDATE users SET paused = ? WHERE user_id = ?", (1 if paused else 0, user_id)
        )
        await self.conn.commit()

    async def list_users(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            """
            SELECT u.*, (SELECT COUNT(*) FROM watches w WHERE w.user_id = u.user_id) AS brands
            FROM users u ORDER BY u.created_at DESC
            """
        )
        return list(await cur.fetchall())

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

    async def list_watches(self, user_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM watches WHERE user_id = ? ORDER BY created_at", (user_id,)
        )
        return list(await cur.fetchall())

    async def count_watches(self, user_id: int) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) FROM watches WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0]

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
        Все бренды тех пользователей, у кого есть доступ и не стоит пауза.
        Именно по ним монитор делает запросы.
        """
        admin_ids = config.ADMIN_IDS or [-1]
        placeholders = ",".join("?" for _ in admin_ids)
        cur = await self.conn.execute(
            f"""
            SELECT w.* FROM watches w
            JOIN users u ON u.user_id = w.user_id
            WHERE u.paused = 0
              AND (u.user_id IN ({placeholders}) OR u.sub_until > ?)
            """,
            (*admin_ids, _now()),
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

    async def cleanup_seen(self, older_than_days: int = 60) -> None:
        """Удаляет очень старые записи, чтобы база не разрасталась."""
        border = _now() - older_than_days * 86400
        await self.conn.execute("DELETE FROM seen WHERE first_seen < ?", (border,))
        await self.conn.commit()
