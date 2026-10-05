"""
Команды админа.

  /grant <id> <дни> [тариф]  — выдать/продлить доступ. Тариф: start, pro, elite
                                (по умолчанию pro). Пример: /grant 123456789 30 elite
  /plan <id> <тариф>         — сменить тариф, не трогая срок
  /revoke <id>               — забрать доступ
  /users                     — пользователи
  /stats                     — статистика и прогноз расходов
  /payments                  — последние оплаты звёздами (handlers/payments.py)
  /export                    — все оплаты файлом для таблицы «HUNTR-финансы»
  /refund <id> <charge_id>   — вернуть звёзды (handlers/payments.py)
  /broadcast                 — ответь этой командой на сообщение, чтобы разослать
                                его всем пользователям (спросит подтверждение)
"""

import asyncio
import html
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

import ai
import apify_guard
import config
import plans
from avito import AvitoPrices
from db import Database
from handlers.common import btn, human_date, is_admin, kb, safe_answer
from handlers.payments import reward_referrer
from monitor import Monitor

router = Router(name="admin")
router.message.filter(lambda m: m.from_user and is_admin(m.from_user.id))

_broadcasts: dict[int, tuple[int, int]] = {}  # admin -> (chat_id, message_id)


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, db: Database) -> None:
    args = (command.args or "").split()
    if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
        await message.answer("Формат: <code>/grant 123456789 30 pro</code> (ID, дни, тариф: start / pro / elite)")
        return
    user_id, days = int(args[0]), int(args[1])
    code = args[2].lower() if len(args) > 2 else "pro"
    if code not in plans.ALL_PLANS or code in ("admin", "trial"):
        await message.answer("Тариф: start, pro или elite")
        return
    plan = plans.ALL_PLANS[code]
    until = await db.grant(user_id, days, code)
    await message.answer(f"✅ <code>{user_id}</code>: {plan.title} до {fmt_date(until)}.")
    try:
        await message.bot.send_message(
            user_id,
            f"🎉 Подключил тариф <b>{plan.title}</b> до {human_date(until)}!\n\n"
            "Нажми /start и добавляй бренды.",
        )
    except Exception:
        await message.answer("(Пользователю не удалось написать — пусть сначала нажмёт /start.)")

    # Бонус тому, кто пригласил
    ref = await reward_referrer(message.bot, db, user_id)
    if ref:
        await message.answer(f"🤝 Пригласившему <code>{ref}</code> +{config.REFERRAL_BONUS_DAYS} дн.")


@router.message(Command("plan"))
async def cmd_plan(message: Message, command: CommandObject, db: Database) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[0].isdigit() or args[1].lower() not in plans.ALL_PLANS:
        await message.answer("Формат: <code>/plan 123456789 elite</code>")
        return
    await db.set_plan(int(args[0]), args[1].lower())
    await message.answer(f"Тариф <code>{args[0]}</code> → {plans.ALL_PLANS[args[1].lower()].title}")


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, db: Database) -> None:
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: <code>/revoke 123456789</code>")
        return
    await db.revoke(int(arg))
    await message.answer(f"Доступ для <code>{arg}</code> закрыт.")


@router.message(Command("users"))
async def cmd_users(message: Message, db: Database) -> None:
    users = await db.list_users()
    if not users:
        await message.answer("Пользователей пока нет.")
        return
    now = int(time.time())
    active = [u for u in users if u["sub_until"] > now]
    by_plan: dict[str, int] = {}
    for u in active:
        by_plan[u["plan"] or "pro"] = by_plan.get(u["plan"] or "pro", 0) + 1
    mrr = sum(plans.get_plan(code).price_stars * n for code, n in by_plan.items())
    lines = [
        f"👥 Всего: {len(users)} · с доступом: {len(active)}",
        "По тарифам: " + (", ".join(f"{plans.get_plan(c).title} {n}" for c, n in by_plan.items()) or "—"),
        f"Если все продлят: ~{plans.stars(mrr)} в месяц ≈ {plans.rub(mrr * config.STAR_USD_PAYOUT * config.USD_RUB_RATE)} к выводу",
        "",
    ]
    for u in users[:40]:
        if u["user_id"] in config.ADMIN_IDS:
            status = "админ"
        elif u["sub_until"] > now:
            status = f"{plans.get_plan(u['plan']).title} до {fmt_date(u['sub_until'])}"
        else:
            status = "нет доступа"
        name = "@" + u["username"] if u["username"] else (u["first_name"] or "—")
        pause = " ⏸" if u["paused"] else ""
        lines.append(f"<code>{u['user_id']}</code> {html.escape(name)} — {status}, брендов: {u['brands']}{pause}")
    await message.answer("\n".join(lines))


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, monitor: Monitor, avito: AvitoPrices) -> None:
    f = await monitor.cost_forecast()
    uptime_h = (time.time() - monitor.started_at) / 3600
    last = datetime.fromtimestamp(monitor.last_cycle_at).strftime("%H:%M:%S") if monitor.last_cycle_at else "ещё не было"
    users = await db.list_users()
    now = int(time.time())
    stars_month = await db.stars_since(now - 30 * 86400)
    payout_rub = stars_month * config.STAR_USD_PAYOUT * config.USD_RUB_RATE
    await message.answer(
        "📊 <b>Статистика</b>\n\n"
        f"Брендов на радаре (уникальных): {f['jobs']} · подписок на бренды: {f['watches']}\n"
        f"Мин. интервал: {config.CHECK_INTERVAL_MIN} мин · объявлений за запрос: {config.MAX_ITEMS}\n\n"
        f"💸 Apify Goofish, максимум: ~${f['usd_day']:.2f}/день ≈ {plans.rub(f['rub_month'])}/мес\n"
        "<i>Реально меньше: «умная экономия» реже проверяет бренды без новинок. "
        "Точные цифры — Apify → Billing.</i>\n"
        f"💰 Оплачено за 30 дней: {plans.stars(stars_month)} ≈ {plans.rub(payout_rub)} к выводу\n"
        f"Итого за месяц: ≈ {plans.rub(payout_rub - f['rub_month'])} (выручка минус Apify по максимуму)\n\n"
        f"ИИ: {'✅ ' + config.AI_MODEL + ' / ' + config.AI_VISION_MODEL if ai.enabled() else '❌ выключен (нет AI_API_KEY)'}\n"
        f"Авито: {'✅ ' + config.AVITO_ACTOR_ID if avito.enabled else '❌ выключено'}\n\n"
        f"С момента запуска ({uptime_h:.1f} ч):\n"
        f"• циклов: {monitor.cycles} (последний: {last})\n"
        f"• запросов к площадкам: {monitor.searches}\n"
        f"• получено объявлений: {monitor.items_fetched}\n"
        f"• отсеяно (подделки/не тот бренд): {monitor.items_filtered}\n"
        f"• отправлено карточек: {monitor.messages_sent}"
    )


@router.message(Command("avitotest"))
async def cmd_avitotest(message: Message, command: CommandObject, avito: AvitoPrices) -> None:
    """Проверка Авито: /avitotest stone island куртка — сделает настоящий запрос к актору."""
    query = (command.args or "stone island куртка").strip()
    parts = query.split()
    await message.answer(f"Проверяю Авито: «{html.escape(query)}»… (до 2 минут)")
    keyword, category = " ".join(parts[:-1]) or query, parts[-1] if len(parts) > 1 else None
    apify_guard.clear()   # проверка вручную — пробуем по-настоящему, даже если недавно был лимит
    market = await avito.market(keyword, category)
    if not avito.enabled:
        await message.answer("Авито выключено (AVITO_ENABLED=0).")
    elif market and market.get("median"):
        await message.answer(f"✅ Работает: {market['count']} цен, медиана {market['median']:.0f} ₽ "
                             f"({market['p25']:.0f}–{market['p75']:.0f} ₽)\n{market['url']}")
    else:
        if avito.last_log == "APIFY_LIMIT" or apify_guard.blocked():
            await message.answer(apify_guard.alert_text())
            return
        tail = html.escape(" \n".join((avito.last_log or "").strip().splitlines()[-12:]))[-3000:]
        await message.answer(f"⚠️ Цен не получил (нашлось: {(market or {}).get('count', 0)}).\n\n"
                             + (f"<b>Конец лога актора:</b>\n<pre>{tail}</pre>" if tail else
                                "Актор ничего не написал в лог — загляни в логи Railway."))


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, db: Database) -> None:
    if not message.reply_to_message:
        await message.answer("Ответь командой /broadcast на сообщение, которое нужно разослать.")
        return
    _broadcasts[message.from_user.id] = (message.chat.id, message.reply_to_message.message_id)
    total = len(await db.all_user_ids())
    active = len(await db.all_user_ids(only_active=True))
    await message.answer(
        f"Разослать это сообщение?\nВсем: {total} · только с доступом: {active}",
        reply_markup=kb([btn(f"📣 Всем ({total})", "adm:bc:all"), btn(f"Только с доступом ({active})", "adm:bc:act")],
                        [btn("Отмена", "adm:bc:no")]),
    )


@router.callback_query(F.data.startswith("adm:bc:"))
async def cb_broadcast(callback: CallbackQuery, db: Database) -> None:
    if not is_admin(callback.from_user.id):
        await safe_answer(callback)
        return
    mode = callback.data.split(":")[2]
    source = _broadcasts.pop(callback.from_user.id, None)
    if mode == "no" or not source:
        await callback.message.edit_text("Рассылка отменена.")
        return
    ids = await db.all_user_ids(only_active=(mode == "act"))
    await callback.message.edit_text(f"📣 Рассылаю {len(ids)}…")
    ok = 0
    for user_id in ids:
        try:
            await callback.bot.copy_message(user_id, source[0], source[1])
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)  # не больше ~20 сообщений в секунду
    await callback.message.answer(f"Готово: доставлено {ok} из {len(ids)}.")
