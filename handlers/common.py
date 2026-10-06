"""
Общие помощники для всех экранов бота.

Главная идея интерфейса: у пользователя есть одно сообщение-«главная»,
и почти все кнопки не шлют новые сообщения, а меняют это же сообщение.
Получается как приложение: экраны сменяют друг друга, чат не засоряется.
"""

import asyncio
import dataclasses
import html
import time
from datetime import datetime

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
    WebAppInfo,
)

import brands
import config
import plans
import rates
from db import Database

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# Нижней клавиатуры (Главная · Лента · Бренды) больше нет: она закрывала
# объявления при листании. Всё есть в кнопке меню HUNTR, в /start и в «/».
# REPLY_KB теперь УБИРАЕТ старую клавиатуру у тех, у кого она осталась.
BTN_HOME = "Главная"
BTN_FEED = "Лента"
BTN_BRANDS = "Бренды"
BTN_FAVS = "Избранное"
# Старые подписи кнопок — если у кого-то осталась старая клавиатура, она всё ещё работает
OLD_HOME = {"🏠 Пульт", BTN_HOME}
OLD_BRANDS = {"🎯 Бренды", BTN_BRANDS}
OLD_FAVS = {"⭐ Избранное", BTN_FAVS}
REPLY_KB = ReplyKeyboardRemove()

_background: set[asyncio.Task] = set()


def run_background(coro) -> None:
    """Запускает долгую задачу в фоне, чтобы бот не «зависал»."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def plural(n: int, one: str, few: str, many: str) -> str:
    """plural(3, 'день', 'дня', 'дней') -> 'дня'"""
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def trial_days_text() -> str:
    return f"{config.TRIAL_DAYS} {plural(config.TRIAL_DAYS, 'день', 'дня', 'дней')}"


async def trial_eligible(db: Database, user_id: int) -> bool:
    """
    Может ли человек включить тест-драйв (пробный период): тест включён в настройках,
    человек его ещё не пробовал и доступа сейчас нет. Админам тест не нужен.
    Лежит здесь (а не в handlers/trial.py), потому что нужен user_plan ниже —
    так нет круговых импортов.
    """
    if not config.TRIAL_DAYS or is_admin(user_id):
        return False
    user = await db.get_user(user_id)
    if not user or user["trial_used"]:
        return False
    return not await db.has_access(user_id)


def human_date(ts: int) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt.day} {MONTHS[dt.month - 1]}"


def esc(text) -> str:
    return html.escape(str(text))


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url_btn(text: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=url)


def kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def back_home() -> list[InlineKeyboardButton]:
    return [btn("‹ Главная", "h:home")]


def support_url() -> str | None:
    return f"https://t.me/{config.SUPPORT_USERNAME}" if config.SUPPORT_USERNAME else None


def webapp_button(text: str | None = None) -> InlineKeyboardButton | None:
    if not config.WEBAPP_URL:
        return None
    return InlineKeyboardButton(text=f"📱 {text or 'Открыть ' + config.BRAND_NAME}",
                                web_app=WebAppInfo(url=config.WEBAPP_URL))


def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


async def user_plan(db: Database, user_id: int) -> plans.Plan:
    """
    Тариф человека с учётом докупленных «своих» брендов: каждый оплаченный
    слот добавляет +1 к общему числу брендов и +1 к своим.
    """
    if is_admin(user_id):
        return plans.ADMIN
    # Кто ещё не пробовал тест-драйв, может сразу выбрать первый бренд — тест включится сам.
    # Пока он не включился, лимиты считаем по пробному тарифу, а не по PRO
    # (get_plan(None) по старой памяти отдаёт PRO) — иначе можно было бы добавить больше, чем даёт тест.
    if await trial_eligible(db, user_id):
        return plans.TRIAL
    plan = plans.get_plan(await db.user_plan_code(user_id))
    extra = await db.own_slots(user_id) if await db.has_access(user_id) else 0
    if extra:
        plan = dataclasses.replace(plan, brands=plan.brands + extra,
                                   own_brands=plan.own_brands + extra, extra_own=extra)
    return plan


def price_text(price_min: float | None, price_max: float | None, with_rub: bool = False) -> str:
    def fmt(v: float) -> str:
        return f"{v:,.0f}".replace(",", " ")

    if price_min is None and price_max is None:
        return "любая цена"
    if price_min is not None and price_max is not None:
        text = f"¥{fmt(price_min)}–{fmt(price_max)}"
    elif price_min is not None:
        text = f"от ¥{fmt(price_min)}"
    else:
        text = f"до ¥{fmt(price_max)}"
    if with_rub:
        r = rates.cny_rub()
        if price_min is not None and price_max is not None:
            text += f" <i>(≈ {fmt(price_min * r)}–{fmt(price_max * r)} ₽)</i>"
        elif price_min is not None:
            text += f" <i>(от ≈ {fmt(price_min * r)} ₽)</i>"
        else:
            text += f" <i>(до ≈ {fmt(price_max * r)} ₽)</i>"
    return text


async def feed_days(db: Database, user_id: int) -> int:
    """Сколько дней лента хранит находки — зависит от тарифа (START 3, PRO 7, ELITE 14)."""
    return (await user_plan(db, user_id)).feed_days


async def feed_since(db: Database, user_id: int) -> int:
    return int(time.time()) - await feed_days(db, user_id) * 86400


def days_text(n: int) -> str:
    return f"{n} {plural(n, 'день', 'дня', 'дней')}"


def upsell_kb(feature_plan: "plans.Plan | None", back: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки «Открыть PRO/ELITE» + назад — под подсказкой о том, чего нет на тарифе."""
    rows = []
    if feature_plan:
        rows.append([btn(f"💎 Подключить {feature_plan.title}", f"pl:p:{feature_plan.code}")])
    rows.append([btn("Все тарифы", "pl:open")] + ([btn("‹ Назад", back)] if back else []))
    return kb(*rows)


def brand_limits_text(plan: plans.Plan, total: int, own: int) -> str:
    text = f"{total} из {plan.brands}"
    if plan.own_brands and plan.own_brands < 100:
        text += f" · своих {own} из {plan.own_brands}"
        if plan.extra_own:
            text += f" (+{plan.extra_own} докуплено)"
    return text


async def count_own(db: Database, user_id: int) -> int:
    return sum(1 for w in await db.list_watches(user_id) if not brands.is_catalog(w["keyword"]))


async def show(event: CallbackQuery | Message, text: str, markup: InlineKeyboardMarkup | None = None) -> Message | None:
    """
    Показать экран: для кнопки — меняем текущее сообщение, если это текст;
    если это фото/видео (или изменить нельзя) — присылаем новое.
    """
    if isinstance(event, CallbackQuery):
        msg = event.message
        if msg and msg.text is not None:
            try:
                await msg.edit_text(text, reply_markup=markup)
                return msg
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return msg
        elif msg and (msg.photo or msg.animation) and len(text) <= 1024:
            # Экран с картинкой (главная, лента): меняем подпись, картинка остаётся
            try:
                await msg.edit_caption(caption=text, reply_markup=markup)
                return msg
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    return msg
        if msg:
            return await msg.answer(text, reply_markup=markup)
        return None
    return await event.answer(text, reply_markup=markup)


async def safe_answer(callback: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        await callback.answer(text, show_alert=alert)
    except Exception:
        pass


def now() -> int:
    return int(time.time())
