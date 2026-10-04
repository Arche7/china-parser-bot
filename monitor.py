"""
Монитор — «сердце» бота.

Раз в CHECK_INTERVAL_MIN минут:
  1. Берёт все бренды всех активных пользователей.
  2. Объединяет одинаковые (если 5 человек следят за «nike», запрос к Apify
     будет один, а не пять — это экономит деньги).
  3. Для каждого бренда спрашивает у площадки свежие объявления.
  4. Находит те, которых ещё не видел, и рассылает их подписчикам этого бренда
     (с учётом их фильтра по цене).

При самом первом запросе по новому бренду бот ничего не рассылает, а просто
запоминает текущие объявления — иначе на тебя вывалилась бы куча старых.
"""

import asyncio
import html
import logging
import time
from collections import defaultdict

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
from db import Database
from sources.base import Listing, Source

log = logging.getLogger(__name__)


def format_price(listing: Listing) -> str:
    if listing.price is None:
        return "цена не указана"
    price = f"{listing.price:,.0f}".replace(",", " ")
    if listing.currency.upper() in ("CNY", "RMB", "¥"):
        rub = f"{listing.price * config.CNY_RUB_RATE:,.0f}".replace(",", " ")
        return f"¥{price} (≈ {rub} ₽)"
    return f"{price} {listing.currency}"


def price_matches(listing: Listing, price_min: float | None, price_max: float | None) -> bool:
    """Подходит ли объявление под фильтр цены пользователя."""
    if price_min is None and price_max is None:
        return True
    if listing.price is None:
        return False  # с фильтром цены объявления без цены не шлём
    if price_min is not None and listing.price < price_min:
        return False
    if price_max is not None and listing.price > price_max:
        return False
    return True


def build_caption(listing: Listing, source_title: str, keyword: str, header: str = "🆕") -> str:
    title = listing.title
    if len(title) > 400:
        title = title[:400] + "…"
    lines = [
        f"{header} <b>{html.escape(source_title)}</b> · бренд: <b>{html.escape(keyword)}</b>",
        "",
        html.escape(title),
        "",
        f"💰 {format_price(listing)}",
    ]
    place = []
    if listing.city:
        place.append(f"📍 {html.escape(str(listing.city))}")
    if listing.seller:
        place.append(f"👤 {html.escape(str(listing.seller))}")
    if place:
        lines.append("  ·  ".join(place))
    if listing.extra.get("free_shipping"):
        lines.append("🚚 Бесплатная доставка по Китаю")
    if listing.posted_at:
        lines.append(f"🕒 {html.escape(str(listing.posted_at))}")
    caption = "\n".join(lines)
    return caption[:1020]  # лимит подписи к фото в Telegram — 1024 символа


class Monitor:
    def __init__(self, bot: Bot, db: Database, sources: list[Source]):
        self.bot = bot
        self.db = db
        self.sources = [s for s in sources if s.enabled]
        self.source_titles = {s.name: s.title for s in sources}
        # Не больше 3 запусков актора одновременно
        self._semaphore = asyncio.Semaphore(3)
        # Чтобы один и тот же бренд не проверялся двумя задачами одновременно
        self._locks: dict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)
        # Статистика с момента запуска (для /stats)
        self.started_at = time.time()
        self.cycles = 0
        self.searches = 0
        self.items_fetched = 0
        self.messages_sent = 0
        self.last_cycle_at: float | None = None

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info(
            "Монитор запущен: интервал %d мин, площадки: %s",
            config.CHECK_INTERVAL_MIN,
            ", ".join(s.title for s in self.sources) or "нет активных",
        )
        await asyncio.sleep(5)
        while True:
            try:
                await self.check_all()
            except Exception:
                log.exception("Ошибка в цикле мониторинга")
            await asyncio.sleep(config.CHECK_INTERVAL_MIN * 60)

    async def check_all(self) -> None:
        watches = await self.db.active_watches()
        by_keyword: dict[str, list] = defaultdict(list)
        for watch in watches:
            by_keyword[watch["keyword"]].append(watch)

        log.info("Проверка: %d уникальных брендов", len(by_keyword))
        tasks = [
            self.check_keyword(source, keyword, keyword_watches)
            for source in self.sources
            for keyword, keyword_watches in by_keyword.items()
        ]
        if tasks:
            await asyncio.gather(*tasks)

        self.cycles += 1
        self.last_cycle_at = time.time()
        # Раз в ~сутки чистим старые записи
        if self.cycles % max(1, (24 * 60) // max(1, config.CHECK_INTERVAL_MIN)) == 0:
            await self.db.cleanup_seen()

    async def check_keyword(self, source: Source, keyword: str, watches: list) -> list[Listing]:
        """Проверить один бренд на одной площадке и разослать новинки."""
        async with self._locks[(source.name, keyword)]:
            async with self._semaphore:
                try:
                    listings = await source.search(keyword, config.MAX_ITEMS)
                except NotImplementedError:
                    return []
                except Exception as e:
                    log.warning("%s: ошибка поиска «%s»: %s", source.title, keyword, e)
                    return []

            self.searches += 1
            self.items_fetched += len(listings)

            # Убираем дубли внутри одной выдачи
            unique: dict[str, Listing] = {}
            for listing in listings:
                unique.setdefault(listing.id, listing)
            listings = list(unique.values())
            if not listings:
                return []

            first_time = not await self.db.has_any_seen(source.name, keyword)
            unseen_ids = await self.db.filter_unseen(
                source.name, keyword, [l.id for l in listings]
            )
            new_listings = [l for l in listings if l.id in unseen_ids]
            await self.db.mark_seen(source.name, keyword, [l.id for l in new_listings])

            if first_time:
                log.info("%s: «%s» — первый запрос, запомнил %d объявлений",
                         source.title, keyword, len(new_listings))
                return listings

            if new_listings:
                log.info("%s: «%s» — новых объявлений: %d",
                         source.title, keyword, len(new_listings))

            # Выдача идёт от новых к старым — шлём в хронологическом порядке
            for listing in reversed(new_listings):
                for watch in watches:
                    if price_matches(listing, watch["price_min"], watch["price_max"]):
                        await self.send_listing(watch["user_id"], listing, keyword)
            return listings

    # ------------------------------------------------------------------
    # Предпросмотр при добавлении нового бренда
    # ------------------------------------------------------------------

    async def preview_new_keyword(
        self, user_id: int, keyword: str, price_min: float | None, price_max: float | None
    ) -> None:
        """
        Вызывается сразу после добавления бренда. Если этот бренд ещё никто
        не отслеживал — делаем первый запрос прямо сейчас и показываем
        пользователю 3 самых свежих объявления, чтобы он видел, что всё работает.
        """
        for source in self.sources:
            if await self.db.has_any_seen(source.name, keyword):
                continue  # бренд уже отслеживается — новинки придут сами
            watches = [w for w in await self.db.active_watches() if w["keyword"] == keyword]
            listings = await self.check_keyword(source, keyword, watches)
            suitable = [l for l in listings if price_matches(l, price_min, price_max)][:3]
            if not suitable:
                await self._safe_send_text(
                    user_id,
                    f"🔎 {html.escape(source.title)}: по запросу <b>{html.escape(keyword)}</b> "
                    "сейчас ничего подходящего не нашлось. Как только появятся новые "
                    "объявления — пришлю.",
                )
                continue
            await self._safe_send_text(
                user_id,
                f"👀 {html.escape(source.title)}: вот что есть по <b>{html.escape(keyword)}</b> "
                "прямо сейчас. Дальше буду присылать только <b>новые</b> объявления.",
            )
            for listing in suitable:
                await self.send_listing(user_id, listing, keyword, header="📌")

    # ------------------------------------------------------------------
    # Отправка сообщений
    # ------------------------------------------------------------------

    async def send_listing(self, user_id: int, listing: Listing, keyword: str, header: str = "🆕") -> None:
        caption = build_caption(
            listing, self.source_titles.get(listing.source, listing.source), keyword, header
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔗 Открыть объявление", url=listing.url)]]
        )

        for attempt in range(2):
            try:
                if listing.image:
                    try:
                        await self.bot.send_photo(
                            user_id, photo=listing.image, caption=caption, reply_markup=keyboard
                        )
                    except TelegramBadRequest:
                        # Telegram не смог скачать фото — отправим просто текстом
                        await self.bot.send_message(user_id, caption, reply_markup=keyboard)
                else:
                    await self.bot.send_message(user_id, caption, reply_markup=keyboard)
                self.messages_sent += 1
                await asyncio.sleep(0.1)  # не спамим Telegram слишком быстро
                return
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                # Пользователь заблокировал бота — ставим его на паузу
                log.info("Пользователь %s заблокировал бота — пауза", user_id)
                await self.db.set_paused(user_id, True)
                return
            except Exception as e:
                log.warning("Не удалось отправить объявление %s пользователю %s: %s",
                            listing.id, user_id, e)
                return

    async def _safe_send_text(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as e:
            log.warning("Не удалось отправить сообщение %s: %s", user_id, e)
