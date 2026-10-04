"""
Настройки: вид карточки, перевод, тихие часы, доставка и комиссия байера.
Всё переключается кнопками на одном экране.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery

from db import Database
from handlers.common import back_home, btn, kb, safe_answer, show
from monitor import Monitor

router = Router(name="settings")


def _money(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


async def settings_screen(db: Database, user_id: int):
    s = await db.get_settings(user_id)
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"<b>Карточка:</b> {'подробная — с продавцом и интересом к вещи' if s['card'] == 'full' else 'компактная — только главное'}\n"
        f"<b>Заголовки:</b> {'перевод на русский, оригинал спрятан' if s['title'] == 'ru' else 'как на площадке'}\n"
        f"<b>Тихие часы 00:00–08:00 (МСК):</b> {'включены — ночью без звука' if s['quiet'] else 'выключены'}\n\n"
        "<b>Для расчёта выгоды</b>\n"
        f"Доставка Китай → Россия: <b>{_money(float(s['delivery']))} ₽/кг</b>\n"
        f"Комиссия байера: <b>{float(s['fee']):g}%</b>\n"
        "<i>Поставь цифры своего байера и карго — тогда «📊 Выгода» посчитает точно.</i>"
    )
    markup = kb(
        [btn("🗂 Карточка: " + ("подробная" if s["card"] == "full" else "компактная"), "st:card")],
        [btn("🈶 Заголовки: " + ("перевод" if s["title"] == "ru" else "оригинал"), "st:title")],
        [btn("🌙 Тихие часы: " + ("вкл" if s["quiet"] else "выкл"), "st:quiet")],
        [btn("−100", "st:d:-100"), btn(f"🚚 {_money(float(s['delivery']))} ₽/кг", "noop"), btn("+100", "st:d:100")],
        [btn("−1%", "st:f:-1"), btn(f"🤝 байер {float(s['fee']):g}%", "noop"), btn("+1%", "st:f:1")],
        back_home(),
    )
    return text, markup


@router.callback_query(F.data == "st:open")
async def cb_settings(callback: CallbackQuery, db: Database) -> None:
    text, markup = await settings_screen(db, callback.from_user.id)
    await show(callback, text, markup)
    await safe_answer(callback)


@router.callback_query(F.data.startswith("st:"))
async def cb_settings_change(callback: CallbackQuery, db: Database, monitor: Monitor) -> None:
    user_id = callback.from_user.id
    s = await db.get_settings(user_id)
    parts = callback.data.split(":")
    action = parts[1]
    if action == "card":
        s = await db.update_settings(user_id, card="compact" if s["card"] == "full" else "full")
    elif action == "title":
        s = await db.update_settings(user_id, title="orig" if s["title"] == "ru" else "ru")
    elif action == "quiet":
        s = await db.update_settings(user_id, quiet=not s["quiet"])
    elif action == "d":
        value = max(0, min(5000, float(s["delivery"]) + int(parts[2])))
        s = await db.update_settings(user_id, delivery=value)
    elif action == "f":
        value = max(0, min(30, float(s["fee"]) + int(parts[2])))
        s = await db.update_settings(user_id, fee=value)
    monitor.forget_settings(user_id)
    text, markup = await settings_screen(db, user_id)
    await show(callback, text, markup)
    await safe_answer(callback, "Сохранил")
