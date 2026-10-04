"""
Кнопки под карточкой объявления: легит-чек, выгода, избранное, фразы продавцу.
Плюс экран «Избранное» и легит-чек по своим фото.
"""

import asyncio
import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import ai
import brands
import cards
from avito import AvitoPrices
from db import Database
from decoder import decode
from handlers.common import (
    BTN_FAVS, back_home, btn, esc, is_admin, kb, safe_answer, show, url_btn, user_plan,
)

log = logging.getLogger(__name__)
router = Router(name="listing")


class LegitFlow(StatesGroup):
    photos = State()


def _ref(data: str) -> tuple[str, str, str]:
    """'l:lg:goofish:123' -> ('lg', 'goofish', '123')"""
    _, action, source, item_id = data.split(":", 3)
    return action, source, item_id


async def _quota(db: Database, user_id: int, kind: str) -> tuple[bool, int]:
    """(можно ли, сколько останется после этого раза)"""
    plan = await user_plan(db, user_id)
    limit = plan.legit_checks if kind == "legit" else plan.price_checks
    used = await db.get_usage(user_id, kind)
    return used < limit, max(0, limit - used - 1)


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
                "они соберутся здесь, чтобы вернуться к ним позже.", kb(back_home()))
    lines = [f"⭐ <b>Избранное</b> · {len(items)}", ""]
    for i, (source, keyword, data) in enumerate(items, start=1):
        d = decode(data.get("title") or "")
        what = data.get("title_ru") or d.summary() or "объявление"
        price = f"¥{data['price']:,.0f}".replace(",", " ") if data.get("price") is not None else "без цены"
        lines.append(f"{i}. <a href=\"{html.escape(data['url'])}\">{esc(brands.display_name(keyword))}</a> — "
                     f"{esc(what)[:60]} · <b>{price}</b>")
    lines.append("\n<i>Нажми на бренд — откроется объявление.</i>")
    return "\n".join(lines), kb(back_home())


@router.message(F.text == BTN_FAVS)
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
    found = await db.get_listing(source, item_id)
    if not found:
        await safe_answer(callback, "Объявление устарело", alert=True)
        return
    keyword, data = found
    user_id = callback.from_user.id
    ok, left = await _quota(db, user_id, "price")
    if not ok:
        await safe_answer(callback)
        await callback.message.reply("📊 Сравнения цен в этом месяце закончились. "
                                     "На тарифе выше их больше 👇", reply_markup=_no_quota_kb())
        return
    await safe_answer(callback, "Считаю…")
    wait = await callback.message.reply("📊 Считаю себестоимость и смотрю цены на Авито…")
    settings = await db.get_settings(user_id)
    category = decode(data.get("title") or "").category
    market = await avito.market(keyword, category)
    if market and market.get("median"):
        await db.add_usage(user_id, "price")
    text = cards.profit_text(data, keyword, settings, market)
    if market and market.get("median") is not None:
        text += f"\n<i>Осталось сравнений в этом месяце: {left}</i>"
    rows = []
    if market:
        rows.append([url_btn("Открыть поиск на Авито ↗", market["url"])])
    rows.append([btn("⚙️ Доставка и комиссия", "st:open")])
    await wait.edit_text(text, reply_markup=kb(*rows))


# ---------------------------------------------------------------- легит-чек по объявлению

def _ai_off_text(user_id: int) -> str:
    if is_admin(user_id):
        return ("🛡 Легит-чек выключен: не задан <code>AI_API_KEY</code>.\n"
                "Добавь его во вкладке Variables на Railway — инструкция в README.")
    return "🛡 Легит-чек скоро заработает — мы уже подключаем ИИ. Загляни чуть позже 🙏"


@router.callback_query(F.data.startswith("l:lg:"))
async def cb_legit_listing(callback: CallbackQuery, db: Database) -> None:
    _, source, item_id = _ref(callback.data)
    user_id = callback.from_user.id
    if not ai.enabled():
        await safe_answer(callback)
        await callback.message.reply(_ai_off_text(user_id))
        return
    found = await db.get_listing(source, item_id)
    if not found:
        await safe_answer(callback, "Объявление устарело", alert=True)
        return
    keyword, data = found
    ok, left = await _quota(db, user_id, "legit")
    if not ok:
        await safe_answer(callback)
        await callback.message.reply("🛡 Легит-чеки в этом месяце закончились. На тарифе выше их больше 👇",
                                     reply_markup=_no_quota_kb())
        return
    if not data.get("image"):
        await safe_answer(callback, "У объявления нет фото — проверять нечего", alert=True)
        return
    await safe_answer(callback, "Смотрю…")
    wait = await callback.message.reply("🛡 Смотрю на фото внимательно… обычно это 10–30 секунд.")
    price = data.get("price")
    result = await ai.legit_check(
        brands.display_name(keyword),
        image_urls=[data["image"]],
        title=data.get("title"),
        price_text=f"¥{price:.0f}" if price is not None else None,
    )
    if not result:
        await wait.edit_text("Не получилось проверить — ИИ не ответил. Попробуй ещё раз через минуту.")
        return
    await db.add_usage(user_id, "legit")
    text = cards.legit_text(brands.display_name(keyword), result, left)
    text += ("\n\n💡 По одному фото из объявления много не скажешь. Запроси у продавца фото "
             "из списка выше и пришли их мне — «🛡 Легит-чек» на пульте.")
    await wait.edit_text(text, reply_markup=kb([btn("💬 Как спросить по-китайски", f"l:ph:{source}:{item_id}")]))


# ---------------------------------------------------------------- легит-чек по своим фото

LEGIT_HOWTO = (
    "🛡 <b>Легит-чек по фото</b>\n\n"
    "Пришли от 2 до 6 фото одним или несколькими сообщениями. Лучше всего видно:\n"
    "• вещь целиком\n• логотип крупно\n• бирка с размером и тег с составом\n"
    "• строчка и швы\n• фурнитура: молния, кнопки, пуллер\n\n"
    "В подписи к фото можно написать бренд и модель — так точнее. "
    "Когда пришлёшь всё — нажми «Проверить».\n\n"
    "<i>Осталось в этом месяце: {left}</i>"
)

# Сюда складываем фото, пока человек их присылает (по одному сообщению на фото)
_pending: dict[int, dict] = {}
_locks: dict[int, asyncio.Lock] = {}


@router.callback_query(F.data == "lg:start")
async def cb_legit_start(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    user_id = callback.from_user.id
    if not ai.enabled():
        await show(callback, _ai_off_text(user_id), kb(back_home()))
        await safe_answer(callback)
        return
    ok, left = await _quota(db, user_id, "legit")
    if not ok:
        await show(callback, "🛡 Легит-чеки в этом месяце закончились. На тарифе выше их больше.",
                   kb([btn("💎 Тарифы", "pl:open")], back_home()))
        await safe_answer(callback)
        return
    await state.set_state(LegitFlow.photos)
    _pending[user_id] = {"photos": [], "caption": None, "status": None}
    await show(callback, LEGIT_HOWTO.format(left=left + 1), kb([btn("✖️ Отмена", "h:home")]))
    await safe_answer(callback)


@router.message(LegitFlow.photos, F.photo)
async def got_legit_photo(message: Message, state: FSMContext) -> None:
    user_id = message.from_user.id
    lock = _locks.setdefault(user_id, asyncio.Lock())
    async with lock:
        bucket = _pending.setdefault(user_id, {"photos": [], "caption": None, "status": None})
        if len(bucket["photos"]) >= 6:
            return
        bucket["photos"].append(message.photo[-1].file_id)
        if message.caption:
            bucket["caption"] = message.caption[:200]
        count = len(bucket["photos"])
        text = f"📷 Фото: {count} из 6" + (" — можно проверять" if count >= 2 else " — пришли ещё хотя бы одно")
        markup = kb([btn(f"🛡 Проверить ({count})", "lg:go")], [btn("✖️ Отмена", "lg:cancel")]) if count >= 2 \
            else kb([btn("✖️ Отмена", "lg:cancel")])
        status = bucket["status"]
        if status:
            try:
                await message.bot.edit_message_text(text, chat_id=user_id, message_id=status, reply_markup=markup)
                return
            except Exception:
                pass
        sent = await message.answer(text, reply_markup=markup)
        bucket["status"] = sent.message_id


@router.message(LegitFlow.photos)
async def got_legit_other(message: Message) -> None:
    await message.answer("Жду фото 📷 Если передумал — нажми «Отмена».",
                         reply_markup=kb([btn("✖️ Отмена", "lg:cancel")]))


@router.callback_query(F.data == "lg:cancel")
async def cb_legit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    _pending.pop(callback.from_user.id, None)
    await safe_answer(callback, "Отменил")
    await show(callback, "Ок, отменил. Возвращайся, когда будут фото 🙂", kb(back_home()))


@router.callback_query(F.data == "lg:go")
async def cb_legit_go(callback: CallbackQuery, bot: Bot, db: Database, state: FSMContext) -> None:
    user_id = callback.from_user.id
    bucket = _pending.pop(user_id, None)
    await state.clear()
    if not bucket or len(bucket["photos"]) < 2:
        await safe_answer(callback, "Нужно хотя бы 2 фото", alert=True)
        return
    ok, left = await _quota(db, user_id, "legit")
    if not ok:
        await safe_answer(callback, "Легит-чеки в этом месяце закончились", alert=True)
        return
    await safe_answer(callback, "Проверяю…")
    await show(callback, f"🛡 Изучаю {len(bucket['photos'])} фото… обычно это до минуты.")
    images = []
    for file_id in bucket["photos"]:
        buffer = io.BytesIO()
        try:
            await bot.download(file_id, destination=buffer)
            images.append(buffer.getvalue())
        except Exception as e:
            log.warning("Не удалось скачать фото: %s", e)
    caption = bucket.get("caption")
    brand = brands.display_name(brands.canonical(caption)) if caption else "определи сам по фото"
    result = await ai.legit_check(brand, images=images, title=caption)
    if not result:
        await callback.message.answer("Не получилось проверить — ИИ не ответил. Попробуй ещё раз через минуту.",
                                      reply_markup=kb(back_home()))
        return
    await db.add_usage(user_id, "legit")
    title = caption or (result.get("item") or "вещь")
    await callback.message.answer(cards.legit_text(title, result, left),
                                  reply_markup=kb([btn("🛡 Проверить другую вещь", "lg:start")], back_home()))
