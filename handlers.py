"""
Обработчики команд и кнопок Telegram.

Команды для пользователя:
  /start  — приветствие и меню
  /add    — добавить бренд:  /add stone island 500-3000
  /preset — добавить готовый набор брендов (см. brands.py)
  /list   — мои бренды (с кнопками удаления)
  /del    — удалить бренд:   /del stone island
  /pause  — поставить уведомления на паузу
  /resume — снять с паузы
  /help   — помощь
  /id     — показать мой Telegram ID

Команды для админа:
  /grant <id> <дни>  — выдать доступ (например, после оплаты)
  /revoke <id>       — забрать доступ
  /users             — список пользователей
  /stats             — статистика и примерный расход на Apify
"""

import asyncio
import html
import re
import time
from datetime import datetime
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

import brands
import config
from brands_cn import chinese_name
from db import Database
from monitor import Monitor

router = Router()

# Ссылки на фоновые задачи, чтобы Python не удалил их раньше времени
_background_tasks: set[asyncio.Task] = set()

# Тексты кнопок главного меню
BTN_ADD = "➕ Добавить бренд"
BTN_LIST = "📋 Мои бренды"
BTN_PRESET = "⭐ Готовый набор"
BTN_PAUSE = "⏸ Пауза"
BTN_RESUME = "▶️ Продолжить"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "✖️ Отмена"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_ADD), KeyboardButton(text=BTN_LIST)],
        [KeyboardButton(text=BTN_PRESET)],
        [KeyboardButton(text=BTN_PAUSE), KeyboardButton(text=BTN_RESUME)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
)

CANCEL_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True
)

# Команды и кнопки, доступные даже без подписки
PUBLIC_COMMANDS = {"/start", "/help", "/id"}
PUBLIC_BUTTONS = {BTN_HELP}
# Кнопки меню — их нельзя принять за название бренда
MENU_BUTTONS = {BTN_ADD, BTN_LIST, BTN_PRESET, BTN_PAUSE, BTN_RESUME, BTN_HELP, BTN_CANCEL}


def _preset_names() -> str:
    return ", ".join(brands.BRANDS[key]["title"] for key in brands.PRESET)


HELP_TEXT = (
    "🤖 <b>Я слежу за объявлениями на китайских площадках</b>\n\n"
    "Добавь бренд — и я буду присылать новые объявления с ним, "
    "как только они появятся.\n\n"
    "<b>Как добавить бренд:</b>\n"
    "• нажми «➕ Добавить бренд» и напиши название, или\n"
    "• команда <code>/add stone island</code>\n\n"
    "<b>Готовый набор:</b> кнопка «⭐ Готовый набор» или /preset — сразу "
    f"добавит {_preset_names()} с ценой "
    f"{brands.PRESET_PRICE_MIN}–{brands.PRESET_PRICE_MAX} ¥.\n\n"
    "<b>Фильтр по цене (в юанях ¥):</b>\n"
    "• <code>/add arcteryx 500-3000</code> — от 500 до 3000 ¥\n"
    "• <code>/add chrome hearts -2000</code> — до 2000 ¥\n"
    "• <code>/add rick owens 1000-</code> — от 1000 ¥\n\n"
    "<b>Другие команды:</b>\n"
    "/list — мои бренды\n"
    "/del название — удалить бренд\n"
    "/pause и /resume — пауза уведомлений\n"
    "/id — мой Telegram ID\n\n"
    "🇨🇳 <b>Написания и китайские названия:</b> для брендов из готового "
    "набора я сам ищу и по латинице, и по-китайски (например, <code>gucci</code> "
    "и <code>古驰</code>), понимаю сокращения (<code>lv</code>, <code>ysl</code>) "
    "и отсеиваю пометки подделок (高仿, 复刻, A货 …). Для других брендов, если я "
    "знаю китайское название, предложу добавить и его. "
    "Одно и то же объявление дважды не придёт.\n\n"
    "Площадки: Goofish (闲鱼) ✅ · 95分 — скоро"
)


class AddBrand(StatesGroup):
    waiting_keyword = State()


# ----------------------------------------------------------------------
# Проверка доступа: сохраняем пользователя в базу и пускаем
# только админов и тех, у кого активна подписка.
# ----------------------------------------------------------------------

class AccessMiddleware(BaseMiddleware):
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

        if isinstance(event, Message) and event.text:
            first_word = event.text.split()[0].split("@")[0].lower()
            if first_word in PUBLIC_COMMANDS or event.text in PUBLIC_BUTTONS:
                return await handler(event, data)

        if await self.db.has_access(user.id):
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(
                "🔒 У тебя пока нет доступа к боту.\n\n"
                f"Твой Telegram ID: <code>{user.id}</code>\n"
                "Отправь его администратору, чтобы получить доступ."
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("🔒 Нет доступа", show_alert=True)
        return None


# ----------------------------------------------------------------------
# Вспомогательные функции
# ----------------------------------------------------------------------

PRICE_RE = re.compile(r"^¥?(\d+)?\s*-\s*¥?(\d+)?$")


def parse_brand_input(text: str) -> tuple[str, float | None, float | None]:
    """
    'stone island 500-3000' -> ('stone island', 500.0, 3000.0)
    'nike'                  -> ('nike', None, None)
    """
    parts = text.strip().split()
    price_min = price_max = None
    if parts:
        match = PRICE_RE.match(parts[-1])
        if match and (match.group(1) or match.group(2)):
            price_min = float(match.group(1)) if match.group(1) else None
            price_max = float(match.group(2)) if match.group(2) else None
            parts = parts[:-1]
    keyword = " ".join(parts).strip().lower()
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    return keyword, price_min, price_max


def price_text(price_min: float | None, price_max: float | None) -> str:
    if price_min is None and price_max is None:
        return "любая цена"
    if price_min is not None and price_max is not None:
        return f"{price_min:.0f}–{price_max:.0f} ¥"
    if price_min is not None:
        return f"от {price_min:.0f} ¥"
    return f"до {price_max:.0f} ¥"


def search_text(keyword: str) -> str:
    """'gucci' -> 'ищу: gucci, 古驰' (только для брендов из brands.py)."""
    if not brands.get_brand(keyword):
        return ""
    return "ищу: " + ", ".join(brands.search_queries(keyword))


async def merge_old_variants(db: Database, user_id: int, key: str) -> list[str]:
    """
    Если у пользователя уже есть этот бренд в другом написании
    (например, «古驰», а добавляем «gucci») — удаляем старую запись,
    чтобы бренд не проверялся дважды. Возвращает удалённые написания.
    """
    removed = []
    for watch in await db.list_watches(user_id):
        if watch["keyword"] != key and brands.canonical(watch["keyword"]) == key:
            await db.delete_watch(user_id, watch["id"])
            removed.append(watch["keyword"])
    return removed


async def build_list(db: Database, user_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    watches = await db.list_watches(user_id)
    if not watches:
        return (
            "У тебя пока нет брендов. Нажми «➕ Добавить бренд», «⭐ Готовый набор» "
            "или напиши <code>/add название</code>.",
            None,
        )
    lines = [f"📋 <b>Твои бренды</b> ({len(watches)}/{config.MAX_BRANDS_PER_USER}):\n"]
    buttons = []
    for i, w in enumerate(watches, start=1):
        line = (
            f"{i}. <b>{html.escape(brands.display_name(w['keyword']))}</b> — "
            f"{price_text(w['price_min'], w['price_max'])}"
        )
        extra = search_text(w["keyword"])
        if extra:
            line += f"\n    <i>{html.escape(extra)}</i>"
        lines.append(line)
        buttons.append(
            [InlineKeyboardButton(
                text=f"❌ Удалить «{brands.display_name(w['keyword'])[:30]}»",
                callback_data=f"del:{w['id']}",
            )]
        )
    lines.append(f"\nПроверяю каждые {config.CHECK_INTERVAL_MIN} мин.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


async def add_brand(message: Message, db: Database, monitor: Monitor, raw_text: str) -> None:
    keyword, price_min, price_max = parse_brand_input(raw_text)
    user_id = message.from_user.id

    if len(keyword) < 2:
        await message.answer("Название слишком короткое. Пример: <code>/add stone island</code>")
        return
    if len(keyword) > 60:
        await message.answer("Слишком длинное название — максимум 60 символов.")
        return

    # 'lv', '路易威登', 'Louis Vuitton' -> один бренд 'louis vuitton'
    keyword = brands.canonical(keyword)
    known = brands.get_brand(keyword) is not None
    removed = await merge_old_variants(db, user_id, keyword) if known else []

    existing = {w["keyword"] for w in await db.list_watches(user_id)}
    if keyword not in existing and len(existing) >= config.MAX_BRANDS_PER_USER:
        await message.answer(
            f"Достигнут лимит: {config.MAX_BRANDS_PER_USER} брендов. "
            "Удали ненужный через /list."
        )
        return

    name = html.escape(brands.display_name(keyword))
    is_new = await db.add_watch(user_id, keyword, price_min, price_max)
    details = ""
    if known:
        details = f"\n🔎 {html.escape(search_text(keyword))} + все написания из списка"
    if removed:
        details += "\n♻️ Объединил со старыми записями: " + html.escape(", ".join(removed))

    if not is_new:
        await message.answer(
            f"✏️ Бренд <b>{name}</b> уже был — обновил фильтр: "
            f"{price_text(price_min, price_max)}.{details}",
            reply_markup=MAIN_MENU,
        )
        # С новой ценой это новый поиск — сразу запоминаем текущие объявления
        start_preview(monitor, user_id, keyword, price_min, price_max, show=False)
        return

    await message.answer(
        f"✅ Добавил <b>{name}</b> ({price_text(price_min, price_max)}).{details}\n"
        "Ищу текущие объявления — это может занять до пары минут…",
        reply_markup=MAIN_MENU,
    )
    start_preview(monitor, user_id, keyword, price_min, price_max)
    if not known:
        await offer_chinese_name(message, db, user_id, keyword)


def start_preview(
    monitor: Monitor,
    user_id: int,
    keyword: str,
    price_min: float | None,
    price_max: float | None,
    show: bool = True,
) -> None:
    """Первый запрос делаем в фоне, чтобы бот не «зависал» на время поиска."""
    task = asyncio.create_task(
        monitor.preview_new_keyword(user_id, keyword, price_min, price_max, show=show)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def offer_chinese_name(message: Message, db: Database, user_id: int, keyword: str) -> None:
    """Если знаем китайское название бренда — предлагаем добавить и его."""
    cn = chinese_name(keyword)
    if not cn or cn == keyword or await db.get_watch_by_keyword(user_id, cn):
        return
    watch = await db.get_watch_by_keyword(user_id, keyword)
    if not watch:
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Добавить {cn}", callback_data=f"cn:{watch['id']}"),
        InlineKeyboardButton(text="Не надо", callback_data="cnno"),
    ]])
    await message.answer(
        f"🇨🇳 На Goofish <b>{html.escape(keyword)}</b> часто пишут по-китайски: "
        f"<b>{cn}</b>. Добавить и это название? Так найдётся больше объявлений.\n\n"
        "<i>Это займёт ещё одно место в списке брендов и ещё один запрос к Apify "
        "при каждой проверке. Одно и то же объявление дважды не придёт.</i>",
        reply_markup=keyboard,
    )


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# ----------------------------------------------------------------------
# Общие команды
# ----------------------------------------------------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    user = message.from_user
    if await db.has_access(user.id):
        await message.answer(
            f"Привет, {html.escape(user.first_name or 'друг')}! 👋\n\n" + HELP_TEXT,
            reply_markup=MAIN_MENU,
        )
    else:
        await message.answer(
            f"Привет, {html.escape(user.first_name or 'друг')}! 👋\n\n"
            "Я присылаю новые объявления с китайских площадок (Goofish/闲鱼) "
            "по брендам, которые ты выберешь.\n\n"
            f"🔒 Доступ пока закрыт. Твой Telegram ID: <code>{user.id}</code>\n"
            "Отправь его администратору, чтобы получить доступ."
        )


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=MAIN_MENU)


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Твой Telegram ID: <code>{message.from_user.id}</code>")


# ----------------------------------------------------------------------
# Отмена ввода
# ----------------------------------------------------------------------

@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=MAIN_MENU)


# ----------------------------------------------------------------------
# Добавление бренда
# ----------------------------------------------------------------------

@router.message(Command("add"))
async def cmd_add(
    message: Message, command: CommandObject, db: Database, monitor: Monitor, state: FSMContext
) -> None:
    if command.args:
        await state.clear()
        await add_brand(message, db, monitor, command.args)
    else:
        await state.set_state(AddBrand.waiting_keyword)
        await message.answer(
            "Напиши название бренда.\n"
            "Можно сразу с ценой в юанях: <code>stone island 500-3000</code>",
            reply_markup=CANCEL_MENU,
        )


@router.message(F.text == BTN_ADD)
async def btn_add(message: Message, state: FSMContext) -> None:
    await state.set_state(AddBrand.waiting_keyword)
    await message.answer(
        "Напиши название бренда.\n"
        "Можно сразу с ценой в юанях: <code>stone island 500-3000</code>",
        reply_markup=CANCEL_MENU,
    )


@router.message(StateFilter(AddBrand.waiting_keyword), F.text, ~F.text.in_(MENU_BUTTONS))
async def got_keyword(message: Message, db: Database, monitor: Monitor, state: FSMContext) -> None:
    if message.text.startswith("/"):
        await message.answer("Сначала напиши название бренда или нажми «✖️ Отмена».")
        return
    await state.clear()
    await add_brand(message, db, monitor, message.text)


# ----------------------------------------------------------------------
# Готовый набор брендов
# ----------------------------------------------------------------------

@router.message(Command("preset"))
@router.message(F.text == BTN_PRESET)
async def cmd_preset(message: Message, db: Database, monitor: Monitor, state: FSMContext) -> None:
    """Добавляет все бренды из brands.PRESET с ценой PRESET_PRICE_MIN–MAX."""
    await state.clear()
    user_id = message.from_user.id
    price_min = float(brands.PRESET_PRICE_MIN)
    price_max = float(brands.PRESET_PRICE_MAX)

    added, updated, skipped, merged = [], [], [], []
    for key in brands.PRESET:
        merged += await merge_old_variants(db, user_id, key)
        existing = {w["keyword"] for w in await db.list_watches(user_id)}
        if key not in existing and len(existing) >= config.MAX_BRANDS_PER_USER:
            skipped.append(brands.display_name(key))
            continue
        if await db.add_watch(user_id, key, price_min, price_max):
            added.append(key)
        else:
            updated.append(key)
        # Молча запоминаем текущие объявления — присылать будем только новые
        start_preview(monitor, user_id, key, price_min, price_max, show=False)

    lines = [f"⭐ <b>Готовый набор</b> — цена {price_text(price_min, price_max)}\n"]
    for key in added + updated:
        mark = "✅" if key in added else "✏️"
        lines.append(
            f"{mark} <b>{html.escape(brands.display_name(key))}</b> — "
            f"<i>{html.escape(search_text(key))}</i>"
        )
    if merged:
        lines.append("\n♻️ Объединил со старыми записями: " + html.escape(", ".join(merged)))
    if skipped:
        lines.append(
            f"\n⚠️ Не влезли (лимит {config.MAX_BRANDS_PER_USER} брендов): "
            + html.escape(", ".join(skipped))
            + ". Удали лишнее через /list и нажми кнопку ещё раз."
        )
    lines.append(
        "\nСейчас молча запоминаю текущие объявления, чтобы не засыпать тебя старыми. "
        f"Новые начнут приходить в течение ~{config.CHECK_INTERVAL_MIN} мин.\n"
        "Подделки (高仿, 复刻, A货, 1:1 …) и объявления без названия бренда отсеиваю."
    )
    await message.answer("\n".join(lines), reply_markup=MAIN_MENU)


# ----------------------------------------------------------------------
# Список и удаление
# ----------------------------------------------------------------------

@router.message(Command("list"))
@router.message(F.text == BTN_LIST)
async def cmd_list(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, keyboard = await build_list(db, message.from_user.id)
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(callback: CallbackQuery, db: Database) -> None:
    try:
        watch_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    keyword = await db.delete_watch(callback.from_user.id, watch_id)
    await callback.answer(
        f"Удалил «{brands.display_name(keyword)}»" if keyword else "Уже удалено"
    )
    text, keyboard = await build_list(db, callback.from_user.id)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        pass  # сообщение не изменилось — не страшно


@router.callback_query(F.data.startswith("cn:"))
async def cb_add_chinese(callback: CallbackQuery, db: Database, monitor: Monitor) -> None:
    user_id = callback.from_user.id
    try:
        watch_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    watch = await db.get_watch(user_id, watch_id)
    cn = chinese_name(watch["keyword"]) if watch else None
    if not cn:
        await callback.answer("Исходный бренд уже удалён", show_alert=True)
        return
    if await db.get_watch_by_keyword(user_id, cn) is None:
        if await db.count_watches(user_id) >= config.MAX_BRANDS_PER_USER:
            await callback.answer(
                f"Лимит {config.MAX_BRANDS_PER_USER} брендов. Удали лишний через /list.",
                show_alert=True,
            )
            return
        await db.add_watch(user_id, cn, watch["price_min"], watch["price_max"])
        start_preview(monitor, user_id, cn, watch["price_min"], watch["price_max"])
    await callback.answer(f"Добавил {cn}")
    try:
        await callback.message.edit_text(
            f"✅ Добавил и китайское название <b>{cn}</b> "
            f"({price_text(watch['price_min'], watch['price_max'])})."
        )
    except Exception:
        pass


@router.callback_query(F.data == "cnno")
async def cb_skip_chinese(callback: CallbackQuery) -> None:
    await callback.answer("Ок")
    try:
        await callback.message.delete()
    except Exception:
        pass


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        await message.answer("Напиши, что удалить: <code>/del stone island</code>\nИли открой /list.")
        return
    keyword, _, _ = parse_brand_input(command.args)
    user_id = message.from_user.id
    # Сначала пробуем как написано, потом — как бренд из списка (/del lv)
    deleted = await db.delete_watch_by_keyword(user_id, keyword)
    if not deleted:
        deleted = await db.delete_watch_by_keyword(user_id, brands.canonical(keyword))
    if deleted:
        await message.answer(f"🗑 Удалил <b>{html.escape(brands.display_name(keyword))}</b>.")
    else:
        await message.answer("Такого бренда нет в твоём списке. Проверь /list.")


# ----------------------------------------------------------------------
# Пауза
# ----------------------------------------------------------------------

@router.message(Command("pause"))
@router.message(F.text == BTN_PAUSE)
async def cmd_pause(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    await db.set_paused(message.from_user.id, True)
    await message.answer("⏸ Уведомления на паузе. Чтобы продолжить — /resume.")


@router.message(Command("resume"))
@router.message(F.text == BTN_RESUME)
async def cmd_resume(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    await db.set_paused(message.from_user.id, False)
    await message.answer("▶️ Уведомления снова включены.")


# ----------------------------------------------------------------------
# Админ-команды
# ----------------------------------------------------------------------

@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, db: Database) -> None:
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Формат: <code>/grant 123456789 30</code> (ID и количество дней)")
        return
    user_id, days = int(args[0]), int(args[1])
    until = await db.grant(user_id, days)
    await message.answer(f"✅ Доступ для <code>{user_id}</code> до {fmt_date(until)}.")
    try:
        await message.bot.send_message(
            user_id,
            f"🎉 Тебе открыт доступ к боту до {fmt_date(until)}!\nНажми /start, чтобы начать.",
        )
    except Exception:
        await message.answer("(Пользователю не удалось написать — пусть сначала нажмёт /start.)")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, db: Database) -> None:
    if not is_admin(message.from_user.id):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: <code>/revoke 123456789</code>")
        return
    await db.revoke(int(arg))
    await message.answer(f"Доступ для <code>{arg}</code> закрыт.")


@router.message(Command("users"))
async def cmd_users(message: Message, db: Database) -> None:
    if not is_admin(message.from_user.id):
        return
    users = await db.list_users()
    if not users:
        await message.answer("Пользователей пока нет.")
        return
    now = int(time.time())
    lines = [f"👥 Пользователей: {len(users)}\n"]
    for u in users[:50]:
        if u["user_id"] in config.ADMIN_IDS:
            status = "админ"
        elif u["sub_until"] > now:
            status = f"до {fmt_date(u['sub_until'])}"
        else:
            status = "нет доступа"
        name = "@" + u["username"] if u["username"] else (u["first_name"] or "—")
        pause = " ⏸" if u["paused"] else ""
        lines.append(
            f"<code>{u['user_id']}</code> {html.escape(name)} — {status}, брендов: {u['brands']}{pause}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, monitor: Monitor) -> None:
    if not is_admin(message.from_user.id):
        return
    watches = await db.active_watches()
    jobs = monitor.group_watches(watches)
    queries = sum(len(brands.search_queries(keyword)) for keyword, _, _ in jobs)
    sources_count = max(1, len(monitor.sources))
    runs_per_day = (24 * 60) / max(1, config.CHECK_INTERVAL_MIN)
    items_per_day = queries * sources_count * runs_per_day * config.MAX_ITEMS
    cost_per_day = items_per_day / 1000 * config.APIFY_PRICE_PER_1000

    uptime_h = (time.time() - monitor.started_at) / 3600
    last = (
        datetime.fromtimestamp(monitor.last_cycle_at).strftime("%H:%M:%S")
        if monitor.last_cycle_at else "ещё не было"
    )
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Активных брендов (уникальных): {len(jobs)}\n"
        f"Поисковых запросов за проверку: {queries}\n"
        f"Подписок на бренды всего: {len(watches)}\n"
        f"Интервал: {config.CHECK_INTERVAL_MIN} мин, объявлений за запрос: {config.MAX_ITEMS}\n\n"
        f"💸 Примерный расход Apify (максимум): ~${cost_per_day:.2f}/день, "
        f"~${cost_per_day * 30:.2f}/мес\n"
        "<i>(если по запросу меньше объявлений, чем лимит, — выйдет дешевле; "
        "точные цифры смотри в Apify → Billing)</i>\n\n"
        f"С момента запуска ({uptime_h:.1f} ч):\n"
        f"• циклов проверки: {monitor.cycles} (последний: {last})\n"
        f"• запросов к площадкам: {monitor.searches}\n"
        f"• получено объявлений: {monitor.items_fetched}\n"
        f"• отсеяно (подделки/не тот бренд): {monitor.items_filtered}\n"
        f"• отправлено сообщений: {monitor.messages_sent}"
    )


# ----------------------------------------------------------------------
# Любой другой текст
# ----------------------------------------------------------------------

@router.message(F.text)
async def fallback(message: Message) -> None:
    await message.answer(
        "Не понял 🙂 Чтобы добавить бренд, нажми «➕ Добавить бренд» "
        "или напиши <code>/add название</code>. Помощь — /help.",
        reply_markup=MAIN_MENU,
    )
