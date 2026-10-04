"""
Кнопки под объявлением: легит-чек, выгода, избранное, фразы продавцу.
Плюс экран «Избранное».

Легит-чек и выгода считаются в services.py — так же, как в приложении HUNTR.
"""

import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

import ai
import brands
import cards
import services
from avito import AvitoPrices
from db import Database
from decoder import decode
from handlers.common import OLD_FAVS, back_home, btn, esc, is_admin, kb, safe_answer, show, url_btn

log = logging.getLogger(__name__)
router = Router(name="listing")


def _ref(data: str) -> tuple[str, str, str]:
    """'l:lg:goofish:123' -> ('lg', 'goofish', '123')"""
    _, action, source, item_id = data.split(":", 3)
    return action, source, item_id


def _no_quota_kb():
    return kb([btn("💎 Тарифы", "pl:open")])


# ---------------------------------------------------------------- избранное

@router.callback_query(F.data.startswith("l:fv:"))
async def cb_favorite(callback: CallbackQuery, db: Database) -> None:
    _, source, item_id = _ref(callback.data)
    found = await db.get_listing(source, item_id)
    if not found:
        await safe_answer(callback, "Объявление устарело", alert=True)
        return
    now_fav = await db.toggle_favorite(callback.from_user.id, source, item_id)
    await safe_answer(callback, "⭐ В избранном" if now_fav else "Убрал из избранного")
    _, data = found
    try:
        await callback.message.edit_reply_markup(
            reply_markup=cards.card_keyboard(source, item_id, data["url"], is_fav=now_fav))
    except Exception:
        pass


async def favorites_screen(db: Database, user_id: int):
    items = await db.list_favorites(user_id)
    if not items:
        return ("⭐ <b>Избранное</b>\n\nПока пусто. Жми «☆ В избранное» под объявлениями — "
                "они соберутся здесь.", kb(back_home()))
    lines = [f"⭐ <b>Избранное</b> · {len(items)}", ""]
    for i, (source, keyword, data) in enumerate(items[:25], start=1):
        d = decode(data.get("title") or "")
        what = data.get("title_ru") or d.summary() or "объявление"
        price = f"¥{data['price']:,.0f}".replace(",", " ") if data.get("price") is not None else "без цены"
        lines.append(f"{i}. <a href=\"{html.escape(data['url'])}\">{esc(brands.display_name(keyword))}</a> — "
                     f"{esc(what)[:50]} · <b>{price}</b>")
    return "\n".join(lines)[:1020], kb(back_home())


@router.message(F.text.in_(OLD_FAVS))
async def msg_favorites(message: Message, db: Database) -> None:
    text, markup = await favorites_screen(db, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "f:list")
async def cb_favorites(callback: CallbackQuery, db: Database) -> None:
    text, markup = await favorites_screen(db, callback.from_user.id)
    await show(callback, text, markup)
    await safe_answer(callback)


# ---------------------------------------------------------------- фразы продавцу

@router.callback_query(F.data.startswith("l:ph:"))
async def cb_phrases(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    await callback.message.reply(cards.phrases_text())


# ---------------------------------------------------------------- выгода

@router.callback_query(F.data.startswith("l:pr:"))
async def cb_profit(callback: CallbackQuery, db: Database, avito: AvitoPrices) -> None:
    _, source, item_id = _ref(callback.data)
    user_id = callback.from_user.id
    await safe_answer(callback, "Считаю…")
    wait = await callback.message.reply("📊 Считаю себестоимость и смотрю цены на Авито…")
    try:
        r = await services.profit_for(db, avito, user_id, source, item_id)
    except LookupError:
        await wait.edit_text("Это объявление уже устарело — открой свежее из ленты.")
        return
    except services.LimitReached:
        await wait.edit_text("📊 Сравнения цен в этом месяце закончились. На тарифе выше их больше 👇",
                             reply_markup=_no_quota_kb())
        return
    settings = await db.get_settings(user_id)
    text = cards.profit_text(r["data"], r["keyword"], settings, r["market"])
    if r["market"] and r["market"].get("median") is not None:
        text += f"\n<i>Осталось сравнений в этом месяце: {r['left']}</i>"
    rows = []
    if r["market"]:
        rows.append([url_btn("Открыть поиск на Авито ↗", r["market"]["url"])])
    rows.append([btn("⚙️ Доставка и комиссия", "st:open")])
    await wait.edit_text(text, reply_markup=kb(*rows))


# ---------------------------------------------------------------- легит-чек объявления

def _ai_off_text(user_id: int) -> str:
    if is_admin(user_id):
        return ("🛡 Легит-чек выключен: не задан <code>AI_API_KEY</code>.\n"
                "Добавь его во вкладке Variables на Railway.")
    return "🛡 Легит-чек скоро заработает — мы уже подключаем ИИ. Загляни чуть позже 🙏"


@router.callback_query(F.data.startswith("l:lg:"))
async def cb_legit(callback: CallbackQuery, db: Database, sources: dict) -> None:
    _, source, item_id = _ref(callback.data)
    user_id = callback.from_user.id
    if not ai.enabled():
        await safe_answer(callback)
        await callback.message.reply(_ai_off_text(user_id))
        return
    await safe_answer(callback, "Проверяю…")
    wait = await callback.message.reply(
        "🛡 Беру все фото и описание объявления, смотрю продавца и сверяю детали… "
        "Обычно 20–60 секунд.")
    try:
        r = await services.legit_for(db, sources, user_id, source, item_id)
    except LookupError:
        await wait.edit_text("Это объявление уже устарело — открой свежее из ленты.")
        return
    except services.LimitReached:
        await wait.edit_text("🛡 Легит-чеки в этом месяце закончились. На тарифе выше их больше 👇",
                             reply_markup=_no_quota_kb())
        return
    if not r["result"]:
        await wait.edit_text("Не получилось проверить — нет фото или ИИ не ответил. Попробуй через минуту.")
        return
    text = cards.legit_text(brands.display_name(r["keyword"]), r["result"], r["left"],
                            seller=r["seller"], photos=r["photos"])
    if r["cached"]:
        text += "\n<i>Это объявление уже проверяли — показываю результат, лимит не тратится.</i>"
    await wait.edit_text(text[:4000], reply_markup=kb([btn("💬 Как спросить продавца по-китайски",
                                                            f"l:ph:{source}:{item_id}")]))
