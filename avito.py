"""
Сколько такая вещь стоит в России — по объявлениям на Авито.

Важно: официальный API Авито (developers.avito.ru) работает только с
ТВОИМИ объявлениями и сообщениями — искать чужие объявления он не умеет.
Поэтому цены берём через актор на Apify (тот же APIFY_TOKEN, что и для
Goofish). По умолчанию — ahaham_bytiz/avito-scraper: на момент написания
он стоил около $0.5 за 1000 объявлений из поиска + трафик прокси.

Чтобы не платить за одно и то же, результат по каждому запросу
запоминается на AVITO_CACHE_HOURS часов (по умолчанию сутки): если
десять человек нажали «Цена в РФ» на куртку Stone Island — запрос один.

Авито жёстко режет запросы: иногда сразу отвечает «429 Too Many Requests»
(слишком много запросов с этого IP). Поэтому:
  1. основной актор запускаем до AVITO_TRIES раз — каждый запуск получает
     новый российский IP из жилых прокси Apify;
  2. если не вышло — запасной актор AVITO_FALLBACK_ACTOR_ID
     (по умолчанию logiover/avito-ru-scraper: сам меняет IP и повторяет при 429,
     ~$2.1 за 1000 объявлений, 30 объявлений ≈ $0.06).

Если актор выключен (AVITO_ENABLED=0) или все попытки не удались — бот всё
равно даст кнопку «Открыть поиск на Авито», чтобы посмотреть цены руками.
"""

import asyncio
import logging
import re
import statistics
from urllib.parse import quote_plus

from apify_client import ApifyClientAsync

import apify_guard
import brands
import config
from db import Database

log = logging.getLogger(__name__)


def search_query(keyword: str, category: str | None) -> str:
    """'stone island' + 'куртка' -> 'Stone Island куртка'"""
    name = brands.display_name(keyword)
    return f"{name} {category}".strip() if category else name


def search_url(query: str) -> str:
    # /rossiya — поиск по всей России (так ссылки выглядят на самом Авито)
    return f"https://www.avito.ru/rossiya?q={quote_plus(query)}"


def _price_of(item: dict) -> float | None:
    """Цена объявления: число в price, иначе цифры из priceText ('45 000 ₽')."""
    for key in ("price", "priceValue", "priceRub", "priceAmount"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, dict):
            value = value.get("value") or value.get("amount")
        if isinstance(value, str):
            digits = re.sub(r"[^0-9]", "", value)
            if digits:
                return float(digits)
    text = item.get("priceText") or item.get("price_text") or item.get("priceString")
    if isinstance(text, str):
        digits = re.sub(r"[^0-9]", "", text)
        if digits:
            return float(digits)
    return None


def _relevant(keyword: str, title: str) -> bool:
    """Объявление действительно про наш бренд (латиницей или по-русски)?"""
    title_l = title.lower()
    if brands.mentions_brand(keyword, title_l):
        brand = brands.get_brand(keyword)
        # Для незнакомых брендов mentions_brand всегда True — проверим хотя бы слово
        if brand or keyword.lower() in title_l:
            return True
    return any(alias in title_l for alias in brands.russian_aliases(keyword))


def _stats(prices: list[float]) -> dict | None:
    if len(prices) < 3:
        return None
    prices = sorted(prices)
    q = statistics.quantiles(prices, n=4)
    low, high = q[0], q[2]
    iqr = high - low
    # Отбрасываем явные выбросы: «1 ₽ за фото» и «999 999 ₽»
    clean = [p for p in prices if low - 1.5 * iqr <= p <= high + 1.5 * iqr] or prices
    q = statistics.quantiles(clean, n=4) if len(clean) >= 4 else [clean[0], statistics.median(clean), clean[-1]]
    return {
        "count": len(clean),
        "median": round(statistics.median(clean), -2),
        "p25": round(q[0], -2),
        "p75": round(q[2], -2),
        "min": round(clean[0], -2),
    }


RU_RESIDENTIAL = {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"], "apifyProxyCountry": "RU"}


def explain(log_tail: str) -> str:
    """Человеческое объяснение, почему актор ничего не нашёл, + хвост его лога."""
    low = log_tail.lower()
    if "429" in low or "too many requests" in low:
        why = "Авито ответил 429 Too Many Requests — временно заблокировал IP прокси."
    elif "403" in low or "captcha" in low or "капч" in low or "firewall" in low:
        why = "Авито показал капчу или блокировку (403)."
    elif "proxy" in low and ("not allowed" in low or "access" in low or "residential" in low):
        why = "Apify не дал жилые прокси RU — проверь, что на аккаунте есть доступ к Residential proxy."
    else:
        why = "Актор отработал, но объявлений не вернул."
    tail = " \n".join(log_tail.strip().splitlines()[-6:])
    return f"{why}\n{tail}".strip()


class AvitoPrices:
    def __init__(self, db: Database):
        self.db = db
        self.client = ApifyClientAsync(config.APIFY_TOKEN) if config.AVITO_ENABLED else None
        self.last_log = ""   # почему последний запрос ничего не дал — для /avitotest
        self.last_actor = ""  # какой актор принёс цены в последний раз

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def market(self, keyword: str, category: str | None) -> dict | None:
        """
        {'query', 'url', 'count', 'median', 'p25', 'p75', 'min'} или
        {'query', 'url'} без цифр — если узнать цены не получилось.
        """
        query = search_query(keyword, category)
        base = {"query": query, "url": search_url(query)}
        if not self.enabled:
            return base

        cached = await self.db.get_avito(query.lower(), config.AVITO_CACHE_HOURS * 3600)
        if cached is not None and cached.get("median"):
            return {**base, **cached}

        self.last_log = ""
        self.last_actor = ""
        items = await self._fetch(base["url"], query)

        relevant = []
        for item in items:
            price = _price_of(item)
            title = str(item.get("title") or "")
            if price is not None and price >= 300 and _relevant(keyword, title):
                relevant.append((price, title, item.get("url")))
        prices = [p for p, _, _ in relevant]
        stats = _stats(prices) or {"count": len(prices)}
        if stats.get("median"):
            # 3 похожих объявления с ценой ближе всего к медиане — чтобы можно было глянуть глазами
            med = stats["median"]
            samples = sorted(relevant, key=lambda r: abs(r[0] - med))[:3]
            stats["samples"] = [{"title": t[:80], "price": p, "url": u} for p, t, u in samples if u]
            await self.db.save_avito(query.lower(), stats)   # пустой результат не запоминаем
        log.info("Авито (%s): «%s» — объявлений %d, подходящих цен %d%s", self.last_actor or "—",
                 query, len(items), len(prices),
                 f" (пример полей: {sorted(items[0].keys())[:12]})" if items and not prices else "")
        return {**base, **stats}

    async def _fetch(self, url: str, query: str) -> list:
        """Объявления с поиска Авито: основной актор (с повторами), потом запасной."""
        logs = []
        for attempt in range(1, max(1, config.AVITO_TRIES) + 1):
            items = await self._run(config.AVITO_ACTOR_ID, {
                "startUrls": [{"url": url}],   # только так: строкой актор ссылку не принимает
                "maxItems": config.AVITO_MAX_ITEMS,
                "maxPagesPerUrl": 1,
                "scrapeDetails": False,
                "language": "ru",
                "proxyConfiguration": RU_RESIDENTIAL,
            }, query)
            if items:
                self.last_actor = config.AVITO_ACTOR_ID
                return items
            logs.append(f"[{config.AVITO_ACTOR_ID}, попытка {attempt}] {self.last_log}")
            if self.last_log == "APIFY_LIMIT":
                return []
            if attempt < config.AVITO_TRIES:
                await asyncio.sleep(3)
        if config.AVITO_FALLBACK_ACTOR_ID:
            items = await self._run(config.AVITO_FALLBACK_ACTOR_ID, {
                "searchQueries": [query],
                "maxItemsPerQuery": config.AVITO_MAX_ITEMS,
                "maxResults": config.AVITO_MAX_ITEMS,
                "proxyConfiguration": RU_RESIDENTIAL,
            }, query)
            if items:
                self.last_actor = config.AVITO_FALLBACK_ACTOR_ID
                return items
            if self.last_log == "APIFY_LIMIT":
                return []
            logs.append(f"[{config.AVITO_FALLBACK_ACTOR_ID}] {self.last_log}")
        self.last_log = "\n".join(logs)
        return []

    async def _run(self, actor_id: str, run_input: dict, query: str) -> list:
        if apify_guard.blocked():
            self.last_log = "APIFY_LIMIT"
            return []
        self.last_log = ""
        try:
            run = await self.client.actor(actor_id).call(
                run_input=run_input,
                timeout_secs=180,
                logger=None,  # не дублировать логи актора в логи бота
            )
        except Exception as e:
            log.warning("Авито (%s): ошибка для «%s»: %s", actor_id, query, e)
            if apify_guard.note(e):
                self.last_log = "APIFY_LIMIT"
            else:
                self.last_log = f"Ошибка запуска: {e}"
            return []
        if not run:
            self.last_log = "Актор не вернул запуск"
            log.warning("Авито (%s): актор не вернул запуск для «%s»", actor_id, query)
            return []
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "default_dataset_id", None)
        if not dataset_id:
            self.last_log = f"У запуска нет результатов (статус {status})"
            log.warning("Авито (%s): у запуска нет датасета (статус %s) для «%s»", actor_id, status, query)
            return []
        try:
            page = await self.client.dataset(dataset_id).list_items(clean=True)
        except Exception as e:
            self.last_log = f"Не прочитал результаты: {e}"
            log.warning("Авито (%s): не прочитал результаты «%s»: %s", actor_id, query, e)
            return []
        items = page.items if hasattr(page, "items") else page.get("items", [])
        # Запасной актор помечает отказы Авито строками со статусом blocked — это не объявления
        items = [i for i in items if isinstance(i, dict) and not i.get("blocked") and i.get("status") != "blocked"]
        if not items:
            # Почему пусто — смотрим хвост лога самого актора (видно блокировки, капчу, ошибки ввода)
            run_id = run.get("id") if isinstance(run, dict) else getattr(run, "id", None)
            tail = ""
            if run_id:
                try:
                    tail = (await self.client.run(run_id).log().get() or "")[-1500:]
                except Exception as e:
                    tail = f"(лог не прочитался: {e})"
            self.last_log = explain(tail)
            log.warning("Авито (%s): статус %s, объявлений нет («%s»). Конец лога: %s",
                        actor_id, status, query, " | ".join(tail.strip().splitlines()[-8:])[:900])
        return items

    async def close(self) -> None:
        return None
