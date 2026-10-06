"""
Команды админа.

  /grant <id> <дни> [тариф]  — выдать/продлить доступ. Тариф: start, pro, elite
                                (по умолчанию pro). Пример: /grant 123456789 30 elite
  /plan <id> <тариф>         — сменить тариф, не трогая срок
  /revoke <id>               — забрать доступ
  /users                     — пользователи
  /stats                     — статистика и прогноз расходов
  /payments                  — последние оплаты звёздами (handlers/payments.py)
  /export                    — все оплаты файлом (только владелец, OWNER_ID)
  /phototest <id или ссылка> — сколько фото отдаёт Goofish по объявлению
  /refund <id> <charge_id>   — вернуть звёзды (handlers/payments.py)
  /broadcast                 — ответь этой командой на сообщение, чтобы разослать
                                его всем пользователям (спросит подтверждение)
  /report [вчера|7]          — отчёт: кто пришёл, кто заходил, оплаты, подписки,
                                конверсия, топ брендов, ИИ. Без аргумента — за сегодня,
                                «вчера» — за вчера, «7» — за неделю. Каждый день в
                                REPORT_HOUR (по Москве) сводка за вчера приходит владельцу сама.
"""

import asyncio
import html
import logging
import time
from datetime import date, datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

import ai
import apify_guard
import brands
import config
import plans
from avito import AvitoPrices
from db import MSK, Database, msk_today
from handlers.common import MONTHS, btn, human_date, is_admin, kb, safe_answer, show
from handlers.payments import reward_referrer
from monitor import Monitor

log = logging.getLogger("report")

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
        f"Мин. интервал: {config.CHECK_INTERVAL_MIN} мин · объявлений за запрос: {config.MAX_ITEMS}\n"
        + (f"⚠️ <b>CHECK_INTERVAL_MIN={config.CHECK_INTERVAL_MIN}</b> — PRO и ELITE сейчас тоже проверяются "
           f"раз в {config.CHECK_INTERVAL_MIN} мин, хотя в тарифах обещано 15 и 10. Перед продажей поставь "
           f"CHECK_INTERVAL_MIN=10 в Variables на Railway.\n" if config.CHECK_INTERVAL_MIN > plans.ELITE.interval_min else "")
        + "\n"
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
        via = f"\nАктор: <code>{html.escape(avito.last_actor)}</code>" if avito.last_actor else "\n(из кэша за сутки)"
        await message.answer(f"✅ Работает: {market['count']} цен, медиана {market['median']:.0f} ₽ "
                             f"({market['p25']:.0f}–{market['p75']:.0f} ₽){via}\n{market['url']}")
    else:
        if avito.last_log == "APIFY_LIMIT" or apify_guard.blocked():
            await message.answer(apify_guard.alert_text())
            return
        tail = html.escape("\n".join((avito.last_log or "").strip().splitlines()[-16:]))[-3200:]
        await message.answer(f"⚠️ Цен не получил (подходящих: {(market or {}).get('count', 0)}).\n\n"
                             + (f"<b>Что ответили акторы:</b>\n<pre>{tail}</pre>" if tail else
                                "Акторы ничего не написали в лог — загляни в логи Railway."))


@router.message(Command("phototest"))
async def cmd_phototest(message: Message, command: CommandObject, sources: dict) -> None:
    """
    Проверка фото: /phototest 1090297096758 (или ссылка на объявление Goofish).
    Делает настоящий запрос полной карточки и говорит, сколько фото вернул актор.
    Стоит как одна полная карточка на Apify.
    """
    import re
    arg = (command.args or "").strip()
    match = re.search(r"(\d{9,})", arg)
    if not match:
        await message.answer("Формат: <code>/phototest 1090297096758</code> или ссылка на объявление Goofish")
        return
    item_id = match.group(1)
    src = sources.get("goofish")
    if not src:
        await message.answer("Goofish не подключён.")
        return
    await message.answer(f"Загружаю карточку {item_id}… (до 2 минут)")
    apify_guard.clear()
    detail = await src.details(item_id)
    if not detail:
        await message.answer(apify_guard.alert_text() if apify_guard.blocked()
                             else "⚠️ Карточку не получил — подробности в логах Railway (строки «Goofish: карточка»).")
        return
    if detail.get("status") and not detail.get("images"):
        await message.answer(f"Объявление {item_id}: статус «{detail['status']}» — похоже, продано или удалено.")
        return
    images = detail.get("images") or []
    lines = [f"📸 Объявление <code>{item_id}</code>: фото <b>{len(images)}</b>"
             + (" (показываем до 9)" if len(images) >= 9 else "")]
    for i, url in enumerate(images[:9], 1):
        lines.append(f"{i}. <a href=\"{html.escape(url)}\">фото {i}</a>")
    seller = detail.get("seller") or {}
    if any(seller.values()):
        lines.append("\nПродавец: " + ", ".join(f"{k}={v}" for k, v in seller.items() if v not in (None, "")))
    await message.answer("\n".join(lines))


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


# ======================================================================
# Отчётность: /report, кнопки под отчётом и ежедневная сводка владельцу
# ======================================================================
# Режимы отчёта: today — сегодня, yday — вчера, 7 — последние 7 дней (включая сегодня).
# Все даты — по Москве (db.msk_today), админы в цифрах не считаются.

REPORT_MODES = {
    "": "today", "сегодня": "today", "today": "today",
    "вчера": "yday", "yesterday": "yday",
    "7": "7", "неделя": "7", "week": "7",
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
# «За октябрь» — именительный падеж (в handlers.common.MONTHS — родительный: «6 октября»)
MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль",
              "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
SPARK = "▁▂▃▄▅▆▇█"
TG_LIMIT = 4000   # Telegram режет сообщения длиннее 4096 символов — держим запас


def _report_days(mode: str) -> tuple[date, date]:
    """Первый и последний день отчёта (оба включительно)."""
    today = msk_today()
    if mode == "yday":
        day = today - timedelta(days=1)
        return day, day
    if mode == "7":
        return today - timedelta(days=6), today
    return today, today


def _day_label(d: date) -> str:
    """date(2026, 10, 6) -> '6 октября, вт'"""
    return f"{d.day} {MONTHS[d.month - 1]}, {WEEKDAYS[d.weekday()]}"


def _who(row) -> str:
    """Как показать человека: @ник, иначе имя. Обрезаем, чтобы список влез в одно сообщение."""
    name = "@" + row["username"] if row["username"] else (row["first_name"] or "без имени")
    return html.escape(name[:24])


def _plan_title(code: str | None) -> str:
    # 'own' — это не тариф, а докупка «+1 свой бренд» (см. payments.slot)
    if code == "own":
        return "+1 бренд"
    if code == "trial":
        return "пробный"
    return plans.get_plan(code).title


def _sparkline(values: list[int]) -> str:
    """[3, 5, 0, 8] -> '▃▅▁█' — «график» из символов: выше столбик — больше людей."""
    top = max(values) if values else 0
    if not top:
        return SPARK[0] * len(values)
    return "".join(SPARK[round(v / top * (len(SPARK) - 1))] for v in values)


def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")


def _short_date(ts: int) -> str:
    d = datetime.fromtimestamp(ts, MSK)
    return f"{d.day} {MONTHS[d.month - 1][:3]} {d.strftime('%H:%M')}"


def _fit(lines: list[str]) -> str:
    """
    Склеивает строки и, если вдруг получилось длиннее лимита Telegram,
    отрезает лишние строки с конца. Режем по строкам, а не по символам:
    каждая строка сама закрывает свои HTML-теги, поэтому разметка не ломается.
    """
    text = "\n".join(lines)
    while len(text) > TG_LIMIT and len(lines) > 1:
        lines = lines[:-2] + ["…"]
        text = "\n".join(lines)
    return text


# Подписи источников для /report (коды — из ссылок ?start=… в handlers/start.py)
SOURCE_NAMES = {"tt": "TikTok", "ig": "Instagram", "tg": "TG-канал", "vk": "VK", "yt": "YouTube", "—": "без метки"}


async def build_report(db: Database, mode: str) -> str:
    """Собирает текст отчёта (HTML). mode: today / yday / 7."""
    first, last = _report_days(mode)
    single = first == last
    act = await db.report_activity(first, last)
    new_users = await db.report_new_users(first, last)
    trials = await db.report_trials_started(first, last)
    ending = await db.report_trials_ending(24)
    pay = await db.report_payments(first, last)
    subs = await db.report_active_subs()
    expiring = await db.report_expiring(3)
    conv_trials, conv_paid = await db.report_conversion(30)
    top = await db.report_top_brands(10)
    usage = await db.report_usage(first, last)

    title = _day_label(first) if single else f"{first.day} {MONTHS[first.month - 1][:3]} – {last.day} {MONTHS[last.month - 1][:3]}"
    when = {"today": "Сегодня", "yday": "Вчера"}.get(mode, "За 7 дней")
    lines = [f"📊 <b>{html.escape(config.BRAND_NAME)} · отчёт за {title}</b>", ""]

    # 👥 Люди
    lines.append(f"👥 <b>{when}</b>")
    lines.append(f"Заходили: <b>{act['total']}</b> · новых: <b>{new_users}</b>")
    lines.append(f"💬 бот {act['bot']} · 📱 приложение {act['app']} · и там и там {act['both']}")
    # Откуда пришли новые: ссылки t.me/<бот>?start=tt / ig / tg / vk / yt (см. handlers/start.py)
    sources = await db.report_sources(first, last)
    if new_users and any(src != "—" for src, _ in sources):
        lines.append("Откуда новые: " + " · ".join(f"{SOURCE_NAMES.get(src, src)} {n}" for src, n in sources))
    if not single:
        dau = await db.report_dau(first, last)
        values = list(dau.values())
        labels = [f"{WEEKDAYS[date.fromisoformat(d).weekday()]} {n}" for d, n in dau.items()]
        lines.append(f"По дням: <code>{_sparkline(values)}</code>")
        lines.append(" · ".join(labels))
        lines.append(f"В среднем за день: {sum(values) / len(values):.1f}")
    lines.append("")

    # 🎁 Пробный период
    lines.append("🎁 <b>Пробный</b>")
    lines.append(f"Включили: <b>{trials}</b> · кончается за 24 ч: <b>{len(ending)}</b>")
    if ending:
        shown = ", ".join(f"{_who(r)} ({_hhmm(r['sub_until'])})" for r in ending[:8])
        lines.append(shown + (f" и ещё {len(ending) - 8}" if len(ending) > 8 else ""))
    lines.append("")

    # 💰 Оплаты — «≈ ₽ к выводу» считаем так же, как /stats: звёзды × STAR_USD_PAYOUT × USD_RUB_RATE
    lines.append("💰 <b>Оплаты</b>")
    if pay["count"]:
        payout_rub = pay["stars"] * config.STAR_USD_PAYOUT * config.USD_RUB_RATE
        lines.append(f"<b>{pay['count']}</b> · {plans.stars(pay['stars'])} ≈ {plans.rub(payout_rub)} к выводу")
        lines.append(f"Первые: {pay['first']} · повторные: {pay['repeat']}")
        lines.append(" · ".join(f"{_plan_title(code)} {n} ({plans.stars(st)})"
                                for code, (n, st) in sorted(pay["by_plan"].items(), key=lambda x: -x[1][1])))
        # Кто заплатил: до 10 человек, свежие сверху. 🔁 — повторная оплата (продление, смена тарифа)
        people = pay.get("people", [])
        for r in people[:10]:
            when = _hhmm(r["created_at"]) if single else _short_date(r["created_at"])
            term = f" {r['months']} мес" if r["plan"] != "own" else ""
            again = " 🔁" if r["repeat"] else ""
            lines.append(f"• {when} {_who(r)} <code>{r['user_id']}</code> · "
                         f"{_plan_title(r['plan'])}{term} · {plans.stars(r['stars'])}{again}")
        if len(people) > 10:
            lines.append(f"…и ещё {len(people) - 10}")
    else:
        lines.append("Оплат не было")
    lines.append("")

    # 📦 Подписки прямо сейчас (не зависят от выбранного дня)
    lines.append("📦 <b>Подписки сейчас</b>")
    paid = {code: n for code, n in subs.items() if code != "trial"}
    parts = [f"{_plan_title(code)} {n}" for code, n in paid.items()]
    if subs.get("trial"):
        parts.append(f"пробный {subs['trial']}")
    lines.append(" · ".join(parts) or "Пока ни одной")
    if paid:
        # Та же формула, что в /users: сколько придёт за месяц, если все платные продлят
        mrr = sum(plans.get_plan(code).price_stars * n for code, n in paid.items())
        lines.append(f"Если все продлят: ~{plans.stars(mrr)}/мес "
                     f"≈ {plans.rub(mrr * config.STAR_USD_PAYOUT * config.USD_RUB_RATE)} к выводу")
    if expiring:
        lines.append(f"Кончаются за 3 дня: <b>{len(expiring)}</b>")
        for r in expiring[:10]:
            auto = " 🔄" if r["auto"] else ""
            lines.append(f"• {_who(r)} <code>{r['user_id']}</code> — {_plan_title(r['plan'])}, "
                         f"{_short_date(r['sub_until'])}{auto}")
        if len(expiring) > 10:
            lines.append(f"…и ещё {len(expiring) - 10}")
        if any(r["auto"] for r in expiring):
            lines.append("<i>🔄 — автопродление, Telegram спишет звёзды сам</i>")
    lines.append("")

    # 📈 Конверсия
    lines.append("📈 <b>Конверсия 30 дн</b>")
    if conv_trials:
        lines.append(f"Пробный → оплата: <b>{conv_paid}</b> из {conv_trials} ({conv_paid * 100 / conv_trials:.0f}%)")
    else:
        lines.append("Пробных за 30 дней не было")
    lines.append("")

    # 🔥 Топ брендов
    lines.append("🔥 <b>Топ брендов</b>")
    lines.append(" · ".join(f"{html.escape(brands.display_name(k))} {n}" for k, n in top) or "Брендов пока нет")
    lines.append("")

    # 🤖 ИИ — по дням видно только помощника в приложении, остальное — итог месяца (таблица usage месячная)
    month = usage["month"]
    lines.append("🤖 <b>ИИ</b>")
    lines.append(f"Помощник (бот + приложение): {usage['app_msgs']} сообщ. от {usage['app_users']} чел.")
    month_parts = [f"легит-чеков {month.get('legit', (0, 0))[0]}",
                   f"помощник {month.get('assistant', (0, 0))[0]}"]
    if month.get("price", (0, 0))[0]:
        month_parts.append(f"сравнений цен {month['price'][0]}")
    lines.append(f"За {MONTHS_NOM[last.month - 1]}: " + " · ".join(month_parts))
    lines.append("")
    lines.append("<i>Админы не считаются · время московское</i>")
    return _fit(lines)


def report_kb(mode: str):
    """Кнопки под отчётом. Текущий режим помечен точкой, чтобы было видно, что открыто."""
    def tab(text: str, code: str):
        return btn(("• " + text) if code == mode else text, f"rep:{code}")
    return kb([tab("Сегодня", "today"), tab("Вчера", "yday"), tab("7 дней", "7")],
              [btn("👤 Кто заходил", f"rep:who:{mode}")])


async def build_who(db: Database, mode: str) -> str:
    """
    Список тех, кто заходил: свежие сверху, максимум 50.
    Для «7 дней» показываем сегодняшний день (список за неделю был бы слишком длинным).
    """
    day = _report_days(mode)[1]
    rows = await db.report_active_users(day, 50)
    now = int(time.time())
    lines = [f"👤 <b>Кто заходил · {_day_label(day)}</b> ({len(rows)}{'+' if len(rows) == 50 else ''})", ""]
    if not rows:
        lines.append("Никто не заходил.")
    for r in rows:
        where = ("💬" if r["bot"] else "") + ("📱" if r["app"] else "")
        if r["sub_until"] and r["sub_until"] > now:
            status = _plan_title(r["plan"])
        else:
            status = "без доступа"
        new = " 🆕" if r["is_new"] else ""
        lines.append(f"{_hhmm(r['last_at'])} {where} {_who(r)} <code>{r['user_id']}</code> · {status}{new}")
    lines += ["", "<i>💬 бот · 📱 приложение · 🆕 пришёл в этот день · время — последнее действие</i>"]
    return _fit(lines)


@router.message(Command("report"))
async def cmd_report(message: Message, command: CommandObject, db: Database) -> None:
    mode = REPORT_MODES.get((command.args or "").strip().lower())
    if mode is None:
        await message.answer("Формат: <code>/report</code> — сегодня, <code>/report вчера</code>, "
                             "<code>/report 7</code> — неделя.")
        return
    await message.answer(await build_report(db, mode), reply_markup=report_kb(mode))


@router.callback_query(F.data.startswith("rep:"))
async def cb_report(callback: CallbackQuery, db: Database) -> None:
    # Фильтр роутера (router.message.filter) действует только на сообщения,
    # а не на нажатия кнопок — поэтому проверяем админа здесь сами.
    if not is_admin(callback.from_user.id):
        await safe_answer(callback)
        return
    parts = callback.data.split(":")
    if parts[1] == "who":
        mode = parts[2] if len(parts) > 2 and parts[2] in ("today", "yday", "7") else "today"
        await show(callback, await build_who(db, mode), kb([btn("‹ К отчёту", f"rep:{mode}")]))
    elif parts[1] in ("today", "yday", "7"):
        await show(callback, await build_report(db, parts[1]), report_kb(parts[1]))
    await safe_answer(callback)


def _seconds_until(hour: int) -> float:
    """Сколько секунд ждать до ближайших hour:00 по Москве (сегодня или завтра)."""
    now = datetime.now(MSK)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def daily_report_loop(bot, db: Database) -> None:
    """
    Каждый день в REPORT_HOUR (по Москве) присылает владельцу отчёт за вчера.
    Запускается из bot.py фоновой задачей. Любая ошибка (нет сети, владелец
    заблокировал бота…) пишется в лог, а цикл продолжает работать —
    отчёт не должен ронять бота.
    """
    if config.REPORT_HOUR < 0 or not config.OWNER_ID:
        log.info("Ежедневный отчёт выключен (REPORT_HOUR=-1 или не задан OWNER_ID)")
        return
    log.info("Ежедневный отчёт: каждый день в %02d:00 по Москве → %s", config.REPORT_HOUR, config.OWNER_ID)
    sent_for = None   # за какой день уже отправили — защита от двойной отправки
    while True:
        try:
            # +1 секунда: часы asyncio и системные могут чуть расходиться,
            # и без запаса цикл мог бы проснуться за мгновение до 09:00
            await asyncio.sleep(_seconds_until(config.REPORT_HOUR) + 1)
            yesterday = msk_today() - timedelta(days=1)
            if sent_for == yesterday:
                continue
            await bot.send_message(config.OWNER_ID, await build_report(db, "yday"), reply_markup=report_kb("yday"))
            sent_for = yesterday
        except asyncio.CancelledError:
            raise   # бота останавливают — выходим
        except Exception:
            log.exception("Ежедневный отчёт не отправлен")
            await asyncio.sleep(60)   # не крутимся в цикле ошибок без паузы
