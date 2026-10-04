"""
Оплата звёздами Telegram: проверка перед оплатой, зачисление, возвраты.

  pre_checkout_query  — Telegram спрашивает «можно ли принять оплату?».
                        Проверяем тариф и свободные места ELITE. Ответить нужно
                        за 10 секунд, поэтому здесь ничего тяжёлого.
  successful_payment  — звёзды списаны. Включаем тариф, сохраняем платёж,
                        отменяем старую подписку при смене тарифа,
                        даём бонус пригласившему.
  /paysupport, /terms — поддержка по оплатам и условия. Telegram требует их
                        у ботов, которые продают за звёзды.
  /refund <id> <charge_id> — админ: вернуть звёзды и закрыть доступ.
  /payments            — админ: последние оплаты.
"""

import html
import logging
import time
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, PreCheckoutQuery

import config
import plans
import texts
from db import Database
from handlers.common import REPLY_KB, btn, human_date, is_admin, kb, support_url, url_btn

log = logging.getLogger(__name__)
router = Router(name="payments")


def parse_payload(payload: str) -> tuple[str, plans.Plan, int] | None:
    """'sub:pro:123' -> ('sub', PRO, 1); 'once:pro:3' -> ('once', PRO, 3)"""
    parts = (payload or "").split(":")
    if len(parts) != 3 or parts[0] not in ("sub", "once"):
        return None
    plan = plans.ALL_PLANS.get(parts[1])
    if not plan or plan not in plans.PAID_PLANS:
        return None
    months = 1 if parts[0] == "sub" else int(parts[2]) if parts[2].isdigit() else 0
    if months not in (1, 3):
        return None
    return parts[0], plan, months


def expected_amount(kind: str, plan: plans.Plan, months: int) -> int | None:
    return plan.price_stars if kind == "sub" else plans.quarter_stars(plan)


async def reward_referrer(bot: Bot, db: Database, user_id: int) -> int | None:
    """Бонус пригласившему за первую оплату друга. Возвращает id пригласившего."""
    user = await db.get_user(user_id)
    if not user or not user["ref_by"] or user["ref_rewarded"]:
        return None
    await db.mark_ref_rewarded(user_id)
    until = await db.grant(user["ref_by"], config.REFERRAL_BONUS_DAYS)
    try:
        await bot.send_message(
            user["ref_by"],
            f"🤝 Твой друг оплатил подписку — дарю +{config.REFERRAL_BONUS_DAYS} дней. "
            f"Доступ теперь до {human_date(until)}. Спасибо!",
        )
    except Exception:
        pass
    return user["ref_by"]


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, db: Database) -> None:
    parsed = parse_payload(query.invoice_payload)
    if not parsed or query.currency != "XTR":
        await query.answer(ok=False, error_message="Этот счёт устарел. Открой тарифы в боте ещё раз.")
        return
    kind, plan, months = parsed
    if query.total_amount != expected_amount(kind, plan, months):
        await query.answer(ok=False, error_message="Цена изменилась. Открой тарифы в боте ещё раз.")
        return
    if plan.seats:
        taken = await db.count_plan_seats(plan.code)
        mine = await db.user_plan_code(query.from_user.id) == plan.code and await db.has_access(query.from_user.id)
        if taken - (1 if mine else 0) >= plan.seats:
            await query.answer(ok=False, error_message=f"Все места {plan.title} заняты. Напиши в поддержку.")
            return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def got_payment(message: Message, bot: Bot, db: Database) -> None:
    payment = message.successful_payment
    user_id = message.from_user.id
    parsed = parse_payload(payment.invoice_payload)
    if not parsed:
        log.error("Оплата с непонятным payload: %s", payment.invoice_payload)
        await message.answer("Оплату получил, но не понял, за какой тариф. Напиши в /paysupport — разберёмся.")
        return
    kind, plan, months = parsed
    recurring = bool(payment.subscription_expiration_date)
    is_new = await db.add_payment(payment.telegram_payment_charge_id, user_id, plan.code,
                                  months, payment.total_amount, recurring)
    if not is_new:
        return  # Telegram прислал то же самое ещё раз

    if recurring:
        # Подписка: Telegram сам говорит, до какого момента она оплачена (+1 день запаса)
        until = await db.set_access_until(user_id, payment.subscription_expiration_date + 86400, plan.code)
    else:
        until = await db.grant(user_id, 30 * months, plan.code)

    # Сменил тариф — отменяем старую подписку, чтобы не списывало за два тарифа
    for old in await db.other_subscriptions(user_id, payment.telegram_payment_charge_id):
        old_row = await db.get_payment(old)
        if old_row and (old_row["plan"] != plan.code or not recurring):
            try:
                await bot.edit_user_star_subscription(user_id=user_id, telegram_payment_charge_id=old, is_canceled=True)
            except Exception as e:
                log.warning("Не удалось отменить старую подписку %s: %s", old, e)
            await db.stop_recurring(old)

    if payment.is_recurring and not payment.is_first_recurring:
        text = (f"⭐ Подписка <b>{plan.title}</b> продлена до {human_date(until)}. "
                "Радар работает дальше — спасибо, что с нами!")
    else:
        text = (f"🎉 <b>{plan.title}</b> подключён до {human_date(until)}!\n\n"
                f"Брендов: до {plan.brands} · проверка каждые {plan.interval_min} мин · "
                f"легит-чеков: {plan.legit_checks} в месяц.\n\n"
                "Открой пульт и добавь бренды — первые находки покажу сразу.")
    await message.answer(text, reply_markup=REPLY_KB)
    await message.answer("Что дальше?", reply_markup=kb([btn("➕ Добавить бренды", "b:add:0")],
                                                        [btn("🏠 Открыть пульт", "h:home")]))
    await reward_referrer(bot, db, user_id)
    for admin in config.ADMIN_IDS:
        try:
            await bot.send_message(
                admin,
                f"💰 Оплата: <code>{user_id}</code> · {plan.title} · {months} мес. · "
                f"{plans.stars(payment.total_amount)}{' · подписка' if recurring else ''}\n"
                f"charge_id: <code>{payment.telegram_payment_charge_id}</code>",
            )
        except Exception:
            pass


@router.message(Command("paysupport"))
@router.message(Command("support"))
async def cmd_paysupport(message: Message) -> None:
    rows = [[url_btn("✍️ Написать в поддержку", support_url())]] if support_url() else []
    await message.answer(
        "🛟 <b>Поддержка по оплате</b>\n\n"
        "Если звёзды списались, а доступ не включился, или есть вопрос по подписке — "
        "напиши нам и приложи свой ID:\n"
        f"<code>{message.from_user.id}</code>\n\n"
        "Отменить подписку можно в любой момент: Настройки Telegram → Мои звёзды → подписки. "
        "Доступ сохранится до конца оплаченного периода.",
        reply_markup=kb(*rows) if rows else None,
    )


@router.message(Command("terms"))
async def cmd_terms(message: Message) -> None:
    await message.answer(texts.TERMS)


# ---------------------------------------------------------------- админ

@router.message(Command("refund"))
async def cmd_refund(message: Message, command: CommandObject, bot: Bot, db: Database) -> None:
    if not is_admin(message.from_user.id):
        return
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit():
        await message.answer("Формат: <code>/refund 123456789 charge_id</code>\n"
                             "charge_id есть в уведомлении об оплате и в /payments")
        return
    user_id, charge_id = int(args[0]), args[1]
    try:
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
    except Exception as e:
        await message.answer(f"Telegram не принял возврат: {html.escape(str(e))}")
        return
    await db.mark_refunded(charge_id)
    await db.revoke(user_id)
    await message.answer(f"↩️ Звёзды возвращены, доступ <code>{user_id}</code> закрыт.")


@router.message(Command("payments"))
async def cmd_payments(message: Message, db: Database) -> None:
    if not is_admin(message.from_user.id):
        return
    rows = await db.list_payments(20)
    month = await db.stars_since(int(time.time()) - 30 * 86400)
    usd = month * config.STAR_USD_PAYOUT
    lines = [
        f"💰 За 30 дней: <b>{plans.stars(month)}</b> ≈ ${usd:,.0f} ≈ {plans.rub(usd * config.USD_RUB_RATE)} к выводу",
        "<i>Telegram зачисляет звёзды на вывод не сразу, а через несколько недель.</i>",
        "",
    ]
    for r in rows:
        when = datetime.fromtimestamp(r["created_at"]).strftime("%d.%m %H:%M")
        flags = " · подписка" if r["recurring"] else ""
        flags += " · ВОЗВРАТ" if r["refunded"] else ""
        lines.append(f"{when} <code>{r['user_id']}</code> {plans.ALL_PLANS.get(r['plan'], plans.PRO).title} "
                     f"{r['months']} мес. · {plans.stars(r['stars'])}{flags}\n<code>{r['charge_id']}</code>")
    if not rows:
        lines.append("Оплат пока нет.")
    await message.answer("\n".join(lines))
