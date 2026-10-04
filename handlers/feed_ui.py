"""
Лента прямо в чате и настройки уведомлений.

Вместо сотни сообщений бот присылает одну сводку («14 новых · Stone Island 5 ·
Gucci 4 …»). Из сводки или с главной открывается просмотрщик: одно сообщение
с фото, которое листается кнопками ‹ ›. Можно оставить только один бренд или
раздел (Одежда, Обувь, Сумки, Аксессуары).

Формат кнопок: fd:v:<тип>:<значение>:<номер>
  тип all — всё, b — бренд (короткий код), g — раздел.
"""

import logging
import os
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

import brands
import cards
import config
from db import Database
from decoder import GROUPS
from aiogram.filters import Command

from handlers.common import BTN_FEED, back_home, btn, kb, safe_answer, show
from monitor import Monitor

log = logging.getLogger(__name__)
router = Router(name="feed")

WEEK = config.FEED_DAYS * 86400   # лента хранит находки за столько дней
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
NO_PHOTO = os.path.join(ASSETS, "home.jpg")


async def resolve_filter(db: Database, user_id: int, ftype: str, fval: str) -> tuple[str | None, str | None, str]:
    """(бренд, раздел, подпись фильтра)"""
    if ftype == "b":
        facets = await db.feed_facets(user_id, since=int(time.time()) - WEEK)
        for key in facets["brands"]:
            if cards.brand_code(key) == fval:
                return key, None, brands.display_name(key)
        return None, None, "Все"
    if ftype == "g" and fval in GROUPS:
        return None, fval, fval
    return None, None, "Все"


async def show_item(callback: CallbackQuery, db: Database, ftype: str, fval: str, idx: int) -> None:
    user_id = callback.from_user.id
    brand, grp, label = await resolve_filter(db, user_id, ftype, fval)
    since = int(time.time()) - WEEK
    total = await db.feed_count(user_id, brand=brand, grp=grp, since=since)
    if total == 0:
        await show(callback, f"📰 <b>Лента пуста</b>\n\nЗа {config.FEED_DAYS} дня здесь ничего не нашлось. "
                             "Добавь бренды или расширь бюджет — и находки появятся.",
                   kb([btn("🎯 Бренды", "b:list")], back_home()))
        return
    idx = max(0, min(idx, total - 1))
    item = (await db.feed_page(user_id, brand=brand, grp=grp, since=since, limit=1, offset=idx))[0]
    settings = await db.get_settings(user_id)
    await db.mark_feed_seen(user_id, [(item["source"], item["item_id"])])
    msg = callback.message
    if msg and settings.get("digest_msg") == msg.message_id:
        # Сводку открыли — больше не удаляем её при следующей
        await db.update_settings(user_id, digest_msg=None)
    caption = cards.build_card(item["data"], item["keyword"], settings, header="")
    caption = f"<i>📰 {label} · {idx + 1} из {total}</i>\n" + caption.lstrip()
    caption = caption[:1020]
    markup = cards.viewer_keyboard(item, ftype, fval, idx, total)
    photo = item["data"].get("image") or FSInputFile(NO_PHOTO)
    if msg and (msg.photo or msg.animation):
        try:
            await msg.edit_media(InputMediaPhoto(media=photo, caption=caption), reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            log.debug("Не удалось сменить фото: %s", e)
    try:
        await msg.answer_photo(photo, caption=caption, reply_markup=markup)
    except TelegramBadRequest:
        await msg.answer(caption, reply_markup=markup)


@router.callback_query(F.data.startswith("fd:v:"))
async def cb_view(callback: CallbackQuery, db: Database) -> None:
    _, _, ftype, fval, idx = callback.data.split(":", 4)
    await safe_answer(callback)
    await show_item(callback, db, ftype, fval, int(idx or 0))


@router.callback_query(F.data.startswith("fd:f:"))
async def cb_view_fav(callback: CallbackQuery, db: Database) -> None:
    _, _, ftype, fval, idx = callback.data.split(":", 4)
    user_id = callback.from_user.id
    brand, grp, _ = await resolve_filter(db, user_id, ftype, fval)
    items = await db.feed_page(user_id, brand=brand, grp=grp, since=int(time.time()) - WEEK, limit=1, offset=int(idx))
    if not items:
        await safe_answer(callback)
        return
    now_fav = await db.toggle_favorite(user_id, items[0]["source"], items[0]["item_id"])
    await safe_answer(callback, "⭐ В избранном" if now_fav else "Убрал из избранного")
    items[0]["fav"] = now_fav
    total = await db.feed_count(user_id, brand=brand, grp=grp, since=int(time.time()) - WEEK)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=cards.viewer_keyboard(items[0], ftype, fval, int(idx), total))
    except Exception:
        pass


@router.callback_query(F.data.startswith("fd:h:"))
async def cb_hide(callback: CallbackQuery, db: Database) -> None:
    """🙈 — скрыть вещь («неинтересно») и сразу показать следующую."""
    _, _, ftype, fval, idx = callback.data.split(":", 4)
    user_id = callback.from_user.id
    brand, grp, _ = await resolve_filter(db, user_id, ftype, fval)
    items = await db.feed_page(user_id, brand=brand, grp=grp, since=int(time.time()) - WEEK, limit=1, offset=int(idx))
    if items:
        await db.set_feed_hidden(user_id, items[0]["source"], items[0]["item_id"], True)
    await safe_answer(callback, "Скрыл — больше не покажу")
    await show_item(callback, db, ftype, fval, int(idx))


@router.callback_query(F.data == "fd:x")
async def cb_close(callback: CallbackQuery, db: Database) -> None:
    """✕ — убрать просмотрщик из чата (если это главная — вернуть главную)."""
    await safe_answer(callback)
    user = await db.get_user(callback.from_user.id)
    msg = callback.message
    if msg and user and user["home_msg_id"] == msg.message_id:
        from handlers.home import restore_home
        await restore_home(callback, db)
        return
    try:
        await msg.delete()
    except Exception:
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("fd:m:"))
async def cb_filter_menu(callback: CallbackQuery, db: Database) -> None:
    user_id = callback.from_user.id
    facets = await db.feed_facets(user_id, since=int(time.time()) - WEEK)
    total = sum(facets["groups"].values())
    rows = [[btn(f"Всё · {total}", "fd:v:all:-:0")]]
    groups = [btn(f"{g} · {n}", f"fd:v:g:{g}:0") for g, n in facets["groups"].items()]
    rows += [groups[i:i + 2] for i in range(0, len(groups), 2)]
    chips = [btn(f"{brands.display_name(k)[:18]} · {n}", f"fd:v:b:{cards.brand_code(k)}:0")
             for k, n in list(facets["brands"].items())[:12]]
    rows += [chips[i:i + 2] for i in range(0, len(chips), 2)]
    rows.append(back_home())
    await safe_answer(callback)
    await show(callback, f"🗂 <b>Что показать?</b>\n\nНаходки за {config.FEED_DAYS} дня — по разделам и брендам.", kb(*rows))


@router.message(F.text == BTN_FEED)
@router.message(Command("feed"))
async def msg_feed(message: Message, db: Database) -> None:
    facets = await db.feed_facets(message.from_user.id, since=int(time.time()) - WEEK)
    total = sum(facets["groups"].values())
    if not total:
        await message.answer("📰 Лента пока пуста — новые находки появятся здесь.",
                             reply_markup=kb([btn("🎯 Бренды", "b:list")]))
        return
    new = await db.feed_count(message.from_user.id, since=int(time.time()) - WEEK, view="new")
    await message.answer(f"📰 <b>Лента</b> · {new} новых · {total} за {config.FEED_DAYS} дня",
                         reply_markup=kb([btn("▶️ Смотреть все", "fd:v:all:-:0")], [btn("🗂 По разделам и брендам", "fd:m:all:-:0")]))


# ----------------------------------------------------------------------
# Уведомления
# ----------------------------------------------------------------------

MODES = {
    "digest": "Сводкой",
    "instant": "Каждое сразу",
    "off": "Только в ленте",
}
EVERY = [15, 30, 60, 180]


async def notify_screen(db: Database, user_id: int):
    s = await db.get_settings(user_id)
    user = await db.get_user(user_id)
    mode = s.get("notify", "digest")
    every = int(s.get("every") or 30)
    paused = bool(user and user["paused"])
    explain = {
        "digest": f"Раз в {every} мин приходит одно сообщение со всеми находками — листаешь их кнопками.",
        "instant": "Каждая находка приходит отдельным сообщением сразу. Удобно, если брендов немного.",
        "off": "Ничего не приходит — находки копятся в ленте, смотришь когда удобно.",
    }[mode]
    text = (f"🔔 <b>Уведомления</b>\n\n<b>{MODES[mode]}.</b> {explain}"
            + ("\n\n⏸ <b>Сейчас пауза</b> — радар работает, но ничего не присылает." if paused else "")
            + f"\n\n🌙 Ночью без звука (00–08 МСК): {'да' if s.get('quiet') else 'нет'}")
    rows = [[btn(("● " if m == mode else "") + label, f"nt:m:{m}") for m, label in MODES.items()]]
    if mode == "digest":
        rows.append([btn(("● " if e == every else "") + (f"{e} мин" if e < 60 else f"{e // 60} ч"), f"nt:e:{e}")
                     for e in EVERY])
    rows.append([btn("🌙 Ночью без звука: " + ("вкл" if s.get("quiet") else "выкл"), "nt:q")])
    rows.append([btn("▶️ Снять с паузы" if paused else "⏸ Пауза", "nt:p")])
    rows.append(back_home())
    return text, kb(*rows)


@router.callback_query(F.data.startswith("nt:"))
async def cb_notify(callback: CallbackQuery, db: Database, monitor: Monitor) -> None:
    user_id = callback.from_user.id
    parts = callback.data.split(":")
    if parts[1] == "m" and parts[2] in MODES:
        await db.update_settings(user_id, notify=parts[2])
    elif parts[1] == "e" and parts[2].isdigit() and int(parts[2]) in EVERY:
        await db.update_settings(user_id, every=int(parts[2]))
    elif parts[1] == "q":
        s = await db.get_settings(user_id)
        await db.update_settings(user_id, quiet=not s.get("quiet"))
    elif parts[1] == "p":
        user = await db.get_user(user_id)
        await db.set_paused(user_id, not bool(user and user["paused"]))
    monitor.forget_settings(user_id)
    text, markup = await notify_screen(db, user_id)
    await show(callback, text, markup)
    await safe_answer(callback, None if parts[1] == "open" else "Сохранил")
