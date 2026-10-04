"""
Бренды: каталог, добавление в 2 нажатия, бюджет, карточка бренда,
пауза и удаление, «Готовый набор».

Сценарий добавления:
  «➕ Добавить» → кнопка бренда из каталога (или «✍️ Свой бренд») →
  кнопка бюджета (или «✍️ Свой диапазон») → готово, показываю что есть сейчас.
"""

import html
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import brands
import plans
from brands_cn import chinese_name
from db import Database
from handlers.common import (
    BTN_BRANDS, back_home, brand_limits_text, btn, count_own, esc, kb, price_text,
    run_background, safe_answer, show, url_btn, user_plan,
)
from monitor import Monitor, watch_interval

router = Router(name="brands")

PAGE = 10  # брендов каталога на одной странице


class AddFlow(StatesGroup):
    own_name = State()      # ждём название своего бренда
    own_price = State()     # ждём свой диапазон цены (при добавлении)
    edit_price = State()    # ждём свой диапазон цены (при изменении)


PRICE_RE = re.compile(r"^¥?\s*(\d+)?\s*[-–—]\s*¥?\s*(\d+)?$")


def parse_price(text: str) -> tuple[float | None, float | None] | None:
    """'500-3000' -> (500, 3000); '-2000' -> (None, 2000); '1000-' -> (1000, None)."""
    text = text.replace(" ", "").replace("¥", "")
    m = PRICE_RE.match(text)
    if not m or not (m.group(1) or m.group(2)):
        if text.isdigit():
            return None, float(text)  # «2000» = «до 2000»
        return None
    low = float(m.group(1)) if m.group(1) else None
    high = float(m.group(2)) if m.group(2) else None
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high


def parse_brand_input(text: str) -> tuple[str, float | None, float | None]:
    """'stone island 500-3000' -> ('stone island', 500.0, 3000.0)"""
    parts = text.strip().split()
    price_min = price_max = None
    if parts:
        parsed = parse_price(parts[-1]) if re.search(r"\d", parts[-1]) and "-" in parts[-1] else None
        if parsed:
            price_min, price_max = parsed
            parts = parts[:-1]
    return " ".join(parts).strip().lower(), price_min, price_max


async def merge_old_variants(db: Database, user_id: int, key: str) -> list[str]:
    """Если бренд уже есть в другом написании (古驰 и gucci) — оставляем одну запись."""
    removed = []
    for watch in await db.list_watches(user_id):
        if watch["keyword"] != key and brands.canonical(watch["keyword"]) == key:
            await db.delete_watch(user_id, watch["id"])
            removed.append(watch["keyword"])
    return removed


async def limit_problem(db: Database, user_id: int, keyword: str) -> str | None:
    """Почему нельзя добавить бренд (лимит тарифа) или None, если можно."""
    plan = await user_plan(db, user_id)
    watches = await db.list_watches(user_id)
    if any(brands.canonical(w["keyword"]) == keyword for w in watches):
        return None  # уже есть — просто обновим цену
    if len(watches) >= plan.brands:
        return (f"В тарифе <b>{esc(plan.title)}</b> — до {plan.brands} брендов, и все места заняты. "
                "Убери ненужный или перейди на тариф побольше.")
    if not brands.is_catalog(keyword):
        own = await count_own(db, user_id)
        if plan.own_brands == 0:
            return (f"В тарифе <b>{esc(plan.title)}</b> доступны бренды из каталога. "
                    "Свои бренды — с тарифа PRO.")
        if own >= plan.own_brands:
            return (f"Своих брендов в тарифе <b>{esc(plan.title)}</b> — до {plan.own_brands}. "
                    "Можно взять бренд из каталога или перейти на тариф выше.")
    return None


def limit_keyboard():
    return kb([btn("💎 Тарифы", "pl:open"), btn("🎯 Мои бренды", "b:list")], back_home())


# ---------------------------------------------------------------- каталог

async def catalog_screen(db: Database, user_id: int, page: int):
    plan = await user_plan(db, user_id)
    watches = await db.list_watches(user_id)
    mine = {brands.canonical(w["keyword"]) for w in watches}
    keys = list(brands.BRANDS)
    pages = max(1, (len(keys) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = keys[page * PAGE:(page + 1) * PAGE]

    rows = [[btn(f"⭐ Готовый набор · {len(brands.PRESET)} брендов", "b:preset")]] if page == 0 else []
    for i in range(0, len(chunk), 2):
        row = []
        for key in chunk[i:i + 2]:
            mark = "✓ " if key in mine else ""
            row.append(btn(f"{mark}{brands.BRANDS[key]['title']}", f"b:pick:{key}"))
        rows.append(row)
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("‹", f"b:add:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("›", f"b:add:{page + 1}"))
        rows.append(nav)
    rows.append([btn("✍️ Свой бренд", "b:own")])
    rows.append([btn("‹ Пульт", "h:home"), btn("🎯 Мои бренды", "b:list")])

    own = await count_own(db, user_id)
    text = (
        "➕ <b>Какой бренд ищем?</b>\n\n"
        "Для брендов из каталога я знаю все написания — латиницей, по-китайски и сленгом "
        "(например, LV = 路易威登 = 驴牌), так находится больше объявлений.\n\n"
        f"<i>Занято: {brand_limits_text(plan, len(watches), own)}</i>"
    )
    return text, kb(*rows)


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject, db: Database, monitor: Monitor, state: FSMContext) -> None:
    await state.clear()
    if command.args:
        keyword, low, high = parse_brand_input(command.args)
        if len(keyword) < 2:
            await message.answer("Название слишком короткое. Пример: <code>/add stone island 500-3000</code>")
            return
        await finish_add(message, db, monitor, message.from_user.id, brands.canonical(keyword), low, high)
        return
    text, markup = await catalog_screen(db, message.from_user.id, 0)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("b:add:"))
async def cb_catalog(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    page = int(callback.data.split(":")[2] or 0)
    text, markup = await catalog_screen(db, callback.from_user.id, page)
    await show(callback, text, markup)
    await safe_answer(callback)


# ---------------------------------------------------------------- выбор бюджета

def price_keyboard(prefix: str, back: str):
    rows = []
    presets = brands.PRICE_PRESETS
    for i in range(0, len(presets), 2):
        rows.append([btn(label, f"{prefix}:{i + j}") for j, (label, _, _) in enumerate(presets[i:i + 2])])
    rows.append([btn("✍️ Свой диапазон", f"{prefix}:c")])
    rows.append([btn("‹ Назад", back)])
    return kb(*rows)


def price_hint() -> str:
    return ("Цена в юанях — так ищет сама площадка. В рублях покажу рядом.\n"
            "<i>Подсказка: для люкса б/у обычно хватает ¥500–5 000, для стритвира — ¥200–1 800.</i>")


async def ask_price(event, db: Database, user_id: int, keyword: str, state: FSMContext) -> None:
    problem = await limit_problem(db, user_id, keyword)
    if problem:
        await show(event, "🔒 " + problem, limit_keyboard())
        return
    await state.update_data(pending=keyword)
    name = esc(brands.display_name(keyword))
    await show(event, f"💰 <b>{name}</b> — какой бюджет?\n\n{price_hint()}", price_keyboard("b:pp", "b:add:0"))


@router.callback_query(F.data.startswith("b:pick:"))
async def cb_pick(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    key = callback.data.split(":", 2)[2]
    if key not in brands.BRANDS:
        await safe_answer(callback, "Не нашёл такой бренд", alert=True)
        return
    await ask_price(callback, db, callback.from_user.id, key, state)
    await safe_answer(callback)


@router.callback_query(F.data == "b:own")
async def cb_own(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    plan = await user_plan(db, callback.from_user.id)
    if plan.own_brands == 0:
        await show(callback, f"🔒 В тарифе <b>{esc(plan.title)}</b> — только бренды из каталога. "
                             "Свои бренды открываются с PRO.", limit_keyboard())
        await safe_answer(callback)
        return
    await state.set_state(AddFlow.own_name)
    await show(callback,
               "✍️ <b>Напиши название бренда</b> — латиницей или по-китайски.\n\n"
               "Например: <code>rick owens</code>, <code>kapital</code>, <code>始祖鸟</code>",
               kb([btn("‹ Назад", "b:add:0")]))
    await safe_answer(callback)


@router.message(StateFilter(AddFlow.own_name), F.text)
async def got_own_name(message: Message, db: Database, state: FSMContext) -> None:
    keyword = message.text.strip().lower()
    if keyword.startswith("/"):
        await state.clear()
        return
    if not 2 <= len(keyword) <= 60:
        await message.answer("Нужно от 2 до 60 символов. Попробуй ещё раз 🙂")
        return
    await state.set_state(None)
    await ask_price(message, db, message.from_user.id, brands.canonical(keyword), state)


@router.callback_query(F.data.startswith("b:pp:"))
async def cb_price_preset(callback: CallbackQuery, db: Database, monitor: Monitor, state: FSMContext) -> None:
    data = await state.get_data()
    keyword = data.get("pending")
    if not keyword:
        await safe_answer(callback, "Начни заново — «➕ Добавить»", alert=True)
        return
    choice = callback.data.split(":")[2]
    if choice == "c":
        await state.set_state(AddFlow.own_price)
        await show(callback,
                   f"✍️ Напиши диапазон в юанях для <b>{esc(brands.display_name(keyword))}</b>:\n\n"
                   "<code>500-3000</code> — от 500 до 3000 ¥\n<code>-2000</code> — до 2000 ¥\n"
                   "<code>1000-</code> — от 1000 ¥",
                   kb([btn("‹ Назад", f"b:pick:{keyword}" if keyword in brands.BRANDS else "b:add:0")]))
        await safe_answer(callback)
        return
    _, low, high = brands.PRICE_PRESETS[int(choice)]
    await state.clear()
    await safe_answer(callback)
    await finish_add(callback, db, monitor, callback.from_user.id, keyword, low, high)


@router.message(StateFilter(AddFlow.own_price), F.text)
async def got_own_price(message: Message, db: Database, monitor: Monitor, state: FSMContext) -> None:
    parsed = parse_price(message.text)
    if parsed is None:
        await message.answer("Не понял диапазон 🤔 Пример: <code>500-3000</code>")
        return
    keyword = (await state.get_data()).get("pending")
    await state.clear()
    if keyword:
        await finish_add(message, db, monitor, message.from_user.id, keyword, *parsed)


async def finish_add(event, db: Database, monitor: Monitor, user_id: int, keyword: str,
                     low: float | None, high: float | None) -> None:
    problem = await limit_problem(db, user_id, keyword)
    if problem:
        await show(event, "🔒 " + problem, limit_keyboard())
        return
    removed = await merge_old_variants(db, user_id, keyword) if brands.is_catalog(keyword) else []
    is_new = await db.add_watch(user_id, keyword, low, high)
    plan = await user_plan(db, user_id)
    name = esc(brands.display_name(keyword))
    every = watch_interval({"is_admin": plan.code == "admin", "plan": plan.code, "keyword": keyword})

    lines = [f"✅ <b>{name}</b> на радаре" if is_new else f"✏️ <b>{name}</b> — бюджет обновлён",
             f"Бюджет: {price_text(low, high, with_rub=True)}",
             f"Проверка: каждые {every} мин"]
    if brands.is_catalog(keyword):
        lines.append(f"<i>Ищу: {esc(', '.join(brands.search_queries(keyword)))} + все написания</i>")
    if removed:
        lines.append("♻️ Объединил со старыми записями: " + esc(", ".join(removed)))
    lines.append("\nСмотрю, что есть прямо сейчас — пришлю через минуту-другую…")
    await show(event, "\n".join(lines),
               kb([btn("➕ Ещё бренд", "b:add:0"), btn("🎯 Мои бренды", "b:list")], back_home()))
    run_background(monitor.preview_new_keyword(user_id, keyword, show=True))

    # Для своих брендов подскажем китайское название
    cn = chinese_name(keyword) if not brands.is_catalog(keyword) else None
    if is_new and cn and cn != keyword and not await db.get_watch_by_keyword(user_id, cn):
        watch = await db.get_watch_by_keyword(user_id, keyword)
        msg = event.message if isinstance(event, CallbackQuery) else event
        await msg.answer(
            f"🇨🇳 На Goofish <b>{name}</b> часто пишут по-китайски: <b>{cn}</b>. "
            "Искать и так? Найдётся больше объявлений.\n\n"
            "<i>Займёт ещё одно место среди своих брендов.</i>",
            reply_markup=kb([btn(f"✅ Да, добавить {cn}", f"b:cn:{watch['id']}"), btn("Не надо", "b:cnno")]),
        )


@router.callback_query(F.data.startswith("b:cn:"))
async def cb_add_chinese(callback: CallbackQuery, db: Database, monitor: Monitor) -> None:
    user_id = callback.from_user.id
    watch = await db.get_watch(user_id, int(callback.data.split(":")[2]))
    cn = chinese_name(watch["keyword"]) if watch else None
    if not cn:
        await safe_answer(callback, "Исходный бренд уже удалён", alert=True)
        return
    problem = await limit_problem(db, user_id, cn)
    if problem:
        await safe_answer(callback, re.sub("<[^>]+>", "", problem), alert=True)
        return
    await db.add_watch(user_id, cn, watch["price_min"], watch["price_max"])
    run_background(monitor.preview_new_keyword(user_id, cn, show=True))
    await safe_answer(callback, f"Добавил {cn}")
    await callback.message.edit_text(f"✅ Ищу и по-китайски: <b>{cn}</b>.")


@router.callback_query(F.data == "b:cnno")
async def cb_skip_chinese(callback: CallbackQuery) -> None:
    await safe_answer(callback, "Ок")
    try:
        await callback.message.delete()
    except Exception:
        pass


# ---------------------------------------------------------------- готовый набор

@router.callback_query(F.data == "b:preset")
async def cb_preset(callback: CallbackQuery, db: Database) -> None:
    plan = await user_plan(db, callback.from_user.id)
    names = ", ".join(brands.BRANDS[k]["title"] for k in brands.PRESET)
    fit = min(len(brands.PRESET), plan.brands)
    note = "" if fit >= len(brands.PRESET) else (
        f"\n\n<i>В тарифе {esc(plan.title)} помещается {fit} — добавлю первые {fit}, "
        "остальные откроются на тарифе выше.</i>")
    text = (f"⭐ <b>Готовый набор</b>\n\n{esc(names)}\n\n"
            f"Бюджет: {price_text(float(brands.PRESET_PRICE_MIN), float(brands.PRESET_PRICE_MAX), True)}"
            f" — потом можно поменять у каждого бренда.{note}")
    await show(callback, text, kb([btn("✅ Добавить набор", "b:presetok")], [btn("‹ Назад", "b:add:0")]))
    await safe_answer(callback)


@router.message(Command("preset"))
async def cmd_preset(message: Message, db: Database, monitor: Monitor) -> None:
    await apply_preset(message, db, monitor, message.from_user.id)


@router.callback_query(F.data == "b:presetok")
async def cb_preset_ok(callback: CallbackQuery, db: Database, monitor: Monitor) -> None:
    await safe_answer(callback, "Добавляю…")
    await apply_preset(callback, db, monitor, callback.from_user.id)


async def apply_preset(event, db: Database, monitor: Monitor, user_id: int) -> None:
    low, high = float(brands.PRESET_PRICE_MIN), float(brands.PRESET_PRICE_MAX)
    added, skipped = [], []
    for key in brands.PRESET:
        if await limit_problem(db, user_id, key):
            skipped.append(brands.display_name(key))
            continue
        await merge_old_variants(db, user_id, key)
        await db.add_watch(user_id, key, low, high)
        added.append(brands.display_name(key))
        # Молча запоминаем текущие объявления — присылать будем только новые
        run_background(monitor.preview_new_keyword(user_id, key, show=False))
    lines = ["⭐ <b>Готово!</b>", ""]
    lines += [f"✅ {esc(n)}" for n in added]
    if skipped:
        lines.append(f"\n🔒 Не поместились по тарифу: {esc(', '.join(skipped))}")
    lines.append(f"\nБюджет {price_text(low, high)}. Сейчас тихо запоминаю, что уже выложено, "
                 "чтобы не засыпать тебя старьём, — дальше пришлю только новое.")
    await show(event, "\n".join(lines), kb([btn("🎯 Мои бренды", "b:list")], back_home()))


# ---------------------------------------------------------------- мои бренды

async def list_screen(db: Database, user_id: int):
    plan = await user_plan(db, user_id)
    watches = await db.list_watches(user_id)
    if not watches:
        return ("🎯 <b>Мои бренды</b>\n\nПока пусто. Начни с готового набора — "
                f"{len(brands.PRESET)} самых ходовых брендов в один клик — или выбери свои.",
                kb([btn("⭐ Готовый набор", "b:preset")], [btn("➕ Выбрать бренды", "b:add:0")], back_home()))
    own = await count_own(db, user_id)
    lines = [f"🎯 <b>Мои бренды</b> · {brand_limits_text(plan, len(watches), own)}", ""]
    rows = []
    for w in watches:
        name = brands.display_name(w["keyword"])
        status = "⏸ " if w["paused"] else ""
        tag = "" if brands.is_catalog(w["keyword"]) else " · свой"
        lines.append(f"{status}<b>{esc(name)}</b> — {price_text(w['price_min'], w['price_max'])}"
                     f"{tag} · находок: {w['found']}")
        rows.append(btn(f"{status}{name}"[:28], f"b:w:{w['id']}"))
    lines.append("\n<i>Нажми на бренд, чтобы поменять бюджет, поставить на паузу или удалить.</i>")
    grid = [rows[i:i + 2] for i in range(0, len(rows), 2)]
    grid.append([btn("➕ Добавить", "b:add:0")])
    grid.append(back_home())
    return "\n".join(lines), kb(*grid)


@router.message(Command("list"))
@router.message(F.text == BTN_BRANDS)
async def cmd_list(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, markup = await list_screen(db, message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "b:list")
async def cb_list(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    text, markup = await list_screen(db, callback.from_user.id)
    await show(callback, text, markup)
    await safe_answer(callback)


async def watch_screen(db: Database, user_id: int, watch_id: int):
    w = await db.get_watch(user_id, watch_id)
    if not w:
        return None
    plan = await user_plan(db, user_id)
    name = esc(brands.display_name(w["keyword"]))
    every = watch_interval({"is_admin": plan.code == "admin", "plan": plan.code, "keyword": w["keyword"]})
    goofish = f"https://www.goofish.com/search?q={html.escape(brands.search_queries(w['keyword'])[0])}"
    lines = [
        f"<b>{name}</b>" + (" · ⏸ на паузе" if w["paused"] else ""),
        "",
        f"💰 Бюджет: {price_text(w['price_min'], w['price_max'], with_rub=True)}",
        f"⏱ Проверка: каждые {every} мин" + ("" if brands.is_catalog(w["keyword"]) else " (свой бренд)"),
        f"📬 Прислал объявлений: {w['found']}",
    ]
    if brands.is_catalog(w["keyword"]):
        lines.append(f"🔎 Ищу: {esc(', '.join(brands.search_queries(w['keyword'])))}")
    markup = kb(
        [btn("💰 Бюджет", f"b:wp:{w['id']}"),
         btn("▶️ Включить" if w["paused"] else "⏸ Пауза", f"b:wz:{w['id']}")],
        [url_btn("Поиск на Goofish ↗", goofish), btn("🗑 Удалить", f"b:del:{w['id']}")],
        [btn("‹ Мои бренды", "b:list")],
    )
    return "\n".join(lines), markup


@router.callback_query(F.data.startswith("b:w:"))
async def cb_watch(callback: CallbackQuery, db: Database) -> None:
    screen = await watch_screen(db, callback.from_user.id, int(callback.data.split(":")[2]))
    if not screen:
        await safe_answer(callback, "Этого бренда уже нет")
        return
    await show(callback, *screen)
    await safe_answer(callback)


@router.callback_query(F.data.startswith("b:wz:"))
async def cb_watch_pause(callback: CallbackQuery, db: Database) -> None:
    watch_id = int(callback.data.split(":")[2])
    w = await db.get_watch(callback.from_user.id, watch_id)
    if not w:
        await safe_answer(callback)
        return
    await db.set_watch_paused(callback.from_user.id, watch_id, not w["paused"])
    await safe_answer(callback, "Включил" if w["paused"] else "Бренд на паузе")
    screen = await watch_screen(db, callback.from_user.id, watch_id)
    await show(callback, *screen)


@router.callback_query(F.data.startswith("b:del:"))
async def cb_delete_ask(callback: CallbackQuery, db: Database) -> None:
    watch_id = int(callback.data.split(":")[2])
    w = await db.get_watch(callback.from_user.id, watch_id)
    if not w:
        await safe_answer(callback)
        return
    await show(callback, f"Удалить <b>{esc(brands.display_name(w['keyword']))}</b> с радара?",
               kb([btn("🗑 Да, удалить", f"b:delok:{watch_id}"), btn("Оставить", f"b:w:{watch_id}")]))
    await safe_answer(callback)


@router.callback_query(F.data.startswith("b:delok:"))
async def cb_delete(callback: CallbackQuery, db: Database) -> None:
    keyword = await db.delete_watch(callback.from_user.id, int(callback.data.split(":")[2]))
    await safe_answer(callback, f"Удалил {brands.display_name(keyword)}" if keyword else "Уже удалено")
    text, markup = await list_screen(db, callback.from_user.id)
    await show(callback, text, markup)


@router.callback_query(F.data.startswith("b:wp:"))
async def cb_watch_price(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    watch_id = int(callback.data.split(":")[2])
    w = await db.get_watch(callback.from_user.id, watch_id)
    if not w:
        await safe_answer(callback)
        return
    await state.update_data(edit_watch=watch_id)
    await show(callback, f"💰 Новый бюджет для <b>{esc(brands.display_name(w['keyword']))}</b>\n"
                         f"Сейчас: {price_text(w['price_min'], w['price_max'], True)}\n\n{price_hint()}",
               price_keyboard("b:wpp", f"b:w:{watch_id}"))
    await safe_answer(callback)


@router.callback_query(F.data.startswith("b:wpp:"))
async def cb_watch_price_set(callback: CallbackQuery, db: Database, monitor: Monitor, state: FSMContext) -> None:
    watch_id = (await state.get_data()).get("edit_watch")
    choice = callback.data.split(":")[2]
    if not watch_id:
        await safe_answer(callback, "Открой бренд ещё раз", alert=True)
        return
    if choice == "c":
        await state.set_state(AddFlow.edit_price)
        await show(callback, "✍️ Напиши диапазон в юанях: <code>500-3000</code>, <code>-2000</code> или <code>1000-</code>",
                   kb([btn("‹ Назад", f"b:w:{watch_id}")]))
        await safe_answer(callback)
        return
    _, low, high = brands.PRICE_PRESETS[int(choice)]
    await apply_new_price(callback, db, monitor, callback.from_user.id, watch_id, low, high)
    await state.clear()
    await safe_answer(callback, "Бюджет обновлён")


@router.message(StateFilter(AddFlow.edit_price), F.text)
async def got_edit_price(message: Message, db: Database, monitor: Monitor, state: FSMContext) -> None:
    parsed = parse_price(message.text)
    if parsed is None:
        await message.answer("Не понял диапазон 🤔 Пример: <code>500-3000</code>")
        return
    watch_id = (await state.get_data()).get("edit_watch")
    await state.clear()
    if watch_id:
        await apply_new_price(message, db, monitor, message.from_user.id, watch_id, *parsed)


async def apply_new_price(event, db, monitor, user_id, watch_id, low, high) -> None:
    w = await db.get_watch(user_id, watch_id)
    if not w:
        return
    await db.update_watch_price(user_id, watch_id, low, high)
    # Общий диапазон цен мог поменяться — молча запомним текущие объявления
    run_background(monitor.preview_new_keyword(user_id, brands.canonical(w["keyword"]), show=False))
    screen = await watch_screen(db, user_id, watch_id)
    await show(event, *screen)


# ---------------------------------------------------------------- старая команда /del

@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject, db: Database) -> None:
    if not command.args:
        text, markup = await list_screen(db, message.from_user.id)
        await message.answer(text, reply_markup=markup)
        return
    keyword, _, _ = parse_brand_input(command.args)
    user_id = message.from_user.id
    deleted = await db.delete_watch_by_keyword(user_id, keyword) or \
        await db.delete_watch_by_keyword(user_id, brands.canonical(keyword))
    await message.answer(f"🗑 Удалил <b>{esc(brands.display_name(keyword))}</b>." if deleted
                         else "Такого бренда нет в списке — загляни в /list.")
