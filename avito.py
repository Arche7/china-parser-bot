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

Если актор выключен (AVITO_ENABLED=0) или упал — бот всё равно даст
кнопку «Открыть поиск на Авито», чтобы посмотреть цены руками.
"""

import logging
import re
import statistics
from urllib.parse import quote_plus

from apify_client import ApifyClientAsync

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
    for key in ("price", "priceValue", "priceRub"):
        value = item.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if isinstance(value, dict):
            value = value.get("value") or value.get("amount")
        if isinstance(value, str):
            digits = re.sub(r"[^0-9]", "", value)
            if digits:
                return float(digits)
    text = item.get("priceText") or item.get("price_text")
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


class AvitoPrices:
    def __init__(self, db: Database):
        self.db = db
        self.client = ApifyClientAsync(config.APIFY_TOKEN) if config.AVITO_ENABLED else None

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

        items: list = []
        # 1-я попытка — жилые прокси РФ (так советует автор актора),
        # 2-я — прокси по умолчанию, если первая ничего не принесла
        for proxy in ({"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"], "apifyProxyCountry": "RU"},
                      {"useApifyProxy": True, "apifyProxyCountry": "RU"}):
            items = await self._run(base["url"], proxy, query)
            if items:
                break

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
        log.info("Авито: «%s» — объявлений %d, подходящих цен %d%s", query, len(items), len(prices),
                 f" (пример полей: {sorted(items[0].keys())[:12]})" if items and not prices else "")
        return {**base, **stats}

    async def _run(self, url: str, proxy: dict, query: str) -> list:
        try:
            run = await self.client.actor(config.AVITO_ACTOR_ID).call(
                run_input={
                    "startUrls": [{"url": url}],
                    "maxItems": config.AVITO_MAX_ITEMS,
                    "maxPagesPerUrl": 1,
                    "scrapeDetails": False,
                    "language": "ru",
                    "proxyConfiguration": proxy,
                },
                timeout_secs=180,
                logger=None,  # не дублировать логи актора в логи бота
            )
        except Exception as e:
            log.warning("Авито: ошибка для «%s»: %s", query, e)
            return []
        if not run:
            log.warning("Авито: актор не вернул запуск для «%s»", query)
            return []
        status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
        dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "default_dataset_id", None)
        if not dataset_id:
            log.warning("Авито: у запуска нет датасета (статус %s) для «%s»", status, query)
            return []
        try:
            page = await self.client.dataset(dataset_id).list_items(clean=True)
        except Exception as e:
            log.warning("Авито: не прочитал результаты «%s»: %s", query, e)
            return []
        items = page.items if hasattr(page, "items") else page.get("items", [])
        if not items:
            log.warning("Авито: актор отработал со статусом %s, но объявлений нет («%s», прокси %s)",
                        status, query, proxy.get("apifyProxyGroups") or "по умолчанию")
        return [i for i in items if isinstance(i, dict)]

    async def close(self) -> None:
        return None
