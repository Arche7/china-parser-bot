"""
Тарифы и «пригласи друга».

Оплаты пока нет: кнопка «Подключить» объясняет, как получить доступ
через поддержку. Когда подключим оплату (Telegram Stars или другую),
поменяется только функция cb_plan_buy.
"""

from aiogram import F, Router
from aiogram.types import CallbackQuery

import config
import plans
from db import Database
from handlers.common import (
    back_home, btn, esc, human_date, is_admin, kb, plural, safe_answer, show, support_url,
    trial_days_text, url_btn, user_plan,
)

router = Router(name="plans")


async def plans_screen(db: Database, user_id: int):
    user = await db.get_user(user_id)
    current = await user_plan(db, user_id)
    active = await db.has_access(user_id)
    lines = ["💎 <b>Тарифы</b>", ""]
    if is_admin(user_id):
        lines.append("Ты админ — у тебя всё без ограничений.\n")
    elif active:
        lines.append(f"Сейчас: <b>{esc(current.title)}</b> до {human_date(user['sub_until'])}\n")
    for p in plans.PAID_PLANS:
        mark = " ← твой" if active and p.code == current.code else ""
        lines.append(f"<b>{p.title}</b> — {plans.rub(p.price_rub)}/мес{mark}")
        lines.append(f"<i>{esc(p.tagline)}</i>")
        lines.append(f"{p.brands} брендов · каждые {p.interval_min} мин · "
                     f"{p.legit_checks} легит-чеков\n")
    lines.append("Чем быстрее проверка — тем раньше ты пишешь продавцу. "
                 "На Goofish хорошие вещи по хорошей цене уходят за часы.")
    rows = [[btn(p.title, f"pl:p:{p.code}") for p in plans.PAID_PLANS]]
    if user and not user["trial_used"] and not active and config.TRIAL_DAYS:
        rows.append([btn(f"🎁 Попробовать {trial_days_text()} бесплатно", "trial")])
    rows.append([btn(f"🤝 Пригласи друга — +{config.REFERRAL_BONUS_DAYS} дней", "pl:ref")])
    rows.append(back_home())
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "pl:open")
async def cb_plans(callback: CallbackQuery, db: Database) -> None:
    text, markup = await plans_screen(db, callback.from_user.id)
    await show(callback, text, markup)
    await safe_answer(callback)


@router.callback_query(F.data.startswith("pl:p:"))
async def cb_plan(callback: CallbackQuery, db: Database) -> None:
    plan = plans.ALL_PLANS.get(callback.data.split(":")[2])
    if not plan:
        await safe_answer(callback)
        return
    lines = [f"💎 <b>{plan.title}</b> — {plans.rub(plan.price_rub)}/мес", f"<i>{esc(plan.tagline)}</i>", ""]
    lines += [f"✓ {esc(perk)}" for perk in plan.perks]
    if plan.seats:
        taken = await db.count_plan_seats(plan.code)
        left = max(0, plan.seats - taken)
        lines.append(f"\n🔥 Осталось мест: <b>{left}</b> из {plan.seats}. "
                     "Мест мало специально: чем меньше людей видят находку первыми — тем она ценнее.")
    lines.append(f"\nЗа 3 месяца — скидка 15%, за год — 30%.")
    await show(callback, "\n".join(lines),
               kb([btn("Подключить", f"pl:buy:{plan.code}")], [btn("‹ Все тарифы", "pl:open")]))
    await safe_answer(callback)


@router.callback_query(F.data.startswith("pl:buy:"))
async def cb_plan_buy(callback: CallbackQuery) -> None:
    plan = plans.ALL_PLANS.get(callback.data.split(":")[2], plans.PRO)
    user_id = callback.from_user.id
    text = (
        f"Отлично, <b>{plan.title}</b> 🙌\n\n"
        "Оплата прямо в боте появится совсем скоро. А пока подключаем вручную — "
        "это быстро: напиши в поддержку название тарифа и свой ID:\n\n"
        f"<code>{plan.title} · {user_id}</code>\n\n"
        "<i>Нажми на строку выше — она скопируется.</i>"
    )
    rows = []
    if support_url():
        rows.append([url_btn("✍️ Написать в поддержку", support_url())])
    rows.append([btn("‹ Все тарифы", "pl:open")])
    await show(callback, text, kb(*rows))
    await safe_answer(callback)


@router.callback_query(F.data == "pl:ref")
async def cb_referral(callback: CallbackQuery, db: Database) -> None:
    me = await callback.bot.me()
    user_id = callback.from_user.id
    link = f"https://t.me/{me.username}?start=ref_{user_id}"
    came, paid = await db.count_refs(user_id)
    text = (
        "🤝 <b>Приглашай друзей</b>\n\n"
        f"Друг приходит по твоей ссылке и получает пробный период. Как только он оформит "
        f"тариф — тебе +{config.REFERRAL_BONUS_DAYS} {plural(config.REFERRAL_BONUS_DAYS, 'день', 'дня', 'дней')} "
        "к подписке. Без ограничений по количеству.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Пришли по ссылке: <b>{came}</b> · оформили тариф: <b>{paid}</b>"
    )
    share = f"https://t.me/share/url?url={link}&text=Бот,%20который%20находит%20брендовые%20вещи%20в%20Китае%20раньше%20всех"
    await show(callback, text, kb([url_btn("📤 Поделиться ссылкой", share)], [btn("‹ Тарифы", "pl:open")]))
    await safe_answer(callback)
