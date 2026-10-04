"""HTTP-сервер мини-приложения HUNTR (Telegram Web App) на aiohttp."""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from aiohttp import web
from aiogram.types import LabeledPrice
from aiogram.utils.web_app import safe_parse_webapp_init_data

import ai
import brands
import cards
import config
import decoder
import monitor as monitor_mod
import plans
import rates
import services
from handlers.brands_ui import limit_problem, merge_old_variants
from handlers.common import count_own, run_background, user_plan
from handlers.plans_ui import _description, _title, seats_left

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
AUTH_MAX_AGE = 86400
FEED_DAYS = 7
FREE_PATHS = {("GET", "/api/me"), ("GET", "/api/plans"), ("POST", "/api/invoice"), ("POST", "/api/trial")}

DB = web.AppKey("db", object)
BOT = web.AppKey("bot", object)
MONITOR = web.AppKey("monitor", object)
AVITO = web.AppKey("avito", object)
SOURCES = web.AppKey("sources", dict)

GROUPS = ["Одежда", "Обувь", "Сумки", "Аксессуары"]


class ApiError(Exception):
    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


def _err(status: int, error: str) -> web.Response:
    return web.json_response({"error": error}, status=status)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def _now() -> int:
    return int(time.time())


# ----------------------------------------------------------------------
# Авторизация и ошибки
# ----------------------------------------------------------------------

@web.middleware
async def api_middleware(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    try:
        init_data = request.headers.get("X-Init-Data", "")
        try:
            parsed = safe_parse_webapp_init_data(config.BOT_TOKEN, init_data)
        except (ValueError, TypeError):
            return _err(401, "bad_init_data")
        if not parsed.user or time.time() - parsed.auth_date.timestamp() > AUTH_MAX_AGE:
            return _err(401, "bad_init_data")
        user = parsed.user
        db = request.app[DB]
        await db.upsert_user(user.id, user.username, user.first_name)
        request["uid"] = user.id
        request["first_name"] = user.first_name
        request["admin"] = user.id in config.ADMIN_IDS
        request["access"] = await db.has_access(user.id)
        route = request.match_info.route
        free = (request.method, getattr(route.resource, "canonical", request.path)) in FREE_PATHS
        if not request["access"] and not free:
            return _err(403, "no_access")
        return await handler(request)
    except ApiError as e:
        return _err(e.status, e.error)
    except web.HTTPException as e:
        return _err(e.status, e.reason or "error")
    except Exception:
        log.exception("webapp: %s %s failed", request.method, request.path)
        return _err(500, "server_error")


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise ApiError(400, "bad_json")
    if not isinstance(data, dict):
        raise ApiError(400, "bad_json")
    return data


def _price(value, name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ApiError(400, f"bad_{name}")
    if v < 0:
        raise ApiError(400, f"bad_{name}")
    return v


# ----------------------------------------------------------------------
# Карточка объявления
# ----------------------------------------------------------------------

def card(source: str, item_id: str, keyword: str, data: dict, fav: bool = False,
         found_at: int | None = None, grp: str | None = None) -> dict:
    title = data.get("title") or ""
    d = decoder.decode(title)
    price = data.get("price")
    rate = rates.cny_rub()
    ago = cards.ago(data.get("posted_at"))
    if not ago and found_at:
        ago = cards.ago(found_at)
    legit = data.get("legit")
    return {
        "source": source,
        "item_id": str(item_id),
        "keyword": keyword,
        "brand": brands.display_name(keyword),
        "category": d.category,
        "group": grp or decoder.group_of(d.category),
        "title": data.get("title_ru") or d.summary() or "Объявление",
        "title_orig": decoder.strip_noise(title) if title else "",
        "price": price,
        "rub": round(price * rate) if price is not None else None,
        "city": decoder.city_ru(data.get("city")),
        "ago": ago,
        "found_at": found_at,
        "image": data.get("image"),
        "url": data.get("url"),
        "size": d.size,
        "condition": d.condition,
        "color": d.color,
        "notes": list(d.notes),
        "fav": bool(fav),
        "legit": {"score": legit.get("score"), "verdict": legit.get("verdict")} if isinstance(legit, dict) else None,
    }


# ----------------------------------------------------------------------
# Эндпоинты
# ----------------------------------------------------------------------

async def api_me(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    user = await db.get_user(uid)
    plan = await user_plan(db, uid)
    access = request["access"]
    since = _now() - 86400
    settings = await db.get_settings(uid)
    until = user["sub_until"] if user and user["sub_until"] else None
    return web.json_response({
        "user": {"id": uid, "first_name": request["first_name"]},
        "admin": request["admin"],
        "access": access,
        "trial_available": bool(config.TRIAL_DAYS > 0 and not (user and user["trial_used"]) and not access),
        "trial_days": config.TRIAL_DAYS,
        "plan": {
            "code": plan.code, "title": plan.title,
            "until": None if request["admin"] else until,
            "brands": plan.brands, "own_brands": plan.own_brands, "interval_min": plan.interval_min,
            "legit_checks": plan.legit_checks, "price_checks": plan.price_checks,
            "legit_left": await services.quota_left(db, uid, "legit"),
            "price_left": await services.quota_left(db, uid, "price"),
        },
        "counts": {
            "brands": len(await db.list_watches(uid)),
            "today": await db.count_sent_since(uid, since),
            "feed_new": await db.feed_count(uid, since=since),
        },
        "settings": {k: settings.get(k) for k in ("notify", "every", "delivery", "fee", "quiet")},
        "paused": bool(user and user["paused"]),
        "rate": rates.cny_rub(),
    })


async def api_feed(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    q = request.query
    brand = q.get("brand") or None
    grp = q.get("group") or None
    try:
        offset = max(0, int(q.get("offset", 0)))
        limit = min(60, max(1, int(q.get("limit", 30))))
    except ValueError:
        raise ApiError(400, "bad_paging")
    since = _now() - FEED_DAYS * 86400
    rows = await db.feed_page(uid, brand=brand, grp=grp, since=since, limit=limit, offset=offset)
    total = await db.feed_count(uid, brand=brand, grp=grp, since=since)
    facets = await db.feed_facets(uid, since)
    return web.json_response({
        "items": [card(r["source"], r["item_id"], r["keyword"], r["data"], r["fav"], r["found_at"], r["grp"])
                  for r in rows],
        "total": total,
        "facets": {
            "brands": [{"key": k, "title": brands.display_name(k), "count": n} for k, n in facets["brands"].items()],
            "groups": [{"name": g, "count": n} for g, n in facets["groups"].items()],
        },
    })


async def api_fav(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    body = await _body(request)
    source, item_id = str(body.get("source") or ""), str(body.get("item_id") or "")
    if not source or not item_id:
        raise ApiError(400, "bad_item")
    if not await db.get_listing(source, item_id):
        raise ApiError(404, "not_found")
    return web.json_response({"fav": await db.toggle_favorite(uid, source, item_id)})


async def api_favorites(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    rows = await db.list_favorites(uid, limit=100)
    return web.json_response({"items": [card(src, data.get("id", ""), kw, data, True) for src, kw, data in rows]})


def _watch_json(w, is_admin: bool, plan_code: str) -> dict:
    return {
        "id": w["id"], "keyword": w["keyword"], "title": brands.display_name(w["keyword"]),
        "price_min": w["price_min"], "price_max": w["price_max"], "paused": bool(w["paused"]),
        "found": w["found"], "catalog": brands.is_catalog(w["keyword"]),
        "interval": monitor_mod.watch_interval({"is_admin": is_admin, "plan": plan_code, "keyword": w["keyword"]}),
    }


async def api_brands(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    plan = await user_plan(db, uid)
    watches = await db.list_watches(uid)
    tracked = {brands.canonical(w["keyword"]) for w in watches}
    return web.json_response({
        "items": [_watch_json(w, request["admin"], plan.code) for w in watches],
        "catalog": [{"key": k, "title": v["title"], "tracked": k in tracked} for k, v in brands.BRANDS.items()],
        "limits": {"brands": plan.brands, "own_brands": plan.own_brands,
                   "used": len(watches), "own_used": await count_own(db, uid)},
        "presets": [{"label": label, "min": lo, "max": hi} for label, lo, hi in brands.PRICE_PRESETS],
    })


async def api_brand_add(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    body = await _body(request)
    raw = str(body.get("keyword") or "").strip()
    if not raw or len(raw) > 60:
        raise ApiError(400, "Напиши название бренда")
    low, high = _price(body.get("price_min"), "price_min"), _price(body.get("price_max"), "price_max")
    if low is not None and high is not None and low > high:
        low, high = high, low
    key = brands.canonical(raw)
    problem = await limit_problem(db, uid, key)
    if problem:
        raise ApiError(400, _strip_tags(problem))
    if brands.is_catalog(key):
        await merge_old_variants(db, uid, key)
    is_new = await db.add_watch(uid, key, low, high)
    run_background(request.app[MONITOR].preview_new_keyword(uid, key, show=True))
    return web.json_response({"ok": True, "is_new": is_new})


async def _watch_or_404(request: web.Request):
    db, uid = request.app[DB], request["uid"]
    try:
        watch_id = int(request.match_info["id"])
    except ValueError:
        raise ApiError(404, "not_found")
    watch = await db.get_watch(uid, watch_id)
    if not watch:
        raise ApiError(404, "not_found")
    return watch


async def api_brand_patch(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    watch = await _watch_or_404(request)
    body = await _body(request)
    if "paused" in body:
        await db.set_watch_paused(uid, watch["id"], bool(body["paused"]))
    if "price_min" in body or "price_max" in body:
        low = _price(body.get("price_min"), "price_min") if "price_min" in body else watch["price_min"]
        high = _price(body.get("price_max"), "price_max") if "price_max" in body else watch["price_max"]
        if low is not None and high is not None and low > high:
            low, high = high, low
        await db.update_watch_price(uid, watch["id"], low, high)
    plan = await user_plan(db, uid)
    return web.json_response({"ok": True, "item": _watch_json(await db.get_watch(uid, watch["id"]),
                                                              request["admin"], plan.code)})


async def api_brand_delete(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    watch = await _watch_or_404(request)
    await db.delete_watch(uid, watch["id"])
    return web.json_response({"ok": True})


async def _item_args(request: web.Request) -> tuple[str, str]:
    body = await _body(request)
    source, item_id = str(body.get("source") or ""), str(body.get("item_id") or "")
    if not source or not item_id:
        raise ApiError(400, "bad_item")
    return source, item_id


async def api_profit(request: web.Request) -> web.Response:
    source, item_id = await _item_args(request)
    try:
        res = await services.profit_for(request.app[DB], request.app[AVITO], request["uid"], source, item_id)
    except LookupError:
        raise ApiError(404, "not_found")
    except services.LimitReached:
        raise ApiError(429, "limit")
    res.pop("data", None)
    return web.json_response(res)


async def api_legit(request: web.Request) -> web.Response:
    source, item_id = await _item_args(request)
    if not ai.enabled():
        raise ApiError(503, "ai_off")
    try:
        res = await services.legit_for(request.app[DB], request.app[SOURCES], request["uid"], source, item_id)
    except LookupError:
        raise ApiError(404, "not_found")
    except services.LimitReached:
        raise ApiError(429, "limit")
    res.pop("data", None)
    return web.json_response(res)


async def api_plans(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    current = await db.user_plan_code(uid) if request["access"] else None
    items = []
    for p in plans.PAID_PLANS:
        items.append({"code": p.code, "title": p.title, "tagline": p.tagline, "perks": list(p.perks),
                      "price_stars": p.price_stars, "quarter_stars": plans.quarter_stars(p),
                      "seats": p.seats, "seats_left": await seats_left(db, p, uid)})
    return web.json_response({"current": current, "payments_enabled": config.PAYMENTS_ENABLED,
                              "star_rub": config.STAR_RUB_BUY, "plans": items})


async def api_invoice(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    if not config.PAYMENTS_ENABLED:
        raise ApiError(400, "payments_off")
    body = await _body(request)
    code = str(body.get("plan") or "")
    plan = next((p for p in plans.PAID_PLANS if p.code == code), None)
    if plan is None:
        raise ApiError(400, "bad_plan")
    months = body.get("months", 1)
    if months not in (1, 3):
        raise ApiError(400, "bad_months")
    if await seats_left(db, plan, uid) == 0:
        raise ApiError(409, "no_seats")
    bot = request.app[BOT]
    if months == 1:
        link = await bot.create_invoice_link(
            title=_title(plan, 1), description=_description(plan),
            payload=f"sub:{plan.code}:{uid}", currency="XTR",
            prices=[LabeledPrice(label=f"{plan.title} на 30 дней", amount=plan.price_stars)],
            subscription_period=plans.SUBSCRIPTION_PERIOD,
        )
    else:
        amount = plans.quarter_stars(plan)
        if amount is None:
            raise ApiError(400, "no_quarter")
        link = await bot.create_invoice_link(
            title=_title(plan, 3), description=_description(plan),
            payload=f"once:{plan.code}:3", currency="XTR",
            prices=[LabeledPrice(label=f"{plan.title} на 90 дней", amount=amount)],
        )
    return web.json_response({"link": link})


async def api_trial(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    if config.TRIAL_DAYS <= 0:
        raise ApiError(400, "trial_off")
    until = await db.start_trial(uid, config.TRIAL_DAYS)
    if until is None:
        raise ApiError(409, "trial_used")
    return web.json_response({"ok": True, "until": until})


NOTIFY = {"digest", "instant", "off"}
EVERY = {15, 30, 60, 180}


async def api_settings(request: web.Request) -> web.Response:
    db, uid = request.app[DB], request["uid"]
    body = await _body(request)
    changes = {}
    if "notify" in body:
        if body["notify"] not in NOTIFY:
            raise ApiError(400, "bad_notify")
        changes["notify"] = body["notify"]
    if "every" in body:
        if body["every"] not in EVERY:
            raise ApiError(400, "bad_every")
        changes["every"] = body["every"]
    if "delivery" in body:
        v = body["delivery"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 20000:
            raise ApiError(400, "bad_delivery")
        changes["delivery"] = int(v)
    if "fee" in body:
        v = body["fee"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 50:
            raise ApiError(400, "bad_fee")
        changes["fee"] = float(v)
    if "quiet" in body:
        if not isinstance(body["quiet"], bool):
            raise ApiError(400, "bad_quiet")
        changes["quiet"] = body["quiet"]
    if "paused" in body:
        if not isinstance(body["paused"], bool):
            raise ApiError(400, "bad_paused")
        await db.set_paused(uid, body["paused"])
    settings = await db.update_settings(uid, **changes) if changes else await db.get_settings(uid)
    request.app[MONITOR].forget_settings(uid)
    user = await db.get_user(uid)
    return web.json_response({"ok": True,
                              "settings": {k: settings.get(k) for k in ("notify", "every", "delivery", "fee", "quiet")},
                              "paused": bool(user and user["paused"])})


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


def create_app(db, bot, monitor, avito, sources: dict) -> web.Application:
    app = web.Application(middlewares=[api_middleware])
    app[DB], app[BOT], app[MONITOR], app[AVITO], app[SOURCES] = db, bot, monitor, avito, sources
    r = app.router
    r.add_get("/", index)
    r.add_get("/app", index)
    r.add_get("/api/me", api_me)
    r.add_get("/api/feed", api_feed)
    r.add_post("/api/fav", api_fav)
    r.add_get("/api/favorites", api_favorites)
    r.add_get("/api/brands", api_brands)
    r.add_post("/api/brands", api_brand_add)
    r.add_patch("/api/brands/{id}", api_brand_patch)
    r.add_delete("/api/brands/{id}", api_brand_delete)
    r.add_post("/api/profit", api_profit)
    r.add_post("/api/legit", api_legit)
    r.add_get("/api/plans", api_plans)
    r.add_post("/api/invoice", api_invoice)
    r.add_post("/api/trial", api_trial)
    r.add_post("/api/settings", api_settings)
    r.add_static("/static", STATIC)
    return app
