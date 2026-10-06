"""
🤖 ИИ-помощник по переписке с продавцом — фишка тарифа ELITE.

Как пользоваться:
  * открыть: кнопка «🤖 ИИ-помощник» на главной, «💬 Продавцу» под объявлением,
    команда /ai или ссылка t.me/<бот>?start=ai (из приложения HUNTR);
  * дальше просто присылать в чат:
      - скриншот переписки с продавцом (Goofish, WeChat) — переведёт, объяснит,
        что продавец имел в виду, и напишет ответ на китайском;
      - фото вещи от продавца — скажет, стал ли риск подделки меньше или больше,
        и каких фото ещё не хватает;
      - вопрос по-русски («предложи 1500», «как спросить про чек?») — ответит
        и напишет фразу на китайском;
  * кнопки под ответом: другой вариант, поторговаться, какие фото попросить,
    итог по риску, закончить.

Важно:
  * ИИ НЕ пишет продавцу сам: официального доступа к чатам Goofish и WeChat нет.
    Ответ на китайском человек копирует (нажатием) и отправляет сам.
  * Это оценка риска, а не гарантия подлинности.
  * Каждый ответ ИИ — минус одно сообщение из лимита тарифа (ELITE — 300 в месяц).
    Несколько фото одним альбомом — это одно сообщение.

Одна переписка — два окна (бот и приложение HUNTR):
  * История хранится в базе (таблица assistant_msgs), по разговорам («thread»):
    "general" — общий разговор, "goofish:<id>" — разговор про конкретную вещь.
    И бот, и приложение читают и пишут ОДНИ И ТЕ ЖЕ разговоры — поэтому начал
    в боте, продолжил в приложении (и наоборот), и ИИ помнит контекст.
  * В памяти бота (FSM) лежит только «в каком разговоре мы сейчас»:
    {"thread": ..., "about": ...}. Последний разговор ещё и сохраняется
    в настройках пользователя (assistant_thread) — после перезапуска бота
    кнопки под старым ответом продолжают тот же разговор, а не пустой.
  * Общие помощники для бота и webapp/server.py: thread_of / thread_parts
    (имя разговора), history_from_msgs (история для ИИ), msg_preview (превью).
  * Фото в базе НЕ храним — только их количество («📷 2 фото»).
"""

import asyncio
import html
import io
import logging
import re
from urllib.parse import quote, urlsplit, urlunsplit

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message, WebAppInfo

import ai
import brands
import cards
import config
import plans
import rates
import services
from db import Database
from decoder import decode
from handlers.common import back_home, btn, esc, kb, plural, safe_answer, show, upsell_kb, user_plan

log = logging.getLogger(__name__)
router = Router(name="assistant")

MAX_IMAGE_BYTES = 8 * 1024 * 1024   # больше не скачиваем — ИИ всё равно уменьшит
ALBUM_WAIT_SEC = 1.3                # сколько ждать остальные фото альбома


class Assistant(StatesGroup):
    chat = State()


# ----------------------------------------------------------------------
# Тексты
# ----------------------------------------------------------------------

PITCH = (
    "🤖 <b>ИИ-помощник по переписке с продавцом</b> · тариф ELITE\n\n"
    "Кидаешь скрин чата с продавцом из Goofish или WeChat — помощник:\n"
    "• переводит и объясняет, что продавец <b>имел в виду</b> (сленг, намёки, уловки);\n"
    "• пишет готовый <b>ответ на китайском</b> — нажал, скопировал, отправил;\n"
    "• подсказывает, <b>как торговаться</b> и какую цену реально предложить;\n"
    "• говорит, <b>каких фото не хватает</b>: бирки, QR-код, швы, фурнитура;\n"
    "• после каждого нового фото <b>уточняет риск подделки</b>: стало лучше или хуже и почему;\n"
    "• ловит красные флаги: «оплати мне на WeChat», 高仿, 1:1, 原单.\n\n"
    "<b>Пример</b>\n"
    "Продавец: <code>可以小刀，走闲鱼</code>\n"
    "🤖 «Готов немного уступить, сделка через Goofish». Это хороший знак: оплата идёт через гарантию площадки. "
    "Ответ: <code>诚心要，1800包邮可以吗？</code> — «Беру серьёзно, 1800 с доставкой — договоримся?»\n\n"
    "<i>Помощник не пишет продавцу сам, а готовит тебе ответ. Это оценка риска, а не гарантия подлинности.</i>"
)

HELLO = (
    "🤖 <b>ИИ-помощник включён</b>{about}\n\n"
    "Присылай сюда:\n"
    "📱 <b>скрин переписки</b> с продавцом — переведу и напишу ответ на китайском;\n"
    "📸 <b>фото вещи</b> от продавца — уточню риск подделки и скажу, чего не хватает;\n"
    "💬 <b>вопрос</b> по-русски — например, «предложи 1500» или «как спросить про чек?».\n\n"
    "<i>Несколько фото можно отправить одним альбомом — это один запрос. "
    "Осталось сообщений в этом месяце: {left}.</i>"
)

_VERDICT = {
    "likely_real": ("🟢", "похоже на оригинал"),
    "unclear": ("🟡", "пока непонятно"),
    "likely_fake": ("🔴", "похоже на подделку"),
}
_CHANGE = {"better": "↗️ стало лучше", "worse": "↘️ стало хуже", "same": "→ без изменений", "first": ""}


def answer_text(r: dict, about: str | None, left: int) -> str:
    """Ответ помощника для Telegram (HTML)."""
    lines = ["🤖 <b>ИИ-помощник</b>" + (f" · {esc(about)}" if about else "")]
    if r.get("seller_said"):
        lines.append(f"\n📩 <b>Продавец пишет:</b> «{esc(r['seller_said'])}»")
    if r.get("meaning"):
        lines.append(f"💡 {esc(r['meaning'])}")
    if r.get("reply_cn"):
        lines.append("\n✍️ <b>Ответ продавцу</b> — нажми, чтобы скопировать:")
        lines.append(f"<code>{esc(r['reply_cn'])}</code>")
        if r.get("reply_ru"):
            lines.append(f"<i>{esc(r['reply_ru'])}</i>")
    legit = r.get("legit")
    if legit:
        emoji, label = _VERDICT.get(legit["verdict"], _VERDICT["unclear"])
        change = _CHANGE.get(legit.get("change"), "")
        lines.append(f"\n🛡 <b>Риск подделки:</b> {emoji} {label} · {legit['score']}/100"
                     + (f" · {change}" if change else ""))
        if legit.get("why"):
            lines.append(esc(legit["why"]))
    if r.get("red_flags"):
        lines.append("\n⚠️ <b>Красные флаги</b>")
        lines += [f"• {esc(x)}" for x in r["red_flags"]]
    if r.get("ask_photos"):
        lines.append("\n📸 <b>Попроси фото</b>")
        lines += [f"• {esc(x)}" for x in r["ask_photos"]]
    if r.get("tips"):
        lines.append("\n💬 <b>Советы</b>")
        lines += [f"• {esc(x)}" for x in r["tips"]]
    footer = f"Осталось сообщений: {left}." if left < 1000 else ""
    if legit:
        footer = "Это оценка риска по фото, а не гарантия подлинности. " + footer
    if footer:
        lines.append(f"\n<i>{footer.strip()}</i>")
    return "\n".join(lines)[:4000]


def answer_kb():
    return kb(
        [btn("🔁 Другой вариант", "as:i:alt"), btn("💰 Поторговаться", "as:i:bargain")],
        [btn("📸 Какие фото попросить", "as:i:photos"), btn("🛡 Итог по риску", "as:i:verdict")],
        [btn("✖ Закончить", "as:x")],
    )


def history_entry(r: dict) -> str:
    """Короткая запись ответа помощника — чтобы он помнил разговор, но не тратил лишние токены."""
    bits = []
    if r.get("seller_said"):
        bits.append(f"Продавец: {r['seller_said']}")
    if r.get("reply_cn"):
        bits.append(f"Предложенный ответ: {r['reply_cn']} ({r.get('reply_ru', '')})")
    if r.get("legit"):
        bits.append(f"Оценка риска: {r['legit']['verdict']} {r['legit']['score']}/100 — {r['legit'].get('why', '')}")
    if r.get("ask_photos"):
        bits.append("Просили фото: " + "; ".join(r["ask_photos"]))
    return "\n".join(bits) or r.get("meaning", "")


# ----------------------------------------------------------------------
# Общее для бота и приложения: разговоры (thread) и история из базы.
# webapp/server.py импортирует эти функции — так бот и приложение гарантированно
# называют разговоры одинаково и одинаково пересказывают историю ИИ.
# ----------------------------------------------------------------------

GENERAL = "general"
# Что разрешаем в имени разговора: защищает базу и ссылки от мусора
_SOURCE_RE = re.compile(r"[a-z0-9_]{1,20}")
_ITEM_RE = re.compile(r"[0-9A-Za-z_-]{1,40}")

# Как кнопки-просьбы выглядят в истории (и для ИИ, и в приложении)
INTENT_LABELS = {"alt": "Другой вариант ответа", "bargain": "Помоги поторговаться",
                 "photos": "Какие фото попросить?", "verdict": "Итог по риску"}


def thread_of(source: str | None, item_id: str | None) -> str:
    """
    Имя разговора: "goofish:123" — про конкретную вещь, "general" — общий.
    Если площадка или id странные — ValueError (приложение ответит 400, бот откроет общий).
    """
    if source and item_id:
        if not _SOURCE_RE.fullmatch(source) or not _ITEM_RE.fullmatch(item_id):
            raise ValueError("bad_item")
        return f"{source}:{item_id}"
    return GENERAL


def thread_parts(thread: str | None) -> tuple[str | None, str | None]:
    """Обратно: "goofish:123" -> ("goofish", "123"); общий или битый разговор -> (None, None)."""
    if not thread or thread == GENERAL or ":" not in thread:
        return None, None
    source, item_id = thread.split(":", 1)
    try:
        thread_of(source, item_id)
    except ValueError:
        return None, None
    return source, item_id


def history_from_msgs(msgs: list[dict]) -> list[dict]:
    """
    Сообщения из базы -> короткая история для ИИ (как в чате: user / assistant).
    Одна и та же функция в боте и приложении — иначе ИИ «помнил» бы разговор по-разному.
    """
    history = []
    for m in msgs:
        if m.get("role") == "user":
            bits = [INTENT_LABELS.get(m.get("intent") or "", ""), m.get("text") or "",
                    f"[прислал фото: {m['photos']}]" if m.get("photos") else ""]
            history.append({"role": "user", "content": " ".join(b for b in bits if b) or "(сообщение)"})
        elif isinstance(m.get("reply"), dict):
            history.append({"role": "assistant", "content": history_entry(m["reply"])})
    return history


def _short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def msg_preview(m: dict, n: int = 80) -> str:
    """Одна строка про сообщение — для списка разговоров в приложении и пересказа в боте."""
    if m.get("role") == "user":
        if m.get("text"):
            return _short(m["text"], n)
        if m.get("intent"):
            return INTENT_LABELS.get(m["intent"], "")
        return f"📷 {m['photos']} фото" if m.get("photos") else "(сообщение)"
    r = m.get("reply") if isinstance(m.get("reply"), dict) else {}
    return _short(r.get("reply_cn") or r.get("seller_said") or r.get("meaning") or "Ответ помощника", n)


def _about(keyword: str, data: dict) -> str:
    """Короткое название вещи для заголовка: «Бренд — что это»."""
    brand = brands.display_name(keyword)
    what = data.get("title_ru") or decode(data.get("title") or "").summary() or ""
    return f"{brand} — {what}"[:60] if what else brand


async def thread_about(db: Database, thread: str) -> str | None:
    """Название разговора без тяжёлого контекста для ИИ (для списка «Мои разговоры»)."""
    source, item_id = thread_parts(thread)
    if not source:
        return None
    found = await db.get_listing(source, item_id)
    return _about(*found) if found else None


# ----------------------------------------------------------------------
# Контекст сделки (если помощника открыли из объявления)
# ----------------------------------------------------------------------

async def listing_context(db: Database, source: str, item_id: str) -> tuple[str, str] | None:
    """(короткое название для заголовка, контекст для ИИ) или None, если объявления нет."""
    found = await db.get_listing(source, item_id)
    if not found:
        return None
    keyword, data = found
    brand = brands.display_name(keyword)
    d = decode(data.get("title") or "")
    about = _about(keyword, data)
    lines = [f"Бренд: {brand}", f"Объявление на Goofish: {data.get('title', '')[:300]}"]
    if data.get("title_ru"):
        lines.append(f"Перевод заголовка: {data['title_ru']}")
    price = data.get("price")
    if price is not None:
        lines.append(f"Цена продавца: ¥{price:.0f} (≈ {price * rates.cny_rub():.0f} ₽)")
    detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
    if detail.get("description"):
        lines.append("Описание продавца: " + detail["description"][:600])
    seller = services.seller_lines(detail)
    if seller:
        lines.append("Продавец: " + "; ".join(seller))
    market = await db.get_avito(f"{brand} {d.category or ''}".strip().lower(), config.AVITO_CACHE_HOURS * 3600)
    if market and market.get("median"):
        lines.append(f"Похожие вещи на Авито в России: медиана {market['median']:.0f} ₽, "
                     f"обычно {market.get('p25', 0):.0f}–{market.get('p75', 0):.0f} ₽")
    legit = data.get("legit")
    if isinstance(legit, dict):
        lines.append(f"Прошлый легит-чек по фото объявления: {legit.get('verdict')} {legit.get('score')}/100; "
                     f"смущает: {'; '.join(legit.get('bad') or []) or 'ничего'}; "
                     f"просили фото: {'; '.join(legit.get('ask') or []) or '—'}")
    if data.get("status"):
        lines.append("Внимание: похоже, вещь уже продана или снята с продажи.")
    return about, "\n".join(lines)


# ----------------------------------------------------------------------
# Вход в помощника
# ----------------------------------------------------------------------

async def open_assistant(target: Message | CallbackQuery, db: Database, state: FSMContext,
                         user_id: int, source: str | None = None, item_id: str | None = None) -> None:
    plan = await user_plan(db, user_id)
    if not plan.assistant or not (await db.has_access(user_id) or user_id in config.ADMIN_IDS):
        markup = upsell_kb(plans.next_plan_with("assistant"))
        if isinstance(target, CallbackQuery):
            await show(target, PITCH, markup)
        else:
            await target.answer(PITCH, reply_markup=markup)
        return
    if not ai.enabled():
        text = ("🤖 Помощник выключен: не задан <code>AI_API_KEY</code> в Variables на Railway."
                if user_id in config.ADMIN_IDS else "🤖 Помощник скоро заработает — подключаем ИИ. Загляни чуть позже 🙏")
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text)
        return
    try:
        thread = thread_of(source, item_id)
    except ValueError:
        thread = GENERAL   # битая ссылка — не падаем, открываем общий разговор
    await enter_thread(state, db, user_id, thread)
    data = await state.get_data()
    msg = target.message if isinstance(target, CallbackQuery) else target
    text, markup = await _welcome(db, user_id, thread, data.get("about"))
    await msg.answer(text, reply_markup=markup)


async def enter_thread(state: FSMContext, db: Database, user_id: int, thread: str) -> None:
    """
    Включаем режим помощника в нужном разговоре. В FSM — только имя разговора и
    заголовок; сама история живёт в базе. Имя разговора запоминаем и в настройках,
    чтобы после перезапуска бота продолжить именно его.
    """
    about = await thread_about(db, thread)
    await state.set_state(Assistant.chat)
    await state.set_data({"thread": thread, "about": about})
    await db.update_settings(user_id, assistant_thread=thread)


def _app_thread_url(thread: str) -> str:
    """Ссылка на приложение, которая сразу откроет этот разговор: .../app?ai=goofish%3A123."""
    parts = urlsplit(config.WEBAPP_URL)
    query = (parts.query + "&" if parts.query else "") + "ai=" + quote(thread, safe="")
    return urlunsplit(parts._replace(query=query))


async def _welcome(db: Database, user_id: int, thread: str, about: str | None):
    """Приветствие: пустой разговор — подсказка; уже есть сообщения — «Продолжаем разговор»."""
    left = await services.quota_left(db, user_id, "assistant")
    msgs = await db.list_assistant_msgs(user_id, thread, limit=4)
    if not msgs:
        text = HELLO.format(about=f" · {esc(about)}" if about else "", left=left)
        return text, kb([btn("✖ Закончить", "as:x")])
    count = await db.count_assistant_msgs(user_id, thread)
    lines = ["🤖 <b>ИИ-помощник</b>" + (f" · {esc(about)}" if about else ""),
             f"\n🔄 <b>Продолжаем разговор</b> — в нём {count} {plural(count, 'сообщение', 'сообщения', 'сообщений')}. "
             "Он общий для бота и приложения HUNTR."]
    # Последние 1–2 обмена: начинаем с вопроса пользователя, чтобы пары не разрывались
    while msgs and msgs[0].get("role") != "user":
        msgs = msgs[1:]
    for m in msgs:
        via = " <i>(из приложения)</i>" if m.get("via", "app") == "app" else ""
        if m.get("role") == "user":
            lines.append(f"\n👤 <b>Ты:</b> {esc(msg_preview(m, 70))}{via}")
            continue
        r = m.get("reply") if isinstance(m.get("reply"), dict) else {}
        if r.get("seller_said"):
            lines.append(f"📩 Продавец: «{esc(_short(r['seller_said'], 70))}»")
        if r.get("reply_cn"):
            lines.append(f"✍️ Ответ: <code>{esc(_short(r['reply_cn'], 70))}</code>")
        if not r.get("seller_said") and not r.get("reply_cn"):
            lines.append(f"🤖 {esc(msg_preview(m, 70))}")
    lines.append("\nПрисылай скрин, фото или вопрос — продолжу с того же места.")
    lines.append(f"<i>Осталось сообщений в этом месяце: {left}.</i>")
    rows = []
    if config.WEBAPP_URL:
        rows.append([InlineKeyboardButton(text="📱 Вся переписка в приложении",
                                          web_app=WebAppInfo(url=_app_thread_url(thread)))])
    rows.append([btn("🧹 Начать заново", "as:clr"), btn("✖ Закончить", "as:x")])
    return "\n".join(lines)[:4000], kb(*rows)


@router.callback_query(F.data == "as:open")
async def cb_open(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await safe_answer(callback)
    await open_assistant(callback, db, state, callback.from_user.id)


@router.callback_query(F.data.startswith("as:l:"))
async def cb_open_listing(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    """«💬 Продавцу» под объявлением: ELITE — помощник с контекстом вещи, остальным — фразы и подсказка."""
    _, _, source, item_id = callback.data.split(":", 3)
    plan = await user_plan(db, callback.from_user.id)
    await safe_answer(callback)
    if not plan.assistant:
        await callback.message.reply(
            cards.phrases_text()
            + "\n\n🤖 <b>В ELITE</b> вместо готовых фраз — ИИ-помощник: кидаешь скрин переписки, "
              "он переводит, пишет ответ на китайском, помогает торговаться и уточняет риск подделки по фото.",
            reply_markup=kb([btn("🤖 Что умеет помощник", "as:open")]),
        )
        return
    await open_assistant(callback, db, state, callback.from_user.id, source, item_id)


@router.message(Command("ai"))
async def cmd_ai(message: Message, db: Database, state: FSMContext) -> None:
    await open_assistant(message, db, state, message.from_user.id)


@router.callback_query(F.data == "as:x")
async def cb_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(callback, "Помощник выключен")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("🤖 Помощник выключен. Включить снова — /ai или кнопка на главной.",
                                  reply_markup=kb(back_home()))


async def _current_thread(state: FSMContext, db: Database, user_id: int) -> str:
    """В каком разговоре мы сейчас: из FSM, а если бот перезапускался — из настроек."""
    if await state.get_state() == Assistant.chat.state:
        thread = (await state.get_data()).get("thread")
        if thread:
            return thread
    thread = (await db.get_settings(user_id)).get("assistant_thread") or GENERAL
    # Проверяем: в настройки могло попасть что угодно — открываем только корректный разговор
    return thread if thread == GENERAL or thread_parts(thread)[0] else GENERAL


@router.callback_query(F.data == "as:clr")
async def cb_clear(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    """«🧹 Начать заново»: стираем историю этого разговора (в боте и в приложении сразу)."""
    user_id = callback.from_user.id
    plan = await user_plan(db, user_id)
    if not plan.assistant:
        await safe_answer(callback)
        await show(callback, PITCH, upsell_kb(plans.next_plan_with("assistant")))
        return
    thread = await _current_thread(state, db, user_id)
    await db.clear_assistant(user_id, thread)
    await enter_thread(state, db, user_id, thread)
    await safe_answer(callback, "Начали заново")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    text, markup = await _welcome(db, user_id, thread, (await state.get_data()).get("about"))
    await callback.message.answer(text, reply_markup=markup)


# ----------------------------------------------------------------------
# Разговор
# ----------------------------------------------------------------------

async def _download(bot: Bot, file_id: str, size: int | None) -> bytes | None:
    if size and size > MAX_IMAGE_BYTES:
        return None
    try:
        buf = await bot.download(file_id, destination=io.BytesIO())
        return buf.getvalue() if buf else None
    except Exception as e:
        log.warning("Помощник: не скачал фото: %s", e)
        return None


def _image_ref(message: Message) -> tuple[str, int | None] | None:
    if message.photo:
        biggest = message.photo[-1]
        return biggest.file_id, biggest.file_size
    doc = message.document
    if doc and (doc.mime_type or "").startswith("image/"):
        return doc.file_id, doc.file_size
    return None


async def reply_step(message: Message, db: Database, state: FSMContext, user_id: int,
                     text: str | None = None, images: list[bytes] | None = None,
                     intent: str | None = None) -> None:
    """Один шаг: проверяем лимит, спрашиваем ИИ, показываем ответ, запоминаем разговор."""
    plan = await user_plan(db, user_id)
    if not plan.assistant:
        await state.clear()
        await message.answer(PITCH, reply_markup=upsell_kb(plans.next_plan_with("assistant")))
        return
    if await services.quota_left(db, user_id, "assistant") <= 0:
        await message.answer("🤖 Сообщения помощнику в этом месяце закончились. "
                             "Лимит обновится 1-го числа. Если нужно больше — напиши в поддержку.")
        return
    data = await state.get_data()
    thread = data.get("thread") or GENERAL
    about = data.get("about")
    wait = await message.answer("🤖 Читаю и думаю… обычно 10–30 секунд.")
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    # Контекст вещи и историю каждый раз берём из базы: там же их видит и дополняет приложение
    context = ""
    source, item_id = thread_parts(thread)
    if source:
        found = await listing_context(db, source, item_id)
        if found:
            about, context = about or found[0], found[1]
    history = history_from_msgs(await db.list_assistant_msgs(user_id, thread, limit=10))
    r = await ai.assistant(context, history, text=text, images=images, intent=intent)
    if not r:
        # ИИ не ответил — лимит не тратим и в историю ничего не пишем
        await wait.edit_text("Не получилось — ИИ не ответил. Попробуй ещё раз через минуту, лимит не потрачен.")
        return
    await db.add_usage(user_id, "assistant")
    # Сохраняем обмен в общую историю. Фото не храним — только сколько их было.
    # "via": "bot" — приложение покажет у таких сообщений пометку «из бота».
    await db.add_assistant_msg(user_id, thread, "user",
                               {"text": text, "photos": len(images or []),
                                "intent": intent if intent in INTENT_LABELS else None, "via": "bot"})
    await db.add_assistant_msg(user_id, thread, "assistant", {"reply": r, "via": "bot"})
    left = await services.quota_left(db, user_id, "assistant")
    try:
        await wait.edit_text(answer_text(r, about, left), reply_markup=answer_kb())
    except Exception:
        await message.answer(answer_text(r, about, left), reply_markup=answer_kb())


@router.callback_query(F.data.startswith("as:i:"))
async def cb_intent(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    intent = callback.data.split(":", 2)[2]
    if await state.get_state() != Assistant.chat.state:
        # Бот перезапускался или помощника закрыли — открываем ПОСЛЕДНИЙ разговор
        # (он записан в настройках), а не пустой, и выполняем просьбу
        plan = await user_plan(db, callback.from_user.id)
        if not plan.assistant:
            await safe_answer(callback)
            await show(callback, PITCH, upsell_kb(plans.next_plan_with("assistant")))
            return
        thread = await _current_thread(state, db, callback.from_user.id)
        await enter_thread(state, db, callback.from_user.id, thread)
    await safe_answer(callback, "Думаю…")
    await reply_step(callback.message, db, state, callback.from_user.id, intent=intent)


# Альбомы: Telegram присылает каждое фото альбома отдельным сообщением с общим media_group_id
_albums: dict[tuple[int, str], list[Message]] = {}


async def _process_album(key: tuple[int, str], db: Database, state: FSMContext) -> None:
    await asyncio.sleep(ALBUM_WAIT_SEC)
    messages = sorted(_albums.pop(key, []), key=lambda m: m.message_id)
    if not messages:
        return
    first = messages[0]
    images, caption = [], None
    for m in messages[:6]:
        ref = _image_ref(m)
        if ref:
            raw = await _download(first.bot, *ref)
            if raw:
                images.append(raw)
        caption = caption or m.caption
    if not images:
        await first.answer("Не получилось открыть фото — пришли их ещё раз (до 8 МБ каждое).")
        return
    if len(messages) > 6:
        await first.answer("Беру первые 6 фото — больше за раз ИИ не смотрит.")
    await reply_step(first, db, state, first.from_user.id, text=caption, images=images)


@router.message(StateFilter(Assistant.chat), F.photo | F.document)
async def msg_image(message: Message, db: Database, state: FSMContext) -> None:
    ref = _image_ref(message)
    if not ref:
        await message.answer("Пришли картинку (скрин или фото) — файлы другого типа помощник не читает.")
        return
    if message.media_group_id:
        key = (message.chat.id, message.media_group_id)
        first = key not in _albums
        _albums.setdefault(key, []).append(message)
        if first:
            asyncio.create_task(_process_album(key, db, state))
        return
    raw = await _download(message.bot, *ref)
    if not raw:
        await message.answer("Не получилось открыть фото — пришли его ещё раз (до 8 МБ).")
        return
    await reply_step(message, db, state, message.from_user.id, text=message.caption, images=[raw])


@router.message(StateFilter(Assistant.chat), F.text, ~F.text.startswith("/"))
async def msg_text(message: Message, db: Database, state: FSMContext) -> None:
    await reply_step(message, db, state, message.from_user.id, text=message.text)


@router.message(StateFilter(Assistant.chat), ~F.text)
async def msg_other(message: Message) -> None:
    await message.answer("Помощник понимает текст, скрины и фото. Голосовые и видео пока не читает 🙏")
