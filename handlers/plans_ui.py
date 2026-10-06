"""
Тарифы, оплата звёздами Telegram и «пригласи друга».

Как устроена оплата (всё делает сам Telegram, карта и платёжный провайдер не нужны):
  * «Месяц» — подписка звёздами: Telegram списывает звёзды раз в 30 дней,
    пока человек не отменит её в настройках Telegram. Бот получает
    сообщение об оплате каждый месяц и продлевает доступ.
  * «3 месяца» — разовый счёт со скидкой 15% (если цена укладывается в лимит
    Telegram — до 10 000 ⭐).
Сама обработка платежа — в handlers/payments.py.
"""

import logging
import uuid

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice

import config
import plans
from db import Database
from handlers.common import (
    back_home, btn, esc, human_date, is_admin, kb, now, plural, safe_answer, show, support_url,
    trial_days_text, url_btn, user_plan,
)
from handlers.trial import left_text

log = logging.getLogger(__name__)
router = Router(name="plans")

# Ссылки на подписку кэшируем, чтобы не создавать новую при каждом нажатии
_sub_links: dict[tuple[int, str], str] = {}


def approx_rub(stars_amount: int) -> str:
    return plans.rub(stars_amount * config.STAR_RUB_BUY)


def compare_table() -> str:
    """Таблица сравнения тарифов моноширинным шрифтом (в Telegram выглядит ровно)."""
    names = [p.title for p in plans.PAID_PLANS]
    short = {
        "Брендов (своих)": "Бренды (свои)", "Проверка каталога": "Проверка", "⚡ Находка сразу": "Сразу",
        "Первым среди всех": "Первым", "Все фото вещи": "Все фото", "🤖 ИИ-помощник продавца": "ИИ-помощник",
        "Легит-чеков": "Легит-чеки", "Сравнений с Авито": "Авито", "Лента хранит": "Лента",
    }
    rows = [("", names)] + [(short.get(r["label"], r["label"]), [v.replace(" мин", "м").replace(" дн.", "д")
                                                              .replace("/мес", "") for v in r["values"]])
                           for r in plans.comparison_rows()]
    w0 = max(len(r[0]) for r in rows)
    widths = [max(len(r[1][i]) for r in rows) for i in range(len(names))]
    out = []
    for label, values in rows:
        out.append(label.ljust(w0) + "  " + "  ".join(v.rjust(widths[i]) for i, v in enumerate(values)))
    return "<pre>" + esc("\n".join(out)) + "</pre>"


async def plans_screen(db: Database, user_id: int):
    user = await db.get_user(user_id)
    current = await user_plan(db, user_id)
    active = await db.has_access(user_id)
    # Кто на тесте, ещё не пробовал или тест только что кончился — тому подсвечиваем PRO:
    # один понятный совет вместо выбора из трёх равных вариантов
    recommend = not is_admin(user_id) and current.code == "trial"
    lines = ["💎 <b>Тарифы</b>", ""]
    if is_admin(user_id):
        lines.append("Ты админ — у тебя всё без ограничений.\n")
    elif active and current.code == "trial":
        lines.append(f"🎁 Тест-драйв: осталось <b>{left_text(user['sub_until'] - now())}</b>. "
                     "Выбери тариф, чтобы радар не выключился — бренды и лента сохранятся.\n")
    elif active:
        lines.append(f"Сейчас: <b>{esc(current.title)}</b> до {human_date(user['sub_until'])}\n")
    for p in plans.PAID_PLANS:
        mark = " ← твой" if active and p.code == current.code else ""
        if recommend and p.code == plans.PRO.code:
            mark = " ← ⭐ советую"
        lines.append(f"<b>{p.title}</b> — {plans.stars(p.price_stars)} в месяц{mark} · <i>{esc(p.tagline)}</i>")
    lines.append("")
    lines.append(compare_table())
    lines.append("")
    lines.append("<b>Чем PRO лучше START:</b> находка приходит сразу, а не сводкой через полчаса; "
                 "проверка вдвое чаще; все фото вещи; лента за неделю.")
    lines.append("<b>Чем ELITE лучше PRO:</b> 🤖 ИИ-помощник ведёт переписку с продавцом — переводит скрины, "
                 "пишет ответ на китайском, подсказывает, как торговаться и какие фото попросить, "
                 "и уточняет легит-чек по каждому новому фото. Плюс находка приходит тебе раньше всех.\n")
    lines.append(f"➕ <b>Свой бренд</b> (которого нет в каталоге) — +1 место за "
                 f"{plans.stars(plans.OWN_ADDON_STARS)} в месяц к любому тарифу. "
                 f"Проверяется каждые {plans.OWN_BRAND_INTERVAL_MIN} мин.\n")
    lines.append("Чем быстрее проверка — тем раньше ты пишешь продавцу. "
                 "На Goofish хорошие вещи по хорошей цене уходят за часы.")
    lines.append(f"\n<i>Оплата звёздами Telegram. 1 ⭐ ≈ {config.STAR_RUB_BUY:g} ₽ "
                 "при покупке звёзд — точная цена зависит от способа покупки.</i>")
    rows = [[btn(f"⭐ {p.title}" if recommend and p.code == plans.PRO.code else p.title, f"pl:p:{p.code}")
             for p in plans.PAID_PLANS]]
    if active and plans.can_buy_own(current) and config.PAYMENTS_ENABLED:
        rows.append([btn(f"➕ Свой бренд — {plans.stars(plans.OWN_ADDON_STARS)}/мес", "pl:own")])
    if user and not user["trial_used"] and not active and config.TRIAL_DAYS:
        rows.append([btn(f"🎁 Включить {trial_days_text()} бесплатно", "trial")])
    rows.append([btn(f"🤝 Пригласи друга — +{config.REFERRAL_BONUS_DAYS} дней", "pl:ref")])
    rows.append(back_home())
    return "\n".join(lines), kb(*rows)


@router.callback_query(F.data == "pl:open")
async def cb_plans(callback: CallbackQuery, db: Database) -> None:
    text, markup = await plans_screen(db, callback.from_user.id)
    await show(callback, text, markup)
    await safe_answer(callback)


async def seats_left(db: Database, plan: plans.Plan, user_id: int) -> int | None:
    """Сколько мест осталось (None — без ограничения). Своё место не считаем занятым."""
    if not plan.seats:
        return None
    taken = await db.count_plan_seats(plan.code)
    if await db.user_plan_code(user_id) == plan.code and await db.has_access(user_id):
        taken -= 1
    return max(0, plan.seats - taken)


@router.callback_query(F.data.startswith("pl:p:"))
async def cb_plan(callback: CallbackQuery, db: Database) -> None:
    plan = plans.ALL_PLANS.get(callback.data.split(":")[2])
    if not plan or plan not in plans.PAID_PLANS:
        await safe_answer(callback)
        return
    user_id = callback.from_user.id
    lines = [
        f"💎 <b>{plan.title}</b> — {plans.stars(plan.price_stars)} в месяц",
        f"<i>≈ {approx_rub(plan.price_stars)} · {esc(plan.tagline)}</i>",
        "",
    ]
    lines += [f"✓ {esc(perk)}" for perk in plan.perks]
    left = await seats_left(db, plan, user_id)
    if left is not None:
        lines.append(f"\n🔥 Свободно мест: <b>{left}</b> из {plan.seats}. "
                     "Мест мало специально: чем меньше людей видят находку первыми — тем она ценнее.")
    quarter = plans.quarter_stars(plan)
    lines.append("\n<b>Как платить</b>")
    lines.append(f"• Месяц — {plans.stars(plan.price_stars)}, продлевается сам, отменить можно в любой момент")
    if quarter:
        lines.append(f"• 3 месяца — {plans.stars(quarter)} разово, скидка 15%")

    rows = []
    if left == 0:
        lines.append("\n😔 Все места заняты. Напиши в поддержку — поставим в лист ожидания.")
    elif config.PAYMENTS_ENABLED:
        rows.append([btn(f"⭐ Месяц — {plans.stars(plan.price_stars)}", f"pl:sub:{plan.code}")])
        if quarter:
            rows.append([btn(f"⭐ 3 месяца — {plans.stars(quarter)}", f"pl:q:{plan.code}")])
    else:
        rows.append([btn("Подключить", f"pl:buy:{plan.code}")])
    rows.append([btn("‹ Все тарифы", "pl:open")])
    await show(callback, "\n".join(lines), kb(*rows))
    await safe_answer(callback)


def _title(plan: plans.Plan, months: int) -> str:
    return f"{config.BRAND_NAME} {plan.title} · {months} мес." if months > 1 else f"{config.BRAND_NAME} {plan.title}"


def _description(plan: plans.Plan) -> str:
    return (f"{plan.brands} брендов, проверка каждые {plan.interval_min} мин, "
            f"{plan.legit_checks} легит-чеков в месяц. Новые объявления с Goofish без подделок.")[:255]


@router.callback_query(F.data.startswith("pl:sub:"))
async def cb_subscribe(callback: CallbackQuery, db: Database) -> None:
    """Подписка на месяц с автопродлением — ссылка на оплату от Telegram."""
    plan = plans.ALL_PLANS.get(callback.data.split(":")[2])
    user_id = callback.from_user.id
    if not plan or plan not in plans.PAID_PLANS or not config.PAYMENTS_ENABLED:
        await safe_answer(callback)
        return
    if await seats_left(db, plan, user_id) == 0:
        await safe_answer(callback, "Все места заняты", alert=True)
        return
    key = (user_id, plan.code)
    link = _sub_links.get(key)
    if not link:
        try:
            link = await callback.bot.create_invoice_link(
                title=_title(plan, 1),
                description=_description(plan),
                payload=f"sub:{plan.code}:{user_id}",
                currency="XTR",
                prices=[LabeledPrice(label=f"{plan.title} на 30 дней", amount=plan.price_stars)],
                subscription_period=plans.SUBSCRIPTION_PERIOD,
            )
        except Exception as e:
            log.warning("Не удалось создать ссылку на подписку: %s", e)
            await safe_answer(callback, "Telegram не дал создать счёт. Попробуй через минуту", alert=True)
            return
        _sub_links[key] = link
    await safe_answer(callback)
    await show(
        callback,
        f"⭐ <b>{plan.title}</b> — {plans.stars(plan.price_stars)} в месяц\n\n"
        "Нажми кнопку — Telegram откроет оплату звёздами. Если звёзд не хватает, "
        "Telegram сам предложит их купить.\n\n"
        "Подписка продлевается каждые 30 дней. Отменить можно в любой момент: "
        "Настройки Telegram → Мои звёзды → подписки. Доступ сохранится до конца оплаченного месяца.",
        kb([url_btn(f"Оплатить {plans.stars(plan.price_stars)}", link)], [btn("‹ Назад", f"pl:p:{plan.code}")]),
    )


@router.callback_query(F.data.startswith("pl:q:"))
async def cb_quarter(callback: CallbackQuery, db: Database) -> None:
    """Разовый счёт на 3 месяца со скидкой."""
    plan = plans.ALL_PLANS.get(callback.data.split(":")[2])
    user_id = callback.from_user.id
    quarter = plans.quarter_stars(plan) if plan else None
    if not plan or not quarter or not config.PAYMENTS_ENABLED:
        await safe_answer(callback)
        return
    if await seats_left(db, plan, user_id) == 0:
        await safe_answer(callback, "Все места заняты", alert=True)
        return
    await safe_answer(callback)
    await callback.message.answer_invoice(
        title=_title(plan, 3),
        description=_description(plan),
        payload=f"once:{plan.code}:3",
        currency="XTR",
        prices=[LabeledPrice(label=f"{plan.title} на 3 месяца", amount=quarter)],
    )


# ---------------------------------------------------------------- «+1 свой бренд»

async def own_addon_link(bot, user_id: int) -> str:
    """
    Ссылка на подписку «+1 свой бренд». У каждой покупки свой slot_id в payload:
    Telegram присылает при продлении тот же payload, так мы узнаём, какой слот продлить.
    """
    slot_id = uuid.uuid4().hex[:16]
    return await bot.create_invoice_link(
        title=f"{config.BRAND_NAME} · +1 свой бренд",
        description=(f"Ещё одно место для своего бренда (не из каталога): проверка каждые "
                     f"{plans.OWN_BRAND_INTERVAL_MIN} мин, находки в ленте и в чате. "
                     "Продлевается раз в 30 дней, отменить можно в любой момент.")[:255],
        payload=f"own:{slot_id}:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label="+1 свой бренд на 30 дней", amount=plans.OWN_ADDON_STARS)],
        subscription_period=plans.SUBSCRIPTION_PERIOD,
    )


@router.callback_query(F.data == "pl:own")
async def cb_own_addon(callback: CallbackQuery, db: Database) -> None:
    user_id = callback.from_user.id
    plan = await user_plan(db, user_id)
    if not config.PAYMENTS_ENABLED:
        await safe_answer(callback, "Оплата сейчас на паузе — напиши в поддержку", alert=True)
        return
    if not await db.has_access(user_id) or plan.code not in {p.code for p in plans.PAID_PLANS}:
        await safe_answer(callback, "Докупить свой бренд можно к платному тарифу", alert=True)
        return
    if not plans.can_buy_own(plan):
        await safe_answer(callback, f"Больше {plans.OWN_ADDON_MAX} своих брендов докупить нельзя", alert=True)
        return
    try:
        link = await own_addon_link(callback.bot, user_id)
    except Exception as e:
        log.warning("Не удалось создать ссылку на «+1 свой бренд»: %s", e)
        await safe_answer(callback, "Telegram не дал создать счёт. Попробуй через минуту", alert=True)
        return
    await safe_answer(callback)
    have = f"Сейчас своих брендов: до {plan.own_brands}"
    have += f" (из них {plan.extra_own} докуплено)." if plan.extra_own else "."
    await show(
        callback,
        f"➕ <b>+1 свой бренд</b> — {plans.stars(plans.OWN_ADDON_STARS)} в месяц\n"
        f"<i>≈ {approx_rub(plans.OWN_ADDON_STARS)}</i>\n\n"
        "Любой бренд, которого нет в каталоге HUNTR: нишевый японский, винтаж, локальная марка. "
        f"Проверяю каждые {plans.OWN_BRAND_INTERVAL_MIN} мин, как и каталог.\n\n"
        f"{have}\n\n"
        "<i>Почему отдельно: бренд из каталога ищут сразу многие — запрос общий и дешёвый. "
        "Свой бренд обычно ищешь только ты, и каждый поиск оплачивается целиком.</i>\n\n"
        "Продлевается каждые 30 дней, отменить можно в любой момент: "
        "Настройки Telegram → Мои звёзды → подписки.",
        kb([url_btn(f"Оплатить {plans.stars(plans.OWN_ADDON_STARS)}", link)],
           [btn("‹ Тарифы", "pl:open"), btn("🎯 Мои бренды", "b:list")]),
    )


@router.callback_query(F.data.startswith("pl:buy:"))
async def cb_plan_buy(callback: CallbackQuery) -> None:
    """Запасной вариант, если PAYMENTS_ENABLED=0: подключение через поддержку."""
    plan = plans.ALL_PLANS.get(callback.data.split(":")[2], plans.PRO)
    user_id = callback.from_user.id
    text = (
        f"Отлично, <b>{plan.title}</b> 🙌\n\n"
        "Оплата в боте сейчас на паузе — подключим вручную. Напиши в поддержку "
        "название тарифа и свой ID:\n\n"
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
        f"Друг приходит по твоей ссылке и получает пробный период. Как только он оплатит "
        f"тариф — тебе +{config.REFERRAL_BONUS_DAYS} {plural(config.REFERRAL_BONUS_DAYS, 'день', 'дня', 'дней')} "
        "к подписке. Без ограничений по количеству.\n\n"
        f"Твоя ссылка:\n<code>{link}</code>\n\n"
        f"Пришли по ссылке: <b>{came}</b> · оплатили: <b>{paid}</b>"
    )
    share = f"https://t.me/share/url?url={link}&text=Бот,%20который%20находит%20брендовые%20вещи%20в%20Китае%20раньше%20всех"
    await show(callback, text, kb([url_btn("📤 Поделиться ссылкой", share)], [btn("‹ Тарифы", "pl:open")]))
    await safe_answer(callback)
