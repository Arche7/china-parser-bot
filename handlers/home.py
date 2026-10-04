"""
Пульт (главный экран), помощь, пауза.
"""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import rates
import texts
from db import Database
from handlers.common import (
    BTN_HOME, REPLY_KB, back_home, brand_limits_text, btn, count_own, esc, human_date,
    is_admin, kb, now, plural, safe_answer, show, user_plan, webapp_button,
)
from monitor import watch_interval

router = Router(name="home")


async def home_screen(db: Database, user_id: int, first_name: str | None = None):
    user = await db.get_user(user_id)
    plan = await user_plan(db, user_id)
    watches = await db.list_watches(user_id)
    own = await count_own(db, user_id)
    found_day = await db.count_sent_since(user_id, now() - 86400)
    has_access = await db.has_access(user_id)

    lines = [f"<b>{esc(config.BRAND_NAME)}</b> · пульт", ""]
    if is_admin(user_id):
        lines.append("👑 Админ · без ограничений")
    elif has_access:
        until = human_date(user["sub_until"])
        lines.append(f"💎 Тариф <b>{esc(plan.title)}</b> · до {until}")
    else:
        lines.append("🔒 Доступа нет — радар выключен")

    if watches:
        active = [w for w in watches if not w["paused"]]
        intervals = sorted({watch_interval({"is_admin": is_admin(user_id), "plan": plan.code,
                                             "keyword": w["keyword"]}) for w in active}) or [plan.interval_min]
        every = f"{intervals[0]}" if len(intervals) == 1 else f"{intervals[0]}–{intervals[-1]}"
        lines.append(f"🎯 Брендов: {brand_limits_text(plan, len(watches), own)} · проверка каждые {every} мин")
    else:
        lines.append("🎯 Брендов пока нет — добавь первый, это 10 секунд")
    lines.append(f"📬 За сутки: {found_day} {plural(found_day, 'находка', 'находки', 'находок')}")
    if user and user["paused"]:
        lines.append("\n⏸ <b>Уведомления на паузе</b>")
    lines.append(f"\n<i>1 ¥ = {rates.cny_rub():.2f} ₽ · {rates.source_label()}</i>")

    paused = bool(user and user["paused"])
    rows = [
        [btn("🎯 Мои бренды", "b:list"), btn("➕ Добавить", "b:add:0")],
        [btn("⭐ Избранное", "f:list"), btn("🛡 Легит-чек", "lg:start")],
        [btn("▶️ Включить" if paused else "⏸ Пауза", "h:pause"), btn("⚙️ Настройки", "st:open")],
        [btn("💎 Тариф", "pl:open"), btn("❓ Как это работает", "h:help")],
    ]
    if webapp_button():
        rows.insert(0, [webapp_button()])
    if not has_access:
        rows = [[btn("🎁 Попробовать бесплатно", "trial")] if user and not user["trial_used"] and config.TRIAL_DAYS
                else [btn("💎 Выбрать тариф", "pl:open")]] + rows
    return "\n".join(lines), kb(*rows)


async def send_home(message: Message, db: Database) -> None:
    """Новый пульт внизу чата (старый остаётся, но это не страшно)."""
    text, markup = await home_screen(db, message.chat.id)
    sent = await message.answer(text, reply_markup=markup)
    await db.set_home_msg(message.chat.id, sent.message_id)


@router.message(F.text == BTN_HOME)
@router.message(Command("menu"))
async def msg_home(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    await send_home(message, db)


@router.callback_query(F.data == "h:home")
async def cb_home(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, markup = await home_screen(db, callback.from_user.id)
    await show(callback, text, markup)
    await safe_answer(callback)


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
