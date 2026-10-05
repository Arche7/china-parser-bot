"""
Все экраны и команды бота.

  start.py      — /start, заставка, слайды «как это работает», пробный период
  home.py       — главная (картинка HUNTR + кнопки), помощь
  brands_ui.py  — каталог брендов, добавление, бюджет, мои бренды
  feed_ui.py    — лента в чате (листалка по брендам и разделам), уведомления и сводки
  listing.py    — кнопки под объявлением: легит-чек, выгода, избранное
  settings_ui.py — настройки
  plans_ui.py   — тарифы, кнопки оплаты звёздами, «пригласи друга»
  payments.py   — приём оплаты звёздами, возвраты, /paysupport, /terms
  assistant.py  — 🤖 ИИ-помощник по переписке с продавцом (ELITE)
  admin.py      — команды админа
"""

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject

import config
import texts
from db import Database
from handlers import admin, assistant, brands_ui, feed_ui, home, listing, payments, plans_ui, settings_ui, start
from handlers.common import OLD_HOME, btn, kb, trial_days_text

# Что можно делать БЕЗ подписки: познакомиться, посмотреть тарифы, включить пробный период
PUBLIC_COMMANDS = {"/start", "/help", "/id", "/menu", "/paysupport", "/support", "/terms"}
PUBLIC_TEXT = set(OLD_HOME)
# as:open — рассказ про ИИ-помощника (продаёт ELITE); сам помощник проверяет доступ внутри
PUBLIC_CALLBACKS = ("ob:", "noop", "trial", "pl:", "h:home", "h:help", "as:open")


class AccessMiddleware(BaseMiddleware):
    """Сохраняет пользователя в базу и пускает дальше только тех, у кого есть доступ."""

    def __init__(self, db: Database):
        self.db = db

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        await self.db.upsert_user(user.id, user.username, user.first_name)

        if user.id in config.ADMIN_IDS:
            return await handler(event, data)
        # Сообщение об оплате звёздами пропускаем всегда — именно оно и включает доступ
        if isinstance(event, Message) and event.successful_payment:
            return await handler(event, data)
        if isinstance(event, Message) and event.text:
            first = event.text.split()[0].split("@")[0].lower()
            if first in PUBLIC_COMMANDS or event.text in PUBLIC_TEXT:
                return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data and event.data.startswith(PUBLIC_CALLBACKS):
            return await handler(event, data)
        if await self.db.has_access(user.id):
            return await handler(event, data)

        record = await self.db.get_user(user.id)
        can_trial = bool(config.TRIAL_DAYS and record and not record["trial_used"])
        text = texts.LOCKED.format(trial_days=trial_days_text()) if can_trial else texts.LOCKED_NO_TRIAL
        markup = kb([btn(f"🎁 {trial_days_text()} бесплатно", "trial")] if can_trial else [],
                    [btn("💎 Тарифы", "pl:open")])
        if isinstance(event, Message):
            await event.answer(text, reply_markup=markup)
        elif isinstance(event, CallbackQuery):
            await event.answer()
            if event.message:
                await event.message.answer(text, reply_markup=markup)
        return None


# Запасной обработчик — если человек написал что-то непонятное
fallback = Router(name="fallback")


@fallback.message(F.text)
async def unknown_text(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Не совсем понял 🙂 Всё управление — кнопками:",
        reply_markup=kb([btn("Главная", "h:home"), btn("➕ Добавить бренд", "b:add:0")]),
    )


def setup(db: Database) -> Router:
    root = Router(name="root")
    middleware = AccessMiddleware(db)
    root.message.outer_middleware(middleware)
    root.callback_query.outer_middleware(middleware)
    # Порядок важен: админ раньше всех, запасной обработчик — последним
    # assistant — сразу после start: пока помощник открыт, он забирает текст и фото себе
    for r in (admin.router, payments.router, start.router, assistant.router, home.router, feed_ui.router, brands_ui.router, listing.router,
              settings_ui.router, plans_ui.router, fallback):
        root.include_router(r)
    return root
