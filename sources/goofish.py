"""
Goofish (闲鱼, Xianyu) — через платный актор на Apify.

Почему не парсим сами: Goofish защищён антиботом Alibaba
(подписи запросов, капча, блокировки по IP). Актор на Apify
решает это за нас с помощью китайских/гонконгских прокси.

Актор: unitbytes/goofish-xianyu-search-scraper
Стоимость: примерно $1.6 за 1000 объявлений в режиме "summary",
платы за сам запуск нет (точную цену смотри на странице актора в Apify).

Фильтр цены (priceMin / priceMax) актор умеет применять сам — поэтому
мы передаём его прямо в запрос: Apify возвращает только объявления
в нужном диапазоне, и мы не платим за лишние.
"""

import logging
import re

from apify_client import ApifyClientAsync

from sources.base import Listing, Source

log = logging.getLogger(__name__)


def _get(data: dict, *keys, default=None):
    """Берёт первое непустое значение из словаря по списку возможных ключей."""
    for key in keys:
        value = data.get(key)
        if value not in (None, "", []):
            return value
    return default


def _parse_price(value) -> float | None:
    """'¥1,299.00' / '1299' / 1299 -> 1299.0"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(",", ""))
    return float(match.group(0)) if match else None



# Пометки «продано» / «снято с продажи» в карточке Goofish
_SOLD_MARKS = ("已售出", "已卖出", "卖掉了", "已售", "sold")
_GONE_MARKS = ("已下架", "下架", "已删除", "offline", "removed", "deleted", "invalid")


def item_status(raw: dict) -> str | None:
    """'sold' — продано, 'gone' — снято/удалено, None — в продаже (или неизвестно)."""
    for key in ("status", "itemStatus", "item_status", "state", "saleStatus", "tradeStatus"):
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).lower()
        if any(m in text for m in _SOLD_MARKS):
            return "sold"
        if any(m in text for m in _GONE_MARKS):
            return "gone"
    if raw.get("isSold") is True or raw.get("sold") is True:
        return "sold"
    return None

class GoofishSource(Source):
    name = "goofish"
    title = "Goofish (闲鱼)"
    enabled = True

    def __init__(self, token: str, actor_id: str, proxy_country: str = "HK"):
        self.client = ApifyClientAsync(token)
        self.actor_id = actor_id
        self.proxy_country = proxy_country

    async def search(
        self,
        keyword: str,
        max_items: int,
        price_min: float | None = None,
        price_max: float | None = None,
    ) -> list[Listing]:
        run_input = {
            "keyword": keyword,
            "maxItems": max(1, min(max_items, 1500)),
            "detailLevel": "summary",   # дёшево и быстро; "full" — дороже
            "sortBy": "newest",         # сначала самые новые
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": self.proxy_country,
            },
        }
        # Фильтр цены — прямо на стороне Goofish (актор принимает целые юани)
        if price_min is not None:
            run_input["priceMin"] = int(price_min)
        if price_max is not None:
            run_input["priceMax"] = int(round(price_max))

        # Запускаем актор и ждём, пока он закончит (до 5 минут)
        run = await self.client.actor(self.actor_id).call(
            run_input=run_input, timeout_secs=300, logger=None
        )
        if not run:
            raise RuntimeError("Apify не вернул результат запуска")

        # Разные версии библиотеки возвращают словарь или объект — поддержим оба варианта
        if isinstance(run, dict):
            status = run.get("status")
            dataset_id = run.get("defaultDatasetId")
        else:
            status = getattr(run, "status", None)
            dataset_id = getattr(run, "default_dataset_id", None)

        if status and str(status).upper().endswith("FAILED"):
            raise RuntimeError(f"Запуск актора завершился со статусом {status}")
        if not dataset_id:
            raise RuntimeError("У запуска актора нет датасета с результатами")

        page = await self.client.dataset(dataset_id).list_items(clean=True)
        items = page.items if hasattr(page, "items") else page.get("items", [])

        listings = []
        for raw in items:
            listing = self._to_listing(raw)
            if listing:
                listings.append(listing)
        price_note = ""
        if price_min is not None or price_max is not None:
            price_note = f" (цена {price_min or 0:g}–{price_max or '∞'})"
        log.info("Goofish: «%s»%s — получено %d объявлений", keyword, price_note, len(listings))
        return listings

    async def details(self, item_id: str) -> dict | None:
        """
        Полная карточка ОДНОГО объявления: все фото, описание, состояние
        и репутация продавца. Нужна для легит-чека. Стоит как один результат
        в режиме "full" (дороже summary, но это один запрос по кнопке).
        """
        url = f"https://www.goofish.com/item?id={item_id}"
        base_input = {
            "maxItems": 1,
            "detailLevel": "full",
            "proxyConfiguration": {
                "useApifyProxy": True,
                "apifyProxyGroups": ["RESIDENTIAL"],
                "apifyProxyCountry": self.proxy_country,
            },
        }
        # Актор принимает ссылки или числовые id; пробуем оба формата.
        # Если актор отработал без ошибок, но карточку не вернул ни в одном
        # формате — скорее всего объявление удалено.
        empty_runs = 0
        for start in ([{"url": url}], [url], [str(item_id)]):
            try:
                run = await self.client.actor(self.actor_id).call(
                    run_input={**base_input, "startUrls": start}, timeout_secs=180, logger=None
                )
                dataset_id = run.get("defaultDatasetId") if isinstance(run, dict) else getattr(run, "default_dataset_id", None)
                if not dataset_id:
                    continue
                page = await self.client.dataset(dataset_id).list_items(clean=True)
                items = page.items if hasattr(page, "items") else page.get("items", [])
            except Exception as e:
                log.warning("Goofish: не удалось получить карточку %s (%s): %s", item_id, type(start[0]).__name__, e)
                continue
            if not items:
                empty_runs += 1
                continue
            raw = items[0]
            images = raw.get("images") or []
            images = [("https:" + i if isinstance(i, str) and i.startswith("//") else i) for i in images if isinstance(i, str)]
            seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
            stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
            return {
                "images": images[:9],
                "description": str(raw.get("description") or "")[:1500],
                "condition": raw.get("condition"),
                "price_original": raw.get("priceOriginal"),
                "seller": {k: seller.get(k) for k in ("name", "zhimaCredit", "zhimaAuth", "totalSold",
                                                     "goodReviewRate", "registeredDays", "replyRate24h", "lastActive")},
                "stats": {k: stats.get(k) for k in ("views", "wants", "favorites")},
                "status": item_status(raw),
            }
        if empty_runs == 3:
            return {"status": "gone"}
        return None

    def _to_listing(self, raw: dict) -> Listing | None:
        item_id = _get(raw, "id", "itemId", "item_id")
        if not item_id:
            return None
        item_id = str(item_id)

        seller = raw.get("seller") or {}
        if not isinstance(seller, dict):
            seller = {}

        url = _get(raw, "url", "itemUrl", default=f"https://www.goofish.com/item?id={item_id}")
        image = _get(raw, "pictureUrl", "picUrl", "image", "mainImage")
        if isinstance(image, str) and image.startswith("//"):
            image = "https:" + image

        return Listing(
            source=self.name,
            id=item_id,
            title=str(_get(raw, "title", "name", default="Без названия")).strip(),
            url=url,
            price=_parse_price(_get(raw, "price", "soldPrice")),
            currency=str(_get(raw, "currency", default="CNY")),
            image=image,
            city=_get(raw, "city", "area", "location"),
            seller=_get(seller, "name", "nick"),
            posted_at=_get(raw, "postedAt", "publishTime"),
            extra={
                "free_shipping": raw.get("freeShipping"),
                "wants": raw.get("wants"),
                "tags": raw.get("tags"),
            },
        )
