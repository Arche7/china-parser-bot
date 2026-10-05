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

Состояние разговора хранится в памяти бота (FSM). После перезапуска бота
разговор начинается заново — это нормально.
"""

import asyncio
import html
import io
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import ai
import brands
import cards
import config
import plans
import rates
import services
from db import Database
from decoder import decode
from handlers.common import back_home, btn, esc, kb, safe_answer, show, upsell_kb, user_plan

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
    what = data.get("title_ru") or d.summary() or ""
    about = f"{brand} — {what}"[:60] if what else brand
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
    about, context = None, ""
    if source and item_id:
        found = await listing_context(db, source, item_id)
        if found:
            about, context = found
    await state.set_state(Assistant.chat)
    await state.set_data({"about": about, "context": context, "history": [], "last": None})
    left = await services.quota_left(db, user_id, "assistant")
    text = HELLO.format(about=f" · {esc(about)}" if about else "", left=left)
    markup = kb([btn("✖ Закончить", "as:x")])
    msg = target.message if isinstance(target, CallbackQuery) else target
    await msg.answer(text, reply_markup=markup)


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
    wait = await message.answer("🤖 Читаю и думаю… обычно 10–30 секунд.")
    try:
        await message.bot.send_chat_action(message.chat.id, "typing")
    except Exception:
        pass
    r = await ai.assistant(data.get("context") or "", data.get("history") or [],
                           text=text, images=images, intent=intent)
    if not r:
        await wait.edit_text("Не получилось — ИИ не ответил. Попробуй ещё раз через минуту, лимит не потрачен.")
        return
    await db.add_usage(user_id, "assistant")
    left = await services.quota_left(db, user_id, "assistant")
    history = list(data.get("history") or [])
    asked = {"alt": "(попросил другой вариант)", "bargain": "(попросил помочь поторговаться)",
             "photos": "(спросил, какие фото попросить)", "verdict": "(попросил итог по риску)"}.get(intent or "", "")
    user_turn = " ".join(x for x in [asked, text or "", f"[прислал фото: {len(images)}]" if images else ""] if x)
    history += [{"role": "user", "content": user_turn or "(сообщение)"},
                {"role": "assistant", "content": history_entry(r)}]
    await state.update_data(history=history[-10:], last=user_turn)
    try:
        await wait.edit_text(answer_text(r, data.get("about"), left), reply_markup=answer_kb())
    except Exception:
        await message.answer(answer_text(r, data.get("about"), left), reply_markup=answer_kb())


@router.callback_query(F.data.startswith("as:i:"))
async def cb_intent(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    intent = callback.data.split(":", 2)[2]
    if await state.get_state() != Assistant.chat.state:
        # Бот перезапускался или помощника закрыли — откроем заново и выполним просьбу
        plan = await user_plan(db, callback.from_user.id)
        if not plan.assistant:
            await safe_answer(callback)
            await show(callback, PITCH, upsell_kb(plans.next_plan_with("assistant")))
            return
        await state.set_state(Assistant.chat)
        await state.set_data({"about": None, "context": "", "history": [], "last": None})
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
