"""
Монитор — «сердце» бота.

Раз в CHECK_INTERVAL_MIN минут:
  1. Берёт все бренды всех активных пользователей.
  2. Объединяет одинаковые (если 5 человек следят за «gucci 200–1800»,
     запрос к Apify будет один, а не пять — это экономит деньги).
     Разные написания одного бренда (lv / 路易威登 / louis vuitton)
     тоже считаются одним брендом — см. brands.py.
  3. Для каждого бренда ищет свежие объявления по 1–2 названиям
     (латиница + по-китайски). Фильтр цены передаётся прямо в Apify,
     поэтому мы не платим за объявления, которые всё равно не подходят.
  4. Отсеивает мусор: заголовки без названия бренда и пометки подделок
     (高仿, 复刻, A货 …).
  5. Находит то, чего ещё не видел, и рассылает подписчикам.

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

import brands
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
    """Подходит ли объявление под фильтр цены."""
    if price_min is None and price_max is None:
        return True
    if listing.price is None:
        return False  # с фильтром цены объявления без цены не шлём
    if price_min is not None and listing.price < price_min:
        return False
    if price_max is not None and listing.price > price_max:
        return False
    return True


def seen_key(query: str, price_min: float | None, price_max: float | None) -> str:
    """
    Под каким именем запоминать уже виденные объявления.
    'gucci' без цены -> 'gucci', с ценой 200–1800 -> 'gucci [200-1800]'.
    (Без цены имя такое же, как в старой версии бота, — ничего не теряется.)
    """
    if price_min is None and price_max is None:
        return query
    low = "" if price_min is None else f"{price_min:g}"
    high = "" if price_max is None else f"{price_max:g}"
    return f"{query} [{low}-{high}]"


def build_caption(listing: Listing, source_title: str, keyword: str, header: str = "🆕") -> str:
    title = listing.title
    if len(title) > 400:
        title = title[:400] + "…"
    lines = [
        f"{header} <b>{html.escape(source_title)}</b> · бренд: "
        f"<b>{html.escape(brands.display_name(keyword))}</b>",
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
        # Чтобы одно объявление не ушло человеку дважды
        self._send_lock = asyncio.Lock()
        # Чтобы один и тот же бренд не проверялся двумя задачами одновременно
        self._locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)
        # Статистика с момента запуска (для /stats)
        self.started_at = time.time()
        self.cycles = 0
        self.searches = 0
        self.items_fetched = 0
        self.items_filtered = 0
        self.messages_sent = 0
        self.last_cycle_at: float | None = None

    # ------------------------------------------------------------------
    # Основной цикл
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info(
            "Монитор запущен: интервал %d мин, объявлений за запрос: %d, площадки: %s",
            config.CHECK_INTERVAL_MIN,
            config.MAX_ITEMS,
            ", ".join(s.title for s in self.sources) or "нет активных",
        )
        await asyncio.sleep(5)
        while True:
            try:
                await self.check_all()
            except Exception:
                log.exception("Ошибка в цикле мониторинга")
            await asyncio.sleep(config.CHECK_INTERVAL_MIN * 60)

    @staticmethod
    def group_watches(watches: list) -> dict[tuple, list]:
        """
        Объединяет подписки в «задания»: (бренд, цена от, цена до) -> подписчики.
        'lv' и '路易威登' превращаются в один бренд 'louis vuitton'.
        """
        jobs: dict[tuple, list] = defaultdict(list)
        for watch in watches:
            key = (brands.canonical(watch["keyword"]), watch["price_min"], watch["price_max"])
            jobs[key].append(watch)
        return jobs

    async def check_all(self) -> None:
        jobs = self.group_watches(await self.db.active_watches())
        queries = sum(len(brands.search_queries(keyword)) for keyword, _, _ in jobs)
        log.info("Проверка: %d брендов, %d поисковых запросов", len(jobs), queries)

        tasks = [
            self.check_job(source, keyword, price_min, price_max, job_watches)
            for source in self.sources
            for (keyword, price_min, price_max), job_watches in jobs.items()
        ]
        if tasks:
            await asyncio.gather(*tasks)

        self.cycles += 1
        self.last_cycle_at = time.time()
        # Раз в ~сутки чистим старые записи
        if self.cycles % max(1, (24 * 60) // max(1, config.CHECK_INTERVAL_MIN)) == 0:
            await self.db.cleanup_seen()

    async def _search(
        self, source: Source, query: str, price_min: float | None, price_max: float | None
    ) -> list[Listing] | None:
        """Один запрос к площадке. None — если запрос не удался."""
        async with self._semaphore:
            try:
                listings = await source.search(
                    query, config.MAX_ITEMS, price_min=price_min, price_max=price_max
                )
            except NotImplementedError:
                return None
            except Exception as e:
                log.warning("%s: ошибка поиска «%s»: %s", source.title, query, e)
                return None
        self.searches += 1
        self.items_fetched += len(listings)
        return listings

    async def check_job(
        self,
        source: Source,
        keyword: str,
        price_min: float | None,
        price_max: float | None,
        watches: list,
    ) -> list[Listing]:
        """
        Проверить один бренд (во всех написаниях) на одной площадке
        и разослать новинки. Возвращает все подходящие объявления
        (нужно для предпросмотра).
        """
        queries = brands.search_queries(keyword)
        keys = [seen_key(q, price_min, price_max) for q in queries]

        async with self._locks[(source.name, keyword, price_min, price_max)]:
            # 1. Ищем по каждому написанию и складываем всё в одну кучу
            fetched: dict[str, Listing] = {}
            for query in queries:
                result = await self._search(source, query, price_min, price_max)
                for listing in result or []:
                    fetched.setdefault(listing.id, listing)
            if not fetched:
                return []

            # 2. Какие из них мы ещё НИ РАЗУ не видели ни по одному написанию
            first_time = True
            for key in keys:
                if await self.db.has_any_seen(source.name, key):
                    first_time = False
                    break
            ids = list(fetched)
            unseen = set(ids)
            for key in keys:
                unseen &= await self.db.filter_unseen(source.name, key, ids)
            for key in keys:
                await self.db.mark_seen(source.name, key, ids)

            # 3. Отсеиваем мусор и подделки
            def suitable(listing: Listing) -> bool:
                return price_matches(listing, price_min, price_max) and brands.listing_ok(
                    keyword, listing.title
                )

            good = [l for l in fetched.values() if suitable(l)]
            label = f"{brands.display_name(keyword)} ({' / '.join(queries)})"

            if first_time:
                log.info("%s: %s — первый запрос, запомнил %d объявлений",
                         source.title, label, len(fetched))
                return good

            new_all = [l for l in fetched.values() if l.id in unseen]
            new_good = [l for l in new_all if suitable(l)]
            self.items_filtered += len(new_all) - len(new_good)
            if new_all:
                log.info("%s: %s — новых: %d, после фильтров: %d",
                         source.title, label, len(new_all), len(new_good))

            # Выдача идёт от новых к старым — шлём в хронологическом порядке
            for listing in reversed(new_good):
                for watch in watches:
                    if price_matches(listing, watch["price_min"], watch["price_max"]):
                        await self.send_listing(watch["user_id"], listing, keyword)
            return good

    # ------------------------------------------------------------------
    # Предпросмотр при добавлении нового бренда
    # ------------------------------------------------------------------

    async def preview_new_keyword(
        self,
        user_id: int,
        keyword: str,
        price_min: float | None,
        price_max: float | None,
        show: bool = True,
    ) -> None:
        """
        Вызывается сразу после добавления бренда. Если этот бренд (с такой
        ценой) ещё никто не отслеживал — делаем первый запрос прямо сейчас.
        show=True  — показываем пользователю 3 самых свежих объявления;
        show=False — просто молча запоминаем текущие (для /preset, чтобы
                     не завалить чат сразу двадцатью сообщениями).
        """
        keyword = brands.canonical(keyword)
        name = html.escape(brands.display_name(keyword))
        keys = [seen_key(q, price_min, price_max) for q in brands.search_queries(keyword)]

        for source in self.sources:
            already = False
            for key in keys:
                if await self.db.has_any_seen(source.name, key):
                    already = True
                    break
            if already:
                continue  # бренд уже отслеживается — новинки придут сами

            jobs = self.group_watches(await self.db.active_watches())
            watches = jobs.get((keyword, price_min, price_max), [])
            listings = await self.check_job(source, keyword, price_min, price_max, watches)
            if not show:
                continue

            suitable = listings[:3]
            if not suitable:
                await self._safe_send_text(
                    user_id,
                    f"🔎 {html.escape(source.title)}: по <b>{name}</b> сейчас ничего "
                    "подходящего не нашлось. Как только появятся новые объявления — пришлю.",
                )
                continue
            await self._safe_send_text(
                user_id,
                f"👀 {html.escape(source.title)}: вот что есть по <b>{name}</b> "
                "прямо сейчас. Дальше буду присылать только <b>новые</b> объявления.",
            )
            for listing in suitable:
                await self.send_listing(user_id, listing, keyword, header="📌")

    # ------------------------------------------------------------------
    # Отправка сообщений
    # ------------------------------------------------------------------

    async def send_listing(self, user_id: int, listing: Listing, keyword: str, header: str = "🆕") -> None:
        async with self._send_lock:
            if await self.db.was_sent(user_id, listing.source, listing.id):
                return
            if await self._deliver(user_id, listing, keyword, header):
                await self.db.mark_sent(user_id, listing.source, listing.id)

    async def _deliver(self, user_id: int, listing: Listing, keyword: str, header: str) -> bool:
        """Отправляет одно объявление. True — если сообщение дошло."""
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
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                # Пользователь заблокировал бота — ставим его на паузу
                log.info("Пользователь %s заблокировал бота — пауза", user_id)
                await self.db.set_paused(user_id, True)
                return False
            except Exception as e:
                log.warning("Не удалось отправить объявление %s пользователю %s: %s",
                            listing.id, user_id, e)
                return False
        return False

    async def _safe_send_text(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as e:
            log.warning("Не удалось отправить сообщение %s: %s", user_id, e)
