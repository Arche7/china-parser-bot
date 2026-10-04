"""
Как выглядят сообщения с объявлениями.

Идея карточки: за 2 секунды понять, стоит ли открывать объявление.
Поэтому сверху — бренд и что за вещь, дальше цена в ¥ и ₽, размер и
состояние, город и время. Китайский оригинал заголовка спрятан в
сворачиваемую цитату — он не мешает, но всегда под рукой.

Под карточкой — кнопки: открыть объявление, легит-чек, цена и выгода,
избранное и готовые фразы для продавца на китайском.
"""

import html
import re
import time
from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import brands
import config
import rates
from decoder import city_ru, decode, has_chinese, strip_noise


def money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ")


def price_line(price: float | None, currency: str = "CNY") -> str:
    if price is None:
        return "цена не указана"
    if currency.upper() in ("CNY", "RMB", "¥"):
        rub = price * rates.cny_rub()
        return f"<b>¥{money(price)}</b> · ≈ {money(rub)} ₽"
    return f"<b>{money(price)} {html.escape(currency)}</b>"


_REL_CN = [(r"(\d+)\s*分钟前", 60), (r"(\d+)\s*小时前", 3600), (r"(\d+)\s*天前", 86400)]


def ago(posted_at) -> str | None:
    """Разные форматы времени с площадки -> '12 мин назад'."""
    if posted_at in (None, ""):
        return None
    seconds = None
    if isinstance(posted_at, (int, float)) or (isinstance(posted_at, str) and posted_at.isdigit()):
        ts = float(posted_at)
        if ts > 1e12:  # миллисекунды
            ts /= 1000
        seconds = time.time() - ts
    elif isinstance(posted_at, str):
        text = posted_at.strip()
        if "刚刚" in text:
            return "только что"
        for pattern, mult in _REL_CN:
            m = re.search(pattern, text)
            if m:
                seconds = int(m.group(1)) * mult
                break
        if seconds is None:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
                seconds = time.time() - dt.timestamp()
            except ValueError:
                return None if has_chinese(text) else html.escape(text)
    if seconds is None or seconds < 0:
        return None
    if seconds < 90:
        return "только что"
    if seconds < 3600:
        return f"{int(seconds // 60)} мин назад"
    if seconds < 86400:
        return f"{int(seconds // 3600)} ч назад"
    return f"{int(seconds // 86400)} дн назад"


def build_card(data: dict, keyword: str, settings: dict, header: str = "🆕") -> str:
    """
    data — объявление в виде словаря (см. Listing + поле title_ru).
    settings — настройки пользователя (вид карточки, перевод).
    """
    title = data.get("title") or ""
    d = decode(title)
    brand = brands.display_name(keyword)
    what = d.category or ""

    lines = [f"{header} <b>{html.escape(brand)}</b>" + (f" — {html.escape(what)}" if what else "")]

    title_ru = data.get("title_ru")
    if settings.get("title", "ru") == "ru" and title_ru:
        lines.append(f"<i>{html.escape(title_ru)}</i>")
    elif settings.get("title") == "orig" or not has_chinese(title):
        lines.append(html.escape(strip_noise(title)[:200]))

    lines.append("")
    lines.append(f"💴 {price_line(data.get('price'), data.get('currency', 'CNY'))}")

    specs = [x for x in (d.condition, d.color) if x]
    if d.size:
        lines.append("📐 " + " · ".join(html.escape(x) for x in [f"размер {d.size}", *specs]))
    elif specs:
        lines.append("✨ " + " · ".join(html.escape(x) for x in specs))

    place = [x for x in (city_ru(data.get("city")), ago(data.get("posted_at"))) if x]
    if place:
        lines.append("📍 " + " · ".join(html.escape(str(x)) for x in place))

    notes = list(d.notes)
    extra = data.get("extra") or {}
    if extra.get("free_shipping") and "доставка по Китаю бесплатно" not in notes:
        notes.append("доставка по Китаю бесплатно")
    if notes:
        lines.append("🏷 " + html.escape(", ".join(notes[:4])))

    if settings.get("card") == "full":
        if data.get("seller"):
            lines.append(f"👤 продавец: {html.escape(str(data['seller']))}")
        wants = extra.get("wants")
        if wants:
            lines.append(f"🔥 хотят купить: {html.escape(str(wants))}")

    # Оригинальный заголовок — в свёрнутой цитате, чтобы не мешал
    if has_chinese(title) and settings.get("title", "ru") == "ru":
        lines.append(f"<blockquote expandable>{html.escape(strip_noise(title)[:500])}</blockquote>")

    return "\n".join(lines)[:1020]  # лимит подписи к фото — 1024 символа


def card_keyboard(source: str, item_id: str, url: str, is_fav: bool = False) -> InlineKeyboardMarkup:
    ref = f"{source}:{item_id}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть на Goofish ↗", url=url)],
        [
            InlineKeyboardButton(text="🛡 Легит-чек", callback_data=f"l:lg:{ref}"),
            InlineKeyboardButton(text="📊 Выгода", callback_data=f"l:pr:{ref}"),
        ],
        [
            InlineKeyboardButton(text="★ В избранном" if is_fav else "☆ В избранное", callback_data=f"l:fv:{ref}"),
            InlineKeyboardButton(text="💬 Фразы продавцу", callback_data=f"l:ph:{ref}"),
        ],
    ])


# ----------------------------------------------------------------------
# Калькулятор выгоды
# ----------------------------------------------------------------------

def profit_text(data: dict, keyword: str, settings: dict, market: dict | None) -> str:
    d = decode(data.get("title") or "")
    brand = brands.display_name(keyword)
    price = data.get("price")
    lines = [f"📊 <b>{html.escape(brand)}</b>" + (f" — {html.escape(d.category)}" if d.category else "")]
    if price is None:
        lines.append("\nУ объявления нет цены — посчитать не получится.")
        return "\n".join(lines)

    rate = rates.cny_rub()
    base = price * rate
    fee = base * float(settings.get("fee", config.BUYER_FEE_PCT)) / 100
    delivery = d.weight_kg * float(settings.get("delivery", config.DELIVERY_RUB_PER_KG))
    total = base + fee + delivery

    lines += [
        "",
        f"Вещь: ¥{money(price)} → {money(base)} ₽ <i>({rates.source_label()} {rate:.2f})</i>",
        f"Байер {settings.get('fee', config.BUYER_FEE_PCT):g}%: +{money(fee)} ₽",
        f"Доставка ~{d.weight_kg:g} кг × {money(float(settings.get('delivery')))} ₽: +{money(delivery)} ₽",
        f"<b>Себестоимость в России: ~{money(total)} ₽</b>",
    ]

    if market and market.get("median"):
        median = market["median"]
        profit = median - total
        pct = profit / total * 100 if total else 0
        sign = "+" if profit >= 0 else "−"
        verdict = "🟢" if pct >= 40 else ("🟡" if pct >= 10 else "🔴")
        lines += [
            "",
            f"На Авито {market['count']} похожих · обычно {money(market['p25'])}–{money(market['p75'])} ₽",
            f"медиана <b>{money(median)} ₽</b> · быстро продать ≈ {money(market['p25'])} ₽",
            f"{verdict} <b>Потенциал: {sign}{money(abs(profit))} ₽ ({sign}{abs(pct):.0f}%)</b>",
        ]
        samples = market.get("samples") or []
        if samples:
            lines.append("")
            for smp in samples[:3]:
                lines.append(f"• <a href=\"{html.escape(smp['url'])}\">{html.escape(smp['title'][:40])}</a>"
                             f" — {money(smp['price'])} ₽")
        lines.append("\n<i>Сверь модель, размер и состояние — цены Авито по похожим, а не по этой вещи.</i>")
    elif market is not None:
        lines += [
            "",
            "Цены в России по этой вещи посчитать не вышло — открой поиск на Авито "
            "по кнопке ниже и сравни сам.",
        ]
    return "\n".join(lines)


SELLER_PHRASES: list[tuple[str, str]] = [
    ("你好，还在吗？", "Здравствуйте, ещё продаёте?"),
    ("可以发一下吊牌和水洗标的照片吗？", "Пришлите, пожалуйста, фото бирки и тега с составом"),
    ("可以拍一下细节吗？logo、拉链、走线和五金", "Можно детальные фото: логотип, молния, строчка, фурнитура?"),
    ("是正品吗？有购买凭证吗？", "Это оригинал? Есть чек или подтверждение покупки?"),
    ("能量一下尺寸吗？衣长、胸围、肩宽、袖长", "Можете замерить: длина, грудь, плечи, рукав?"),
    ("最低多少可以出？诚心要", "Какая минимальная цена? Беру серьёзно"),
    ("可以寄到我的转运仓吗？我发你地址", "Отправите на мой склад? Скину адрес"),
]


def phrases_text() -> str:
    lines = ["💬 <b>Фразы для продавца</b>", "Нажми на китайский текст — он скопируется.\n"]
    for cn, ru in SELLER_PHRASES:
        lines.append(f"<code>{cn}</code>\n<i>{ru}</i>\n")
    lines.append("На Goofish пиши продавцу прямо в объявлении — кнопка «聊一聊» (поболтать).")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Легит-чек
# ----------------------------------------------------------------------

_VERDICT = {
    "likely_real": ("🟢", "Похоже на оригинал"),
    "unclear": ("🟡", "Нужно больше фото"),
    "likely_fake": ("🔴", "Похоже на подделку"),
}


def legit_text(brand: str, result: dict, left: int | None, seller: list[str] | None = None,
               photos: int | None = None) -> str:
    emoji, label = _VERDICT.get(result.get("verdict"), _VERDICT["unclear"])
    score = result.get("score", 50)
    bar = "▰" * round(score / 10) + "▱" * (10 - round(score / 10))
    lines = [f"🛡 <b>Легит-чек · {html.escape(brand)}</b>"]
    if result.get("item"):
        lines.append(f"<i>{html.escape(result['item'])}</i>")
    risk = {"low": "🟢 риск низкий", "medium": "🟡 риск средний", "high": "🔴 риск высокий"}.get(result.get("risk"))
    lines += ["", f"{emoji} <b>{label}</b>" + (f" · {risk}" if risk else ""), f"{bar} {score}/100"]
    if result.get("good"):
        lines.append("\n<b>Выглядит как у оригинала</b>")
        lines += [f"• {html.escape(x)}" for x in result["good"]]
    if result.get("bad"):
        lines.append("\n<b>Что смущает</b>")
        lines += [f"• {html.escape(x)}" for x in result["bad"]]
    if result.get("ask"):
        lines.append("\n<b>Что запросить у продавца</b>")
        lines += [f"• {html.escape(x)}" for x in result["ask"]]
    if seller:
        lines.append("\n<b>Продавец</b>")
        lines += [f"• {html.escape(x)}" for x in seller]
    if photos:
        lines.append(f"\n<i>Проверено фото из объявления: {photos}.</i>")
    lines.append(
        "<i>Это оценка ИИ по фото, а не гарантия подлинности. Для дорогих вещей "
        "закажи профессиональный легит-чек.</i>"
    )
    if left is not None and left < 1000:
        lines.append(f"<i>Осталось легит-чеков в этом месяце: {left}</i>")
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Сводка и лента
# ----------------------------------------------------------------------

import hashlib


def brand_code(keyword: str) -> str:
    """Короткий код бренда для кнопок (в callback_data помещается до 64 байт)."""
    return hashlib.md5(keyword.encode()).hexdigest()[:8]


def _top(counter: dict, n: int) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: -kv[1])[:n]


def _word(total: int) -> str:
    if total % 10 == 1 and total % 100 != 11:
        return "новая находка"
    if 2 <= total % 10 <= 4 and not 12 <= total % 100 <= 14:
        return "новые находки"
    return "новых находок"


def digest_caption(pending: list, every_min: int | None, cheapest: dict | None = None) -> str:
    """
    🆕 12 новых находок · за 30 мин
    Louis Vuitton 5 · Gucci 4 · ещё 3
    Сумки 6 · Одежда 4 · Обувь 2
    💰 самая дешёвая — ¥680 ≈ 8 430 ₽ (Stone Island)
    """
    from collections import Counter
    total = len(pending)
    by_brand = Counter(r["keyword"] for r in pending)
    by_group = Counter(r["grp"] for r in pending)
    top = _top(by_brand, 4)
    brands_line = " · ".join(f"{html.escape(brands.display_name(k))} {n}" for k, n in top)
    rest = total - sum(n for _, n in top)
    if rest > 0:
        brands_line += f" · ещё {rest}"
    groups_line = " · ".join(f"{g} {n}" for g, n in _top(by_group, 4))
    head = f"🆕 <b>{total} {_word(total)}</b>"
    if every_min:
        head += f" · за {every_min} мин" if every_min < 60 else f" · за {every_min // 60} ч"
    lines = [head, "", brands_line, groups_line]
    if cheapest and cheapest.get("price") is not None:
        rub = cheapest["price"] * rates.cny_rub()
        lines.append(f"💰 самая дешёвая — ¥{money(cheapest['price'])} ≈ {money(rub)} ₽ "
                     f"({html.escape(brands.display_name(cheapest['keyword']))})")
    lines += ["", "<i>Листай прямо здесь — все сразу или по брендам и разделам.</i>"]
    return "\n".join(lines)


def digest_keyboard(pending: list) -> InlineKeyboardMarkup:
    from collections import Counter
    from handlers.common import webapp_button  # здесь, чтобы не было циклического импорта
    by_brand = Counter(r["keyword"] for r in pending)
    by_group = Counter(r["grp"] for r in pending)
    rows = [[InlineKeyboardButton(text=f"▶️ Смотреть все · {len(pending)}", callback_data="fd:v:all:-:0")]]
    chips = [InlineKeyboardButton(text=f"{brands.display_name(k)[:18]} · {n}", callback_data=f"fd:v:b:{brand_code(k)}:0")
             for k, n in _top(by_brand, 4)]
    if len(chips) > 1:
        rows += [chips[i:i + 2] for i in range(0, len(chips), 2)]
    groups = [InlineKeyboardButton(text=f"{g} · {n}", callback_data=f"fd:v:g:{g}:0") for g, n in _top(by_group, 3)]
    if len(groups) > 1:
        rows.append(groups)
    last = [InlineKeyboardButton(text="🔔 Как часто", callback_data="nt:open")]
    app = webapp_button("В приложении")
    if app:
        last.insert(0, app)
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def viewer_keyboard(item: dict, ftype: str, fval: str, idx: int, total: int) -> InlineKeyboardMarkup:
    ref = f"{item['source']}:{item['item_id']}"
    base = f"fd:v:{ftype}:{fval}:"
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton(text="‹ Назад", callback_data=f"{base}{idx - 1}"))
    nav.append(InlineKeyboardButton(text=f"{idx + 1} из {total}", callback_data=f"fd:m:{ftype}:{fval}:0"))
    if idx < total - 1:
        nav.append(InlineKeyboardButton(text="Дальше ›", callback_data=f"{base}{idx + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="Открыть на Goofish ↗", url=item["data"]["url"])],
        [InlineKeyboardButton(text="🛡 Легит-чек", callback_data=f"l:lg:{ref}"),
         InlineKeyboardButton(text="📊 Выгода и Авито", callback_data=f"l:pr:{ref}")],
        [InlineKeyboardButton(text="★ В избранном" if item["fav"] else "☆ В избранное",
                              callback_data=f"fd:f:{ftype}:{fval}:{idx}"),
         InlineKeyboardButton(text="🙈 Не интересно", callback_data=f"fd:h:{ftype}:{fval}:{idx}")],
        [InlineKeyboardButton(text="🗂 Фильтр", callback_data=f"fd:m:{ftype}:{fval}:0"),
         InlineKeyboardButton(text="‹ Главная", callback_data="h:home")],
    ])
