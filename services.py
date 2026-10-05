"""
Общая логика для бота и приложения HUNTR: выгода и легит-чек объявления.

И кнопки в чате, и мини-приложение вызывают эти функции, поэтому
результат и лимиты тарифа везде одинаковые.

Легит-чек объявления:
  1. Берём полную карточку объявления с Goofish (все фото, описание,
     репутация продавца) — один запрос к Apify, результат запоминаем.
  2. Отдаём фото и контекст «зрячей» модели ИИ.
  3. Результат тоже запоминаем: повторное нажатие бесплатно и мгновенно,
     лимит тарифа не тратится.
"""

import asyncio
import logging
import time

import ai
import apify_guard
import brands
import config
import plans
import rates
from avito import AvitoPrices
from db import Database
from decoder import decode
from sources.goofish import DETAIL_VERSION

log = logging.getLogger(__name__)


class LimitReached(Exception):
    """Лимит тарифа на этот месяц исчерпан."""


async def _plan(db: Database, user_id: int) -> plans.Plan:
    if user_id in config.ADMIN_IDS:
        return plans.ADMIN
    return plans.get_plan(await db.user_plan_code(user_id))


async def quota_left(db: Database, user_id: int, kind: str) -> int:
    plan = await _plan(db, user_id)
    limit = {"legit": plan.legit_checks, "price": plan.price_checks, "assistant": plan.assistant}.get(kind, 0)
    return max(0, limit - await db.get_usage(user_id, kind))


# ----------------------------------------------------------------------
# Выгода
# ----------------------------------------------------------------------

def cost_breakdown(data: dict, settings: dict) -> dict | None:
    price = data.get("price")
    if price is None:
        return None
    d = decode(data.get("title") or "")
    rate = rates.cny_rub()
    base = price * rate
    fee_pct = float(settings.get("fee", config.BUYER_FEE_PCT))
    per_kg = float(settings.get("delivery", config.DELIVERY_RUB_PER_KG))
    fee = base * fee_pct / 100
    delivery = d.weight_kg * per_kg
    return {
        "rate": rate, "rate_label": rates.source_label(), "base": base, "fee": fee, "fee_pct": fee_pct,
        "weight": d.weight_kg, "per_kg": per_kg, "delivery": delivery, "total": base + fee + delivery,
        "category": d.category,
    }


async def profit_for(db: Database, avito: AvitoPrices, user_id: int, source: str, item_id: str) -> dict:
    """
    {'keyword', 'data', 'cost', 'market', 'profit', 'pct', 'left'}.
    Бросает LookupError, если объявления нет, и LimitReached — если кончился лимит.
    """
    found = await db.get_listing(source, item_id)
    if not found:
        raise LookupError("listing")
    keyword, data = found
    settings = await db.get_settings(user_id)
    cost = cost_breakdown(data, settings)
    category = decode(data.get("title") or "").category

    cached = await db.get_avito(f"{brands.display_name(keyword)} {category or ''}".strip().lower(),
                                config.AVITO_CACHE_HOURS * 3600)
    if cached is not None and not cached.get("median"):
        cached = None   # старый пустой результат — спросим Авито заново
    # Лимит «сравнений с Авито» тратим, только когда Авито включено: без него «Выгода» —
    # это бесплатный калькулятор себестоимости
    if config.AVITO_ENABLED and cached is None and await quota_left(db, user_id, "price") <= 0:
        raise LimitReached("price")
    market = await avito.market(keyword, category)
    if cached is None and market and market.get("median"):
        await db.add_usage(user_id, "price")

    profit = pct = None
    if cost and market and market.get("median"):
        profit = market["median"] - cost["total"]
        pct = profit / cost["total"] * 100 if cost["total"] else 0
    return {"keyword": keyword, "data": data, "cost": cost, "market": market,
            "profit": profit, "pct": pct, "left": await quota_left(db, user_id, "price")}


# ----------------------------------------------------------------------
# Легит-чек объявления
# ----------------------------------------------------------------------

_ZHIMA = [("极好", "отличный"), ("优秀", "отличный"), ("良好", "хороший"), ("中等", "средний"),
          ("一般", "средний"), ("较差", "низкий"), ("差", "низкий")]


def zhima_ru(credit) -> str:
    """'信用良好' -> 'хороший' (кредитный рейтинг продавца Alipay/Zhima)."""
    text = str(credit)
    for cn, ru in _ZHIMA:
        if cn in text:
            return ru
    return text


def _num(value) -> float | None:
    try:
        return float(str(value).replace("%", "").replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def seller_trust(detail: dict | None) -> int:
    """
    Насколько можно доверять продавцу: -2…+2.
    Учитываем рейтинг Zhima, число продаж, % хороших отзывов и возраст аккаунта.
    """
    if not detail:
        return 0
    s = detail.get("seller") or {}
    score = 0
    credit = zhima_ru(s.get("zhimaCredit") or "")
    if credit == "отличный":
        score += 1
    elif credit in ("средний", "низкий"):
        score -= 1
    sold = _num(s.get("totalSold"))
    if sold is not None:
        score += 1 if sold >= 50 else (-1 if sold < 3 else 0)
    rate = _num(s.get("goodReviewRate"))
    if rate is not None:
        rate = rate * 100 if rate <= 1 else rate
        if rate < 90:
            score -= 1
    days = _num(s.get("registeredDays"))
    if days is not None and days < 60:
        score -= 1
    return max(-2, min(2, score))


def risk_of(result: dict, photos: int, trust: int) -> str:
    """Итоговый риск: low / medium / high — по оценке ИИ, числу фото и продавцу."""
    score = result.get("score")
    try:
        score = float(score)
    except (TypeError, ValueError):
        score = 50
    score += trust * 5
    if photos < 3:
        score = min(score, 70)      # по 1–2 фото нельзя честно сказать «оригинал»
    if result.get("verdict") == "likely_fake":
        score = min(score, 40)
    return "low" if score >= 80 else ("medium" if score >= 55 else "high")


def seller_lines(detail: dict | None) -> list[str]:
    """Репутация продавца по-русски — то, что видно в полной карточке Goofish."""
    if not detail:
        return []
    s = detail.get("seller") or {}
    lines = []
    credit = s.get("zhimaCredit")
    if credit:
        lines.append(f"кредит Zhima: {zhima_ru(credit)}")
    if s.get("totalSold") not in (None, ""):
        lines.append(f"продал вещей: {s['totalSold']}")
    if s.get("goodReviewRate") not in (None, ""):
        lines.append(f"хороших отзывов: {s['goodReviewRate']}")
    if s.get("registeredDays") not in (None, ""):
        lines.append(f"на Goofish дней: {s['registeredDays']}")
    return lines


DETAIL_RETRY_SEC = 6 * 3600   # после неудачной загрузки карточки пробуем снова через 6 часов


_enrich_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def enrich(db: Database, sources: dict, keyword: str, source: str, item_id: str, data: dict) -> dict | None:
    """
    Полная карточка объявления (с кэшем в базе).
    Один запрос на объявление за раз: раньше «Все фото» и легит-чек, нажатые почти
    одновременно, грузили карточку дважды, и второй (пустой) ответ Goofish затирал первый.
    """
    lock = _enrich_locks.setdefault((source, item_id), asyncio.Lock())
    async with lock:
        fresh = await db.get_listing(source, item_id)
        if fresh:
            data.clear()
            data.update(fresh[1])   # вдруг карточку уже загрузил соседний запрос
        return await _enrich(db, sources, keyword, source, item_id, data)


async def _enrich(db: Database, sources: dict, keyword: str, source: str, item_id: str, data: dict) -> dict | None:
    cached = data.get("detail")
    if isinstance(cached, dict) and cached and (cached.get("v", 0) >= DETAIL_VERSION or cached.get("status")):
        return cached
    if time.time() - data.get("detail_failed", 0) < DETAIL_RETRY_SEC:
        return cached or None   # недавно не получилось — не платим за повтор каждую минуту
    src = sources.get(source)
    detail = await src.details(item_id) if src else None
    if not detail:
        # Не получилось (например, закончился лимит Apify) — НЕ запоминаем «фото нет»,
        # попробуем ещё раз позже. Старую карточку, если была, оставляем.
        if not apify_guard.blocked():
            data["detail_failed"] = int(time.time())
            await db.save_listing(source, item_id, keyword, data)
        return cached or None
    if isinstance(cached, dict) and cached.get("images") and not detail.get("images"):
        # Goofish иногда отдаёт «урезанную» карточку без фото (restrictionReason) —
        # не теряем уже загруженную галерею
        detail = {**detail, "images": cached["images"]}
    data["detail"] = detail
    data.pop("detail_failed", None)
    if detail.get("status"):
        data["status"] = detail["status"]   # продано / снято — уберём из ленты
    await db.save_listing(source, item_id, keyword, data)
    return detail


async def legit_for(db: Database, sources: dict, user_id: int, source: str, item_id: str) -> dict:
    """
    {'keyword', 'data', 'result', 'seller', 'photos', 'cached', 'left'}.
    result=None — если ИИ не ответил. Бросает LookupError / LimitReached.
    """
    found = await db.get_listing(source, item_id)
    if not found:
        raise LookupError("listing")
    keyword, data = found
    if data.get("legit"):
        result = dict(data["legit"])
        result.setdefault("risk", risk_of(result, data.get("legit_photos", 1), seller_trust(data.get("detail"))))
        return {"keyword": keyword, "data": data, "result": result, "seller": seller_lines(data.get("detail")),
                "photos": data.get("legit_photos", 1), "cached": True, "left": await quota_left(db, user_id, "legit")}
    if await quota_left(db, user_id, "legit") <= 0:
        raise LimitReached("legit")

    detail = await enrich(db, sources, keyword, source, item_id, data)
    images = (detail or {}).get("images") or ([data["image"]] if data.get("image") else [])
    if not images:
        return {"keyword": keyword, "data": data, "result": None, "seller": [], "photos": 0,
                "cached": False, "left": await quota_left(db, user_id, "legit")}

    notes = []
    if detail:
        if detail.get("description"):
            notes.append("Описание продавца: " + detail["description"][:1200])
        if detail.get("condition"):
            notes.append(f"Состояние: {detail['condition']}")
        sl = seller_lines(detail)
        if sl:
            notes.append("Продавец: " + "; ".join(sl))
    category = decode(data.get("title") or "").category
    market = await db.get_avito(f"{brands.display_name(keyword)} {category or ''}".strip().lower(),
                                config.AVITO_CACHE_HOURS * 3600)
    if market and market.get("median"):
        notes.append(f"Похожие вещи на Авито в России: медиана {market['median']:.0f} ₽")
    price = data.get("price")
    result = await ai.legit_check(
        brands.display_name(keyword),
        image_urls=images[:6],
        title=data.get("title"),
        price_text=f"¥{price:.0f} (≈ {price * rates.cny_rub():.0f} ₽)" if price is not None else None,
        notes="\n".join(notes) or None,
    )
    if result:
        result["risk"] = risk_of(result, len(images[:6]), seller_trust(detail))
        await db.add_usage(user_id, "legit")
        data["legit"] = result
        data["legit_photos"] = len(images[:6])
        await db.save_listing(source, item_id, keyword, data)
    return {"keyword": keyword, "data": data, "result": result, "seller": seller_lines(detail),
            "photos": len(images[:6]), "cached": False, "left": await quota_left(db, user_id, "legit")}
