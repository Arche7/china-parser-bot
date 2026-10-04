"""Проверка API мини-приложения. Запуск: python3 webapp/check_server.py (нужны BOT_TOKEN, ADMIN_IDS=1)."""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = tempfile.mkdtemp(prefix="huntr_webapp_test_")
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import config  # noqa: E402
from db import Database  # noqa: E402
from webapp.server import create_app  # noqa: E402


def sign(user_id: int, token: str = None, auth_date: int = None, first_name: str = "Тест") -> str:
    token = token or config.BOT_TOKEN
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": first_name, "username": f"u{user_id}"},
                           ensure_ascii=False, separators=(",", ":")),
    }
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def create_invoice_link(self, **kw):
        self.calls.append(kw)
        return f"https://t.me/$invoice{len(self.calls)}"


class FakeMonitor:
    def __init__(self):
        self.previews, self.forgotten = [], []

    async def preview_new_keyword(self, user_id, keyword, show=True):
        self.previews.append((user_id, keyword))

    def forget_settings(self, user_id):
        self.forgotten.append(user_id)


class FakeAvito:
    async def market(self, keyword, category):
        return {"query": f"{keyword} {category}", "url": "https://www.avito.ru/rossiya?q=x",
                "count": 12, "median": 30000, "p25": 20000, "p75": 40000}


async def main() -> None:
    db = Database(os.environ["DB_PATH"])
    await db.connect()
    bot, mon = FakeBot(), FakeMonitor()
    app = create_app(db, bot, mon, FakeAvito(), {"goofish": None})
    client = TestClient(TestServer(app))
    await client.start_server()
    admin, user = 1, 42
    H = lambda uid: {"X-Init-Data": sign(uid)}  # noqa: E731

    async def call(method, path, uid=None, status=200, headers=None, **kw):
        r = await client.request(method, path, headers=headers if headers is not None else H(uid), **kw)
        body = await r.json() if r.content_type == "application/json" else await r.text()
        assert r.status == status, (method, path, r.status, body)
        return body

    try:
        # --- авторизация
        await call("GET", "/api/me", status=401, headers={})
        await call("GET", "/api/me", status=401, headers={"X-Init-Data": sign(user, token="999:wrong")})
        await call("GET", "/api/me", status=401, headers={"X-Init-Data": sign(user, auth_date=int(time.time()) - 90000)})
        await call("GET", "/api/me", status=401, headers={"X-Init-Data": "hash=abc&user=1"})

        # --- без доступа
        me = await call("GET", "/api/me", user)
        assert me["access"] is False and me["trial_available"] is True and me["user"]["id"] == user, me
        assert (await call("GET", "/api/feed", user, status=403)) == {"error": "no_access"}
        await call("GET", "/api/brands", user, status=403)
        await call("GET", "/api/favorites", user, status=403)
        pl = await call("GET", "/api/plans", user)
        assert [p["code"] for p in pl["plans"]] == ["start", "pro", "elite"], pl
        assert pl["current"] is None and pl["star_rub"] == config.STAR_RUB_BUY

        # --- пробный период
        tr = await call("POST", "/api/trial", user, json={})
        assert tr["ok"] and tr["until"] > time.time()
        await call("POST", "/api/trial", user, status=409, json={})
        me = await call("GET", "/api/me", user)
        assert me["access"] is True and me["plan"]["code"] == "trial" and me["trial_available"] is False, me
        await call("GET", "/api/feed", user)

        # --- бренды
        b = await call("GET", "/api/brands", user)
        assert b["items"] == [] and b["catalog"] and b["presets"] and b["limits"]["used"] == 0, b
        add = await call("POST", "/api/brands", user, json={"keyword": "Gucci", "price_min": 200, "price_max": 1800})
        assert add == {"ok": True, "is_new": True}, add
        await asyncio.sleep(0.05)
        assert mon.previews == [(user, "gucci")], mon.previews
        again = await call("POST", "/api/brands", user, json={"keyword": "古驰", "price_min": None, "price_max": 500})
        assert again["is_new"] is False, again
        await call("POST", "/api/brands", user, status=400, json={"keyword": ""})
        b = await call("GET", "/api/brands", user)
        assert len(b["items"]) == 1 and b["items"][0]["keyword"] == "gucci" and b["items"][0]["price_max"] == 500, b
        assert any(c["key"] == "gucci" and c["tracked"] for c in b["catalog"])
        wid = b["items"][0]["id"]
        p = await call("PATCH", f"/api/brands/{wid}", user, json={"paused": True, "price_min": 100, "price_max": 900})
        assert p["item"]["paused"] is True and p["item"]["price_min"] == 100 and p["item"]["price_max"] == 900, p
        await call("PATCH", "/api/brands/99999", user, status=404, json={"paused": False})
        await call("PATCH", f"/api/brands/{wid}", admin, status=404, json={"paused": False})  # чужой бренд
        await call("DELETE", f"/api/brands/{wid}", user)
        assert (await call("GET", "/api/brands", user))["items"] == []

        # --- лента
        now = int(time.time())
        seed = [("g1", "gucci", "古驰 黑色 夹克 L码 99新", "Одежда", 680),
                ("g2", "gucci", "古驰 运动鞋 42码", "Обувь", 1200),
                ("p1", "prada", "普拉达 包 全新", "Сумки", 1450)]
        for item_id, kw, title, grp, price in seed:
            await db.save_listing("goofish", item_id, kw, {
                "id": item_id, "title": title, "price": price, "city": "上海", "posted_at": now - 300,
                "image": "https://img.example/x.jpg", "url": f"https://www.goofish.com/item?id={item_id}",
                **({"legit": {"verdict": "likely_real", "score": 86}} if item_id == "g1" else {})})
            await db.add_feed(user, "goofish", item_id, kw, grp, notified=True)
        f = await call("GET", "/api/feed", user)
        assert f["total"] == 3 and len(f["items"]) == 3, f
        assert {x["key"]: x["count"] for x in f["facets"]["brands"]} == {"gucci": 2, "prada": 1}, f["facets"]
        assert {x["name"] for x in f["facets"]["groups"]} == {"Одежда", "Обувь", "Сумки"}
        g1 = next(i for i in f["items"] if i["item_id"] == "g1")
        assert g1["brand"] == "Gucci" and g1["rub"] and g1["legit"] == {"score": 86, "verdict": "likely_real"}, g1
        assert g1["title"] and g1["title_orig"] and g1["url"].startswith("https://"), g1
        fb = await call("GET", "/api/feed?brand=gucci&limit=1", user)
        assert fb["total"] == 2 and len(fb["items"]) == 1
        assert (await call("GET", "/api/feed?group=Сумки", user))["total"] == 1
        assert len((await call("GET", "/api/feed?offset=2", user))["items"]) == 1
        await call("GET", "/api/feed?offset=x", user, status=400)
        me = await call("GET", "/api/me", user)
        assert me["counts"]["feed_new"] == 3, me["counts"]

        # --- избранное
        assert (await call("POST", "/api/fav", user, json={"source": "goofish", "item_id": "g2"})) == {"fav": True}
        favs = await call("GET", "/api/favorites", user)
        assert [x["item_id"] for x in favs["items"]] == ["g2"] and favs["items"][0]["fav"] is True, favs
        assert next(i for i in (await call("GET", "/api/feed", user))["items"] if i["item_id"] == "g2")["fav"]
        assert (await call("POST", "/api/fav", user, json={"source": "goofish", "item_id": "g2"})) == {"fav": False}
        assert (await call("GET", "/api/favorites", user))["items"] == []
        await call("POST", "/api/fav", user, status=404, json={"source": "goofish", "item_id": "nope"})

        # --- выгода и легит-чек
        pr = await call("POST", "/api/profit", user, json={"source": "goofish", "item_id": "g1"})
        assert "data" not in pr and pr["market"]["median"] == 30000 and pr["cost"]["total"] > 0, pr
        assert pr["profit"] == 30000 - pr["cost"]["total"]
        await call("POST", "/api/profit", user, status=404, json={"source": "goofish", "item_id": "nope"})
        await call("POST", "/api/profit", user, status=400, json={})
        import ai
        if ai.enabled():
            lg = await call("POST", "/api/legit", user, json={"source": "goofish", "item_id": "g1"})
            assert lg["cached"] is True and "data" not in lg, lg
        else:
            assert (await call("POST", "/api/legit", user, status=503,
                               json={"source": "goofish", "item_id": "g1"})) == {"error": "ai_off"}

        # --- оплата
        inv = await call("POST", "/api/invoice", user, json={"plan": "pro", "months": 1})
        assert inv["link"].startswith("https://t.me/"), inv
        c = bot.calls[-1]
        assert c["currency"] == "XTR" and c["subscription_period"] == 2_592_000 and c["payload"] == f"sub:pro:{user}", c
        inv3 = await call("POST", "/api/invoice", user, json={"plan": "start", "months": 3})
        assert inv3["link"].startswith("https://t.me/")
        c = bot.calls[-1]
        assert "subscription_period" not in c and c["payload"] == "once:start:3", c
        import plans
        assert c["prices"][0].amount == plans.quarter_stars(plans.START)
        await call("POST", "/api/invoice", user, status=400, json={"plan": "elite", "months": 3})
        await call("POST", "/api/invoice", user, status=400, json={"plan": "admin", "months": 1})
        await call("POST", "/api/invoice", user, status=400, json={"plan": "pro", "months": 2})

        # --- настройки
        await call("POST", "/api/settings", user, status=400, json={"notify": "x"})
        await call("POST", "/api/settings", user, status=400, json={"every": 7})
        await call("POST", "/api/settings", user, status=400, json={"fee": "много"})
        st = await call("POST", "/api/settings", user,
                        json={"notify": "instant", "every": 60, "delivery": 1200, "fee": 7, "quiet": True, "paused": True})
        assert st["settings"] == {"notify": "instant", "every": 60, "delivery": 1200, "fee": 7.0, "quiet": True}, st
        assert st["paused"] is True and user in mon.forgotten
        me = await call("GET", "/api/me", user)
        assert me["paused"] is True and me["settings"]["notify"] == "instant", me
        await call("POST", "/api/settings", user, status=400, data="not json",
                   headers={**H(user), "Content-Type": "application/json"})

        # --- админ
        me = await call("GET", "/api/me", admin)
        assert me["admin"] is True and me["access"] is True and me["plan"]["code"] == "admin", me

        # --- страница
        for path in ("/", "/app"):
            r = await client.get(path)
            text = await r.text()
            assert r.status == 200 and "<html" in text.lower() and r.headers.get("Cache-Control") == "no-cache", path
        r = await client.get("/static/index.html")
        assert r.status == 200
        await call("GET", "/api/nope", user, status=404)
    finally:
        await client.close()
        await db.close()
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
