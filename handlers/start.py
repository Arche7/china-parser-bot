"""
Первое знакомство: /start, заставка, слайды «как это работает», пробный период.
"""

import logging
import os

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

import config
import plans
import texts
from db import Database
from handlers.common import (
    REPLY_KB, btn, esc, human_date, kb, safe_answer, trial_days_text, webapp_button,
)
from handlers.home import send_home

log = logging.getLogger(__name__)
router = Router(name="start")

ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
INTRO = os.path.join(ASSETS, "intro.mp4")

# Telegram выдаёт каждой загруженной картинке file_id. Запоминаем его,
# чтобы второй раз не загружать файл заново — так слайды листаются мгновенно.
_file_ids: dict[str, str] = {}


def _media(name: str):
    if name in _file_ids:
        return _file_ids[name]
    return FSInputFile(os.path.join(ASSETS, "onboarding", name))


def _remember(name: str, message: Message) -> None:
    if message.photo:
        _file_ids[name] = message.photo[-1].file_id
    elif message.animation:
        _file_ids[name] = message.animation.file_id
    elif message.video:
        _file_ids[name] = message.video.file_id


def slide_caption(index: int) -> str:
    return texts.ONBOARDING[index][1].replace("{trial_days}", trial_days_text())


async def slide_keyboard(db: Database, user_id: int, index: int):
    last = len(texts.ONBOARDING) - 1
    nav = []
    if index > 0:
        nav.append(btn("‹", f"ob:{index - 1}"))
    nav.append(btn(f"{index + 1} / {last + 1}", "noop"))
    if index < last:
        nav.append(btn("›", f"ob:{index + 1}"))
    rows = [nav]
    if index == last:
        user = await db.get_user(user_id)
        if await db.has_access(user_id):
            rows.append([btn("➕ Добавить первый бренд", "b:add:0")])
        elif user and not user["trial_used"] and config.TRIAL_DAYS:
            rows.append([btn(f"🎁 Начать бесплатно — {trial_days_text()}", "trial")])
        else:
            rows.append([btn("💎 Выбрать тариф", "pl:open")])
        rows.append([btn("Главная", "h:home")])
    else:
        rows.append([btn("Пропустить →", "h:home")])
    return kb(*rows)


async def send_slide(message: Message, db: Database, user_id: int, index: int) -> None:
    name = texts.ONBOARDING[index][0]
    sent = await message.answer_photo(
        _media(name), caption=slide_caption(index), reply_markup=await slide_keyboard(db, user_id, index)
    )
    _remember(name, sent)


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db: Database, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    record = await db.get_user(user.id)
    is_new = record is not None and not record["settings"] and not record["home_msg_id"]

    # Реферальная ссылка: t.me/бот?start=ref_123
    arg = (command.args or "").strip()
    if arg.startswith("ref_") and arg[4:].isdigit():
        await db.set_ref(user.id, int(arg[4:]))
    # ИИ-помощник из приложения: t.me/бот?start=ai или ?start=ai_goofish_123 (с контекстом вещи)
    if arg == "ai" or arg.startswith("ai_"):
        from handlers.assistant import open_assistant
        parts = arg.split("_", 2)
        source, item_id = (parts[1], parts[2]) if len(parts) == 3 else (None, None)
        await open_assistant(message, db, state, user.id, source, item_id)
        return
    # Ссылка сразу на тарифы: t.me/бот?start=plans
    if arg == "plans":
        from handlers.plans_ui import plans_screen
        text, markup = await plans_screen(db, user.id)
        await message.answer(text, reply_markup=markup)
        return

    name = esc(user.first_name or "друг")
    if is_new:
        caption = texts.WELCOME_NEW.format(name=name)
        markup = kb([webapp_button()] if webapp_button() else [],
                    [btn("▶️ Как это работает", "ob:0")],
                    [btn(f"🎁 Сразу попробовать — {trial_days_text()}", "trial")] if config.TRIAL_DAYS else [],
                    [btn("Главная", "h:home")])
        try:
            if os.path.exists(INTRO) or "intro" in _file_ids:
                sent = await message.answer_animation(
                    _file_ids.get("intro") or FSInputFile(INTRO), caption=caption, reply_markup=markup
                )
                if sent.animation:
                    _file_ids["intro"] = sent.animation.file_id
            else:
                await message.answer(caption, reply_markup=markup)
        except Exception as e:
            log.warning("Не удалось отправить заставку: %s", e)
            await message.answer(caption, reply_markup=markup)
        await db.update_settings(user.id)  # отмечаем, что приветствие уже было
        return

    await message.answer(texts.WELCOME_BACK.format(name=name), reply_markup=REPLY_KB)
    await send_home(message, db, animated=True)


@router.callback_query(F.data.startswith("ob:"))
async def cb_slide(callback: CallbackQuery, db: Database) -> None:
    try:
        index = max(0, min(int(callback.data[3:]), len(texts.ONBOARDING) - 1))
    except ValueError:
        index = 0
    name = texts.ONBOARDING[index][0]
    markup = await slide_keyboard(db, callback.from_user.id, index)
    msg = callback.message
    # Если это уже сообщение со слайдом — листаем на месте, иначе присылаем первый слайд
    if msg and msg.photo:
        try:
            edited = await msg.edit_media(
                InputMediaPhoto(media=_media(name), caption=slide_caption(index)), reply_markup=markup
            )
            if isinstance(edited, Message):
                _remember(name, edited)
        except Exception as e:
            log.debug("Слайд не изменился: %s", e)
    elif msg:
        await send_slide(msg, db, callback.from_user.id, index)
    await safe_answer(callback)


@router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await safe_answer(callback)


@router.callback_query(F.data == "trial")
async def cb_trial(callback: CallbackQuery, db: Database) -> None:
    user_id = callback.from_user.id
    if not config.TRIAL_DAYS:
        await safe_answer(callback, "Пробный период сейчас недоступен", alert=True)
        return
    until = await db.start_trial(user_id, config.TRIAL_DAYS)
    if until is None:
        if await db.has_access(user_id):
            await safe_answer(callback, "У тебя уже есть доступ 🙂")
        else:
            await safe_answer(callback, texts.TRIAL_USED, alert=True)
        return
    await safe_answer(callback, "Готово! 🎁")
    text = texts.TRIAL_STARTED.format(until=human_date(until), brands=plans.TRIAL.brands)
    await callback.message.answer(
        text,
        reply_markup=kb([btn("➕ Выбрать бренды", "b:add:0")]),
    )
