"""
Главная (картинка HUNTR с подписью и кнопками), помощь.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
import os

from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

import config
import rates
import texts
from db import Database
from handlers.common import (
    OLD_HOME, REPLY_KB, back_home, brand_limits_text, btn, count_own, esc, human_date,
    is_admin, kb, now, plural, safe_answer, show, user_plan, webapp_button,
)
from monitor import watch_interval

router = Router(name="home")
BANNER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "home.jpg")


async def home_screen(db: Database, user_id: int, first_name: str | None = None):
    """Главная: 3 короткие строки и 5 кнопок. Остальное — в «Ещё»."""
    user = await db.get_user(user_id)
    plan = await user_plan(db, user_id)
    watches = await db.list_watches(user_id)
    has_access = await db.has_access(user_id)
    settings = await db.get_settings(user_id)
    since = now() - config.FEED_DAYS * 86400
    new_count = await db.feed_count(user_id, since=since, view="new")
    paused = bool(user and user["paused"])

    if is_admin(user_id):
        status = "админ"
    elif has_access:
        status = f"{esc(plan.title)} до {human_date(user['sub_until'])}"
    else:
        status = "доступа нет"
    lines = [f"<b>{esc(config.BRAND_NAME)}</b> · <i>{status}</i>"]
    if watches:
        active = [w for w in watches if not w["paused"]]
        intervals = sorted({watch_interval({"is_admin": is_admin(user_id), "plan": plan.code,
                                             "keyword": w["keyword"]}) for w in active}) or [plan.interval_min]
        every = f"{intervals[0]}" if len(intervals) == 1 else f"{intervals[0]}–{intervals[-1]}"
        lines.append(f"🎯 {len(watches)} {plural(len(watches), 'бренд', 'бренда', 'брендов')} · каждые {every} мин")
    else:
        lines.append("🎯 Брендов пока нет — добавь первый")
    mode = settings.get("notify", "digest")
    if paused:
        notif = "⏸ пауза"
    elif mode == "digest":
        notif = f"сводка раз в {settings.get('every', 30)} мин"
    elif mode == "instant":
        notif = "каждая сразу"
    else:
        notif = "без уведомлений"
    lines.append(f"🆕 {new_count} {plural(new_count, 'новая', 'новые', 'новых')} · {notif}")

    rows = []
    if webapp_button():
        rows.append([webapp_button()])
    rows += [
        [btn(f"📰 Лента · {new_count}" if new_count else "📰 Лента", "fd:v:all:-:0"), btn("🎯 Бренды", "b:list")],
        [btn("⭐ Избранное", "f:list"), btn("⋯ Ещё", "h:more")],
    ]
    if not has_access:
        rows = [[btn("🎁 Попробовать бесплатно", "trial")] if user and not user["trial_used"] and config.TRIAL_DAYS
                else [btn("💎 Выбрать тариф", "pl:open")]] + rows
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "h:more")
async def cb_more(callback: CallbackQuery) -> None:
    text = (f"⋯ <b>Ещё</b>\n\n<i>Курс: 1 ¥ = {rates.cny_rub():.2f} ₽ · {rates.source_label()}</i>")
    await show(callback, text, kb(
        [btn("🔔 Уведомления", "nt:open"), btn("⚙️ Настройки", "st:open")],
        [btn("💎 Тариф", "pl:open"), btn("❓ Помощь", "h:help")],
        back_home(),
    ))
    await safe_answer(callback)


_banner_id: str | None = None
_intro_id: str | None = None
INTRO = os.path.join(os.path.dirname(BANNER), "intro.mp4")


async def send_home(message: Message, db: Database, animated: bool = False) -> None:
    """
    Главная — картинка HUNTR (или ролик «как это работает» при /start)
    с подписью и кнопками; остальные экраны меняют подпись на месте.
    """
    global _banner_id, _intro_id
    text, markup = await home_screen(db, message.chat.id)
    try:
        if animated and os.path.exists(INTRO):
            sent = await message.answer_animation(_intro_id or FSInputFile(INTRO), caption=text, reply_markup=markup)
            if sent.animation:
                _intro_id = sent.animation.file_id
        else:
            sent = await message.answer_photo(_banner_id or FSInputFile(BANNER), caption=text, reply_markup=markup)
            if sent.photo:
                _banner_id = sent.photo[-1].file_id
    except Exception:
        sent = await message.answer(text, reply_markup=markup)
    await db.set_home_msg(message.chat.id, sent.message_id)


@router.message(F.text.in_(OLD_HOME))
@router.message(Command("menu"))
async def msg_home(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    await send_home(message, db)


@router.callback_query(F.data == "h:home")
async def cb_home(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await restore_home(callback, db)


async def restore_home(callback: CallbackQuery, db: Database) -> None:
    """Вернуть в это сообщение главную (картинку HUNTR с подписью и кнопками)."""
    global _banner_id
    text, markup = await home_screen(db, callback.from_user.id)
    msg = callback.message
    await safe_answer(callback)
    if msg and (msg.photo or msg.animation):
        if msg.animation:
            await show(callback, text, markup)
            return
        # Возвращаем картинку HUNTR (в ленте здесь было фото вещи)
        try:
            edited = await msg.edit_media(InputMediaPhoto(media=_banner_id or FSInputFile(BANNER), caption=text),
                                          reply_markup=markup)
            if isinstance(edited, Message) and edited.photo:
                _banner_id = edited.photo[-1].file_id
            return
        except Exception:
            pass
    if msg and msg.text is not None:
        await show(callback, text, markup)
    else:
        await send_home(msg, db)


@router.callback_query(F.data == "h:pause")
async def cb_pause(callback: CallbackQuery, db: Database) -> None:
    user = await db.get_user(callback.from_user.id)
    paused = not bool(user and user["paused"])
    await db.set_paused(callback.from_user.id, paused)
    await safe_answer(callback, "Пауза. Бренды на месте, просто молчу" if paused else "Снова на связи 🎯")
    text, markup = await home_screen(db, callback.from_user.id)
    await show(callback, text, markup)


@router.message(Command("pause"))
async def cmd_pause(message: Message, db: Database) -> None:
    await db.set_paused(message.from_user.id, True)
    await message.answer("⏸ Поставил на паузу. Бренды сохранены — /resume, чтобы продолжить.", reply_markup=REPLY_KB)


@router.message(Command("resume"))
async def cmd_resume(message: Message, db: Database) -> None:
    await db.set_paused(message.from_user.id, False)
    await message.answer("▶️ Снова присматриваю за брендами.", reply_markup=REPLY_KB)


@router.callback_query(F.data == "h:help")
async def cb_help(callback: CallbackQuery) -> None:
    await show(callback, texts.HELP, kb([btn("▶️ Показать слайды", "ob:0")], back_home()))
    await safe_answer(callback)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=kb([btn("▶️ Показать слайды", "ob:0")], back_home()))


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")
