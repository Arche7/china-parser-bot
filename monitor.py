"""
Монитор — «сердце» бота.

Каждые TICK_MIN минут (по умолчанию 5):
  1. Берёт все включённые бренды всех пользователей с доступом.
  2. Объединяет одинаковые бренды в одно «задание»: если 20 человек
     следят за Stone Island — запрос к Apify один, а не двадцать.
     Даже с разными ценами: ищем по общему диапазону цен, а потом
     каждому присылаем только то, что подходит под его фильтр.
  3. Решает, пора ли проверять задание. Интервал зависит от тарифа
     самого «быстрого» подписчика (ELITE — 10 мин, PRO — 15, START — 30),
     а «свои» бренды — не чаще раза в 30 минут.
  4. Умная экономия: если по бренду несколько раз подряд не было
     новинок — проверяем его реже (до ×3). Как только новинка появилась —
     возвращаемся к обычному интервалу. Если за одну проверку пришло
     столько новых, сколько мы запросили (значит, могли что-то упустить), —
     в следующий раз берём больше объявлений.
  5. Отсеивает мусор и подделки, переводит заголовки и рассылает карточки.

При самом первом запросе по бренду бот ничего не рассылает, а просто
запоминает текущие объявления — иначе на тебя вывалилась бы куча старых.
"""

import asyncio
import dataclasses
import html
import logging
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

import ai
import brands
import cards
import config
import plans
import rates
from db import Database
from decoder import decode, group_of
from sources.base import Listing, Source

log = logging.getLogger(__name__)

try:
    TZ = ZoneInfo("Europe/Moscow")
except Exception:  # на сервере нет базы часовых поясов — не страшно
    TZ = None

MAX_ITEMS_CAP = 20


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
    """
    if price_min is None and price_max is None:
        return query
    low = "" if price_min is None else f"{price_min:g}"
    high = "" if price_max is None else f"{price_max:g}"
    return f"{query} [{low}-{high}]"


def union_range(watches: list) -> tuple[float | None, float | None]:
    """Общий диапазон цен для всех подписчиков бренда."""
    mins = [w["price_min"] for w in watches]
    maxs = [w["price_max"] for w in watches]
    low = None if any(m is None for m in mins) else min(mins)
    high = None if any(m is None for m in maxs) else max(maxs)
    return low, high


def watch_interval(watch) -> int:
    """Как часто проверять бренд для конкретного подписчика, минут."""
    plan = plans.ADMIN if watch["is_admin"] else plans.get_plan(watch["plan"])
    interval = plan.interval_min
    if not brands.is_catalog(watch["keyword"]):
        interval = max(interval, plan.own_interval_min)
    return max(interval, config.CHECK_INTERVAL_MIN)


def is_quiet_now() -> bool:
    now = datetime.now(TZ) if TZ else datetime.now()
    return now.hour < 8


class Monitor:
    def __init__(self, bot: Bot, db: Database, sources: list[Source]):
        self.bot = bot
        self.db = db
        self.sources = [s for s in sources if s.enabled]
        self.source_titles = {s.name: s.title for s in sources}
        self._semaphore = asyncio.Semaphore(3)      # не больше 3 запусков актора одновременно
        self._send_lock = asyncio.Lock()            # одно объявление не уйдёт человеку дважды
        self._locks: dict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._settings_cache: dict[int, tuple[float, dict]] = {}
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
            "Монитор запущен: шаг %d мин, мин. интервал %d мин, площадки: %s",
            config.TICK_MIN,
            config.CHECK_INTERVAL_MIN,
            ", ".join(s.title for s in self.sources) or "нет активных",
        )
        await asyncio.sleep(5)
        while True:
            try:
                await rates.refresh()
                await self.check_due()
                await self.send_digests()
            except Exception:
                log.exception("Ошибка в цикле мониторинга")
            await asyncio.sleep(config.TICK_MIN * 60)

    @staticmethod
    def group_watches(watches: list) -> dict[str, list]:
        """Бренд -> его подписчики. 'lv' и '路易威登' — один бренд 'louis vuitton'."""
        jobs: dict[str, list] = defaultdict(list)
        for watch in watches:
            jobs[brands.canonical(watch["keyword"])].append(watch)
        return jobs

    @staticmethod
    def job_interval(watches: list) -> int:
        return min(watch_interval(w) for w in watches)

    async def check_due(self) -> None:
        jobs = self.group_watches(await self.db.active_watches())
        now = time.time()
        due = []
        for source in self.sources:
            for keyword, job_watches in jobs.items():
                state = await self.db.get_job(f"{source.name}|{keyword}")
                interval = self.job_interval(job_watches) * 60
                if state:
                    streak = state["empty_streak"]
                    factor = min(config.ADAPTIVE_MAX_FACTOR, 1 + 0.5 * max(0, streak - 1))
                    if now - state["last_check"] < interval * factor - 30:
                        continue
                due.append((source, keyword, job_watches))
        if due:
            log.info("Проверка: %d из %d брендов", len(due), len(jobs))
            await asyncio.gather(*(self.check_job(s, k, w) for s, k, w in due))

        self.cycles += 1
        self.last_cycle_at = time.time()
        if self.cycles % max(1, (24 * 60) // max(1, config.TICK_MIN)) == 0:
            await self.db.cleanup_seen()

    async def _search(
        self, source: Source, query: str, max_items: int, price_min: float | None, price_max: float | None
    ) -> list[Listing] | None:
        """Один запрос к площадке. None — если запрос не удался."""
        async with self._semaphore:
            try:
                listings = await source.search(query, max_items, price_min=price_min, price_max=price_max)
            except NotImplementedError:
                return None
            except Exception as e:
                log.warning("%s: ошибка поиска «%s»: %s", source.title, query, e)
                return None
        self.searches += 1
        self.items_fetched += len(listings)
        return listings

    async def check_job(self, source: Source, keyword: str, watches: list) -> list[Listing]:
        """
        Проверить один бренд (во всех написаниях) на одной площадке
        и разослать новинки. Возвращает подходящие объявления
        (нужно для предпросмотра).
        """
        if not watches:
            return []
        price_min, price_max = union_range(watches)
        queries = brands.search_queries(keyword)
        keys = [seen_key(q, price_min, price_max) for q in queries]
        job_key = f"{source.name}|{keyword}"

        async with self._locks[(source.name, keyword)]:
            state = await self.db.get_job(job_key)
            max_items = (state["max_items"] if state and state["max_items"] else config.MAX_ITEMS)

            # 1. Ищем по каждому написанию и складываем всё в одну кучу
            fetched: dict[str, Listing] = {}
            per_query_new: list[int] = []
            ok = False
            for query in queries:
                result = await self._search(source, query, max_items, price_min, price_max)
                if result is not None:
                    ok = True
                for listing in result or []:
                    fetched.setdefault(listing.id, listing)
            if not ok:
                return []

            # 2. Что из этого мы ещё НИ РАЗУ не видели
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
                return price_matches(listing, price_min, price_max) and brands.listing_ok(keyword, listing.title)

            good = [l for l in fetched.values() if suitable(l)]
            label = f"{brands.display_name(keyword)} ({' / '.join(queries)})"

            if first_time:
                log.info("%s: %s — первый запрос, запомнил %d объявлений", source.title, label, len(fetched))
                await self.db.save_job(job_key, 0, config.MAX_ITEMS, 0)
                return good

            new_all = [l for l in fetched.values() if l.id in unseen]
            new_good = [l for l in new_all if suitable(l)]
            self.items_filtered += len(new_all) - len(new_good)

            # 4. Умная экономия: подстраиваем частоту и объём следующей проверки
            saturated = len(new_all) >= max_items * len(queries) and len(new_all) > 0
            if saturated:
                next_items = min(max_items * 2, MAX_ITEMS_CAP)
            elif len(new_all) <= max_items // 2:
                next_items = config.MAX_ITEMS
            else:
                next_items = max_items
            streak = 0 if new_all else ((state["empty_streak"] if state else 0) + 1)
            await self.db.save_job(job_key, streak, next_items, len(new_good))

            if new_all:
                log.info("%s: %s — новых: %d, после фильтров: %d%s", source.title, label,
                         len(new_all), len(new_good), " (возьму больше в след. раз)" if saturated else "")
            if not new_good:
                return good

            # 5. Переводим заголовки одним запросом и раскладываем по лентам.
            # Сразу присылаем только тем, кто выбрал «каждое сразу»; остальным
            # придёт одна сводка (см. send_digests).
            translations = await self._translate(new_good)
            for listing in reversed(new_good):  # от старых к новым
                for watch in watches:
                    if price_matches(listing, watch["price_min"], watch["price_max"]):
                        await self.deliver_new(watch, listing, keyword, translations.get(listing.id))
            return good

    async def _translate(self, listings: list[Listing]) -> dict[str, str]:
        if not ai.enabled():
            return {}
        titles = [l.title for l in listings]
        result = await ai.translate_titles(titles)
        return {l.id: t for l, t in zip(listings, result) if t}

    # ------------------------------------------------------------------
    # Предпросмотр при добавлении нового бренда
    # ------------------------------------------------------------------

    async def preview_new_keyword(self, user_id: int, keyword: str, show: bool = True) -> None:
        """
        Вызывается сразу после добавления бренда. Если этот бренд ещё никто
        не отслеживал (или поменялся общий диапазон цен) — делаем первый
        запрос прямо сейчас.
        show=True  — показываем 3 самых свежих объявления;
        show=False — молча запоминаем (для «Готового набора»).
        """
        keyword = brands.canonical(keyword)
        name = html.escape(brands.display_name(keyword))
        jobs = self.group_watches(await self.db.active_watches())
        watches = jobs.get(keyword, [])
        if not watches:
            return
        price_min, price_max = union_range(watches)
        keys = [seen_key(q, price_min, price_max) for q in brands.search_queries(keyword)]
        user_watch = next((w for w in watches if w["user_id"] == user_id), None)

        for source in self.sources:
            already = False
            for key in keys:
                if await self.db.has_any_seen(source.name, key):
                    already = True
                    break
            if already:
                if show:
                    await self._safe_send_text(
                        user_id,
                        f"👌 <b>{name}</b> уже на радаре — новые объявления начнут приходить "
                        "со следующей проверки.",
                    )
                continue

            listings = await self.check_job(source, keyword, watches)
            if not show:
                continue
            if user_watch:
                listings = [l for l in listings if price_matches(l, user_watch["price_min"], user_watch["price_max"])]
            suitable = listings[:3]
            if not suitable:
                await self._safe_send_text(
                    user_id,
                    f"🔎 По <b>{name}</b> сейчас ничего подходящего. "
                    "Как только появится — пришлю первым делом.",
                )
                continue
            await self._safe_send_text(
                user_id,
                f"👀 Вот что есть по <b>{name}</b> прямо сейчас. "
                "Дальше буду присылать только <b>новые</b> объявления.",
            )
            translations = await self._translate(suitable)
            for listing in suitable:
                await self.send_listing(user_id, listing, keyword, header="📌",
                                        title_ru=translations.get(listing.id), count=False)
                await self.db.add_feed(user_id, listing.source, listing.id, keyword,
                                       group_of(decode(listing.title).category), notified=True)

    # ------------------------------------------------------------------
    # Отправка сообщений
    # ------------------------------------------------------------------

    async def _settings(self, user_id: int) -> dict:
        cached = self._settings_cache.get(user_id)
        if cached and time.time() - cached[0] < 120:
            return cached[1]
        settings = await self.db.get_settings(user_id)
        self._settings_cache[user_id] = (time.time(), settings)
        return settings

    def forget_settings(self, user_id: int) -> None:
        self._settings_cache.pop(user_id, None)

    async def deliver_new(self, watch, listing: Listing, keyword: str, title_ru: str | None) -> None:
        """Новая находка для одного подписчика: в ленту, а при режиме «сразу» — ещё и в чат."""
        user_id = watch["user_id"]
        data = dataclasses.asdict(listing)
        if title_ru:
            data["title_ru"] = title_ru
        await self.db.save_listing(listing.source, listing.id, keyword, data)
        settings = await self._settings(user_id)
        instant = settings.get("notify") == "instant"
        grp = group_of(decode(listing.title).category)
        if not await self.db.add_feed(user_id, listing.source, listing.id, keyword, grp, notified=instant):
            return  # уже было в ленте (нашлось по другому написанию)
        await self.db.bump_watch_found(user_id, watch["keyword"])
        if instant:
            await self.send_listing(user_id, listing, keyword, title_ru=title_ru, count=False)

    # ------------------------------------------------------------------
    # Сводки: одно сообщение вместо десятков
    # ------------------------------------------------------------------

    async def send_digests(self) -> None:
        now = time.time()
        for user_id in await self.db.users_with_pending():
            settings = await self._settings(user_id)
            mode = settings.get("notify", "digest")
            if mode == "instant":
                await self.db.mark_notified(user_id)
                continue
            if mode == "off":
                continue  # копится в ленте, без уведомлений
            every = int(settings.get("every") or 30) * 60
            if now - float(settings.get("last_digest") or 0) < every - 30:
                continue
            user = await self.db.get_user(user_id)
            if not user or user["paused"]:
                continue
            pending = await self.db.pending_feed(user_id)
            if not pending:
                continue
            # Прошлую сводку, которую так и не открыли, убираем — в чате всегда одна свежая.
            # В новую входят и старые непросмотренные находки, так что ничего не теряется.
            old_msg = settings.get("digest_msg")
            if old_msg:
                try:
                    await self.bot.delete_message(user_id, old_msg)
                except Exception:
                    pass
                unseen = await self.db.feed_page(user_id, since=int(now) - config.FEED_DAYS * 86400,
                                                 view="new", limit=200)
                have = {(r["source"], r["item_id"]) for r in pending}
                extra = [r for r in unseen if (r["source"], r["item_id"]) not in have]
                pending = list(pending) + extra
            else:
                extra = []
            sent_id = await self._send_digest(user_id, pending, settings, merged=bool(extra))
            if sent_id:
                await self.db.mark_notified(user_id)
                await self.db.update_settings(user_id, last_digest=int(now), digest_msg=sent_id)
                self.forget_settings(user_id)

    async def _send_digest(self, user_id: int, pending: list, settings: dict, merged: bool = False) -> int | None:
        """Отправляет сводку. Возвращает id сообщения (None — не дошло)."""
        from cards import digest_caption, digest_keyboard  # здесь, чтобы не было циклического импорта
        top = None
        cheapest = None
        for row in pending[:200]:
            found = await self.db.get_listing(row["source"], row["item_id"])
            if not found:
                continue
            data = found[1]
            if top is None and data.get("image"):
                top = data
            if data.get("price") is not None and (cheapest is None or data["price"] < cheapest["price"]):
                cheapest = {"price": data["price"], "keyword": found[0]}
        caption = digest_caption(pending, every_min=None if merged else int(settings.get("every") or 30),
                                 cheapest=cheapest)
        markup = digest_keyboard(pending)
        silent = bool(settings.get("quiet")) and is_quiet_now()
        try:
            if top:
                try:
                    sent = await self.bot.send_photo(user_id, photo=top["image"], caption=caption,
                                                     reply_markup=markup, disable_notification=silent)
                except TelegramBadRequest:
                    sent = await self.bot.send_message(user_id, caption, reply_markup=markup,
                                                       disable_notification=silent)
            else:
                sent = await self.bot.send_message(user_id, caption, reply_markup=markup, disable_notification=silent)
            self.messages_sent += 1
            return sent.message_id
        except TelegramForbiddenError:
            await self.db.set_paused(user_id, True)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as e:
            log.warning("Не удалось отправить сводку %s: %s", user_id, e)
        return None

    async def send_listing(
        self,
        user_id: int,
        listing: Listing,
        keyword: str,
        header: str = "🆕",
        title_ru: str | None = None,
        watch_keyword: str | None = None,
        count: bool = True,
    ) -> None:
        async with self._send_lock:
            if await self.db.was_sent(user_id, listing.source, listing.id):
                return
            data = dataclasses.asdict(listing)
            if title_ru:
                data["title_ru"] = title_ru
            await self.db.save_listing(listing.source, listing.id, keyword, data)
            if await self._deliver(user_id, data, keyword, header):
                await self.db.mark_sent(user_id, listing.source, listing.id)
                if count:
                    await self.db.bump_watch_found(user_id, watch_keyword or keyword)

    async def _deliver(self, user_id: int, data: dict, keyword: str, header: str) -> bool:
        """Отправляет одну карточку. True — если сообщение дошло."""
        settings = await self._settings(user_id)
        caption = cards.build_card(data, keyword, settings, header)
        keyboard = cards.card_keyboard(data["source"], data["id"], data["url"])
        silent = bool(settings.get("quiet")) and is_quiet_now()

        for _ in range(2):
            try:
                if data.get("image"):
                    try:
                        await self.bot.send_photo(
                            user_id, photo=data["image"], caption=caption,
                            reply_markup=keyboard, disable_notification=silent,
                        )
                    except TelegramBadRequest:
                        # Telegram не смог скачать фото — отправим текстом
                        await self.bot.send_message(user_id, caption, reply_markup=keyboard,
                                                    disable_notification=silent)
                else:
                    await self.bot.send_message(user_id, caption, reply_markup=keyboard,
                                                disable_notification=silent)
                self.messages_sent += 1
                await asyncio.sleep(0.1)
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                log.info("Пользователь %s заблокировал бота — пауза", user_id)
                await self.db.set_paused(user_id, True)
                return False
            except Exception as e:
                log.warning("Не удалось отправить объявление %s пользователю %s: %s", data.get("id"), user_id, e)
                return False
        return False

    async def _safe_send_text(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(user_id, text)
        except Exception as e:
            log.warning("Не удалось отправить сообщение %s: %s", user_id, e)

    # ------------------------------------------------------------------
    # Прогноз расходов (для /stats)
    # ------------------------------------------------------------------

    async def cost_forecast(self) -> dict:
        jobs = self.group_watches(await self.db.active_watches())
        runs_items = 0.0
        for keyword, watches in jobs.items():
            per_day = (24 * 60) / self.job_interval(watches)
            runs_items += per_day * len(brands.search_queries(keyword)) * config.MAX_ITEMS
        items_per_day = runs_items * max(1, len(self.sources))
        usd_day = items_per_day / 1000 * config.APIFY_PRICE_PER_1000
        return {
            "jobs": len(jobs),
            "watches": sum(len(w) for w in jobs.values()),
            "usd_day": usd_day,
            "rub_month": usd_day * 30 * config.USD_RUB_RATE,
        }
