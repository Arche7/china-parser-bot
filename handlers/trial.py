"""
Тест-драйв (пробный период): включение, отсчёт и напоминания.

Почему так устроено:
  * Тест должен ПРОДАВАТЬ, а не быть щедрым. Поэтому он короткий (config.TRIAL_DAYS),
    с лимитами plans.TRIAL, а мы стараемся, чтобы человек за это время
    увидел пользу и вовремя узнал, что тест кончается.
  * Включить тест можно тремя путями — все идут через start_trial() ниже:
      1) кнопка «🎁 Включить … бесплатно» (callback "trial", handlers/start.py);
      2) кнопка в приложении (POST /api/trial, webapp/server.py);
      3) АВТОМАТИЧЕСКИ — когда человек без доступа сохраняет первый бренд
         (handlers/brands_ui.finish_add и POST /api/brands). Это autostart_trial().
  * Напоминания шлёт фоновый цикл trial_nudges_loop() (запускается в bot.py):
      day1 — через сутки: «радар нашёл N вещей»;
      6h   — за 6 часов до конца: что пропадёт + кнопки тарифов;
      end  — в конце: итог + предложение (или мягкий текст, если брендов не было).
    Каждое — максимум один раз: что уже отправлено, хранится в настройках
    пользователя (users.settings → "trial_nudges"), схему базы не трогаем.
    Ночью (если включены тихие часы) и на паузе напоминание ОТКЛАДЫВАЕМ, а не пропускаем.
"""

import asyncio
import logging
import time
from datetime import datetime

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

import brands
import config
import plans
import rates
import texts
from db import Database
from handlers.common import (
    MONTHS, btn, esc, is_admin, kb, plural, support_url, trial_eligible, url_btn, webapp_button,
)
from monitor import TZ, is_quiet_now

log = logging.getLogger(__name__)

CHECK_EVERY_SEC = 600        # как часто цикл проверяет тестовых пользователей (~10 мин)
DAY1_AFTER = 24 * 3600       # «через сутки»
BEFORE_END = 6 * 3600        # «за 6 часов до конца»
MIN_LEFT_FOR_6H = 15 * 60    # если до конца меньше 15 минут — «6 часов» уже не шлём, ждём «конец»
END_WINDOW = 12 * 3600       # «тест закончился» шлём только в первые 12 ч после конца:
                             # иначе после обновления бота написали бы всем, у кого тест был давно


# ---------------------------------------------------------------- время

def _now() -> int:
    return int(time.time())


def until_text(ts: int) -> str:
    """1759924800 -> '8 октября, 14:30' (по Москве — как и тихие часы)."""
    dt = datetime.fromtimestamp(ts, TZ) if TZ else datetime.fromtimestamp(ts)
    return f"{dt.day} {MONTHS[dt.month - 1]}, {dt:%H:%M}"


def left_text(seconds: int) -> str:
    """Сколько осталось: '1 д 5 ч', '5 ч 20 мин', '20 мин'."""
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} д {hours} ч" if hours else f"{days} д"
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    return f"{max(minutes, 1)} мин"


def trial_start_ts(settings: dict, until: int) -> int:
    """
    Когда начался тест. Новые тесты запоминают это сами (settings["trial_start"]).
    У тех, кто включил тест до этого обновления, отметки нет — тогда считаем
    от конца: тест длится ровно TRIAL_DAYS, значит начало = конец − TRIAL_DAYS.
    """
    start = settings.get("trial_start")
    if start:
        return int(start)
    return int(until) - config.TRIAL_DAYS * 86400


# ---------------------------------------------------------------- включение

async def start_trial(db: Database, user_id: int) -> int | None:
    """
    Включает тест-драйв. Возвращает время окончания или None, если нельзя
    (тест выключен, уже был или уже есть доступ — это проверяет db.start_trial).
    Тест включается ОДИН раз: db.start_trial ставит trial_used = 1.
    """
    if not config.TRIAL_DAYS:
        return None
    until = await db.start_trial(user_id, config.TRIAL_DAYS)
    if until is None:
        return None
    # Запоминаем начало и обнуляем отметки напоминаний
    await db.update_settings(user_id, trial_start=_now(), trial_nudges=[])
    log.info("Тест-драйв включён: %s до %s", user_id, until)
    return until


async def autostart_trial(db: Database, user_id: int) -> int | None:
    """
    Человек без доступа сохраняет первый бренд — включаем тест сами, вместо замка.
    Вызывать ТОЛЬКО после проверки лимитов (limit_problem): лимиты для такого
    человека уже считаются по plans.TRIAL (см. handlers/common.user_plan).
    """
    if not await trial_eligible(db, user_id):
        return None
    return await start_trial(db, user_id)


def started_message(until: int, brand: str | None = None):
    """Сообщение «тест включён» с таймлайном: что будет дальше. brand — если тест включился сам."""
    limit = plans.TRIAL.brands
    steps = [texts.TRIAL_NOW_AUTO.format(brands=esc(brand)) if brand
             else texts.TRIAL_NOW_PICK.format(limit=limit)]
    # «через 24 ч» обещаем, только если тест длиннее суток — иначе отчёт совпал бы с концом
    if config.TRIAL_DAYS >= 2:
        steps.append(texts.TRIAL_STEP_DAY1)
    steps.append(texts.TRIAL_STEP_6H)
    text = texts.TRIAL_STARTED.format(until=until_text(until), timeline="\n".join(steps), limit=limit)
    first = "➕ Добавить второй бренд" if brand else "➕ Выбрать бренды"
    rows = [[btn(first, "b:add:0")]]
    if webapp_button():
        rows.append([webapp_button()])
    return text, kb(*rows)


# ---------------------------------------------------------------- данные для напоминаний

def _brand_list(watches) -> str:
    names = [brands.display_name(w["keyword"]) for w in watches]
    if len(names) > 3:
        return esc(", ".join(names[:3])) + f" и ещё {len(names) - 3}"
    return esc(", ".join(names))


def _items(n: int) -> str:
    return f"{n} {plural(n, 'вещь', 'вещи', 'вещей')}"


async def _best_line(db: Database, user_id: int, since: int) -> str:
    """Самая дешёвая находка за тест — конкретный пример цепляет сильнее, чем цифра."""
    cheapest = None
    for row in await db.feed_page(user_id, since=since, limit=60):
        price = row["data"].get("price")
        if price is None:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        if price > 0 and (cheapest is None or price < cheapest[0]):
            cheapest = (price, row["keyword"])
    if not cheapest:
        return ""
    price, keyword = cheapest
    money = f"¥{price:,.0f} ≈ {price * rates.cny_rub():,.0f} ₽".replace(",", " ")
    return texts.TRIAL_BEST.format(brand=esc(brands.display_name(keyword)), price=money)


def _plan_rows(back_feed: bool = False):
    """Кнопки тарифов: PRO — первым (его и советуем), START — рядом, плюс «все тарифы»."""
    rows = [[btn(f"⚡ PRO — {plans.stars(plans.PRO.price_stars)}", "pl:p:pro")],
            [btn(f"START — {plans.stars(plans.START.price_stars)}", "pl:p:start"), btn("Все тарифы", "pl:open")]]
    if back_feed:
        rows.append([btn("📰 Лента", "fd:v:all:-:0")])
    return rows


async def build_nudge(db: Database, user_id: int, stage: str, start: int, until: int):
    """Текст и кнопки напоминания. stage: day1 / 6h / end."""
    watches = await db.list_watches(user_id)
    left = left_text(until - _now())
    names = _brand_list(watches)

    if stage == "day1":
        if not watches:
            return (texts.TRIAL_DAY1_NO_BRANDS.format(left=left),
                    kb([btn("➕ Выбрать бренд", "b:add:0")]))
        found = await db.feed_count(user_id, since=start)
        if not found:
            return (texts.TRIAL_DAY1_QUIET.format(brands=names, left=left),
                    kb([btn("➕ Добавить бренд", "b:add:0"), btn("💰 Бюджет", "b:list")]))
        text = texts.TRIAL_DAY1.format(found=_items(found), brands=names, left=left,
                                       best=await _best_line(db, user_id, start))
        return text, kb([btn("📰 Смотреть находки", "fd:v:all:-:0")], [btn("💎 Тарифы", "pl:open")])

    if stage == "6h":
        if not watches:
            return (texts.TRIAL_6H_NO_BRANDS.format(left=left),
                    kb([btn("➕ Выбрать бренд", "b:add:0")], [btn("💎 Тарифы", "pl:open")]))
        return texts.TRIAL_6H.format(left=left, brands=names), kb(*_plan_rows(back_feed=True))

    # stage == "end"
    # «Добавил бренды за тест» — бренд создан не раньше начала теста (с запасом на автозапуск)
    added = [w for w in watches if (w["created_at"] or 0) >= start - 300]
    if not added:
        rows = [[btn("💎 Тарифы", "pl:open"), btn("❓ Как это работает", "ob:0")]]
        if support_url():
            rows.append([url_btn("✍️ Написать в поддержку", support_url())])
        return texts.TRIAL_END_SOFT, kb(*rows)
    found = await db.feed_count(user_id, since=start)
    if found:
        summary = texts.TRIAL_END_FOUND.format(found=_items(found), brands=names,
                                               best=await _best_line(db, user_id, start))
    else:
        summary = texts.TRIAL_END_NOTHING.format(brands=names)
    return texts.TRIAL_END.format(summary=summary), kb(*_plan_rows())


# ---------------------------------------------------------------- фоновый цикл

def pick_stage(now: int, start: int, until: int, sent: set[str]) -> tuple[str | None, list[str]]:
    """
    Какое напоминание пора отправить и какие заодно считать «уже неактуальными».
    Возвращает (stage или None, список отметок, которые нужно поставить).
    Устаревшие этапы не шлём задним числом: если тест уже кончился, «осталось 6 часов» не нужно.
    """
    left = until - now
    if left <= 0:
        if "end" not in sent and -left <= END_WINDOW:
            return "end", ["day1", "6h", "end"]
        return None, []
    if left <= BEFORE_END:
        if "6h" not in sent and left > MIN_LEFT_FOR_6H:
            return "6h", ["day1", "6h"]
        return None, []
    if now - start >= DAY1_AFTER and "day1" not in sent:
        return "day1", ["day1"]
    return None, []


async def _trial_users(db: Database) -> list:
    """
    Все, у кого тест идёт или закончился недавно. Прямой запрос, а не метод в db.py:
    db.py правится параллельно, а здесь только чтение. Кто оплатил тариф — у того
    plan уже не 'trial', и напоминания про тест ему не придут.
    """
    cur = await db.conn.execute(
        "SELECT user_id, sub_until, paused FROM users WHERE plan = 'trial' AND trial_used = 1 AND sub_until > ?",
        (_now() - END_WINDOW,),
    )
    return list(await cur.fetchall())


async def check_trial_nudges(bot, db: Database) -> int:
    """Один проход: кому что пора отправить. Возвращает, сколько напоминаний ушло."""
    sent_count = 0
    now = _now()
    for row in await _trial_users(db):
        user_id = row["user_id"]
        if is_admin(user_id):
            continue
        settings = await db.get_settings(user_id)
        until = int(row["sub_until"])
        start = trial_start_ts(settings, until)
        done = set(settings.get("trial_nudges") or [])
        stage, marks = pick_stage(now, start, until, done)
        if not stage:
            continue
        # Пауза или тихие часы — не пропускаем, а откладываем до следующего прохода
        if row["paused"] or (settings.get("quiet") and is_quiet_now()):
            continue
        text, markup = await build_nudge(db, user_id, stage, start, until)
        try:
            await bot.send_message(user_id, text, reply_markup=markup)
            sent_count += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            continue  # не отметили — попробуем в следующий раз
        except TelegramForbiddenError:
            log.info("Тест-драйв: %s заблокировал бота — напоминание %s пропускаю", user_id, stage)
        except Exception as e:
            # Отмечаем и при ошибке: лучше не дослать одно напоминание, чем слать его каждые 10 минут
            log.warning("Тест-драйв: не удалось отправить %s пользователю %s: %s", stage, user_id, e)
        await db.update_settings(user_id, trial_nudges=sorted(done | set(marks)))
        await asyncio.sleep(0.1)  # не упираемся в лимиты Telegram
    return sent_count


async def trial_nudges_loop(bot, db: Database) -> None:
    """Фоновая задача: раз в ~10 минут проверяет тестовых пользователей. Запускается в bot.py."""
    if not config.TRIAL_DAYS:
        return
    await asyncio.sleep(60)  # даём боту спокойно запуститься
    while True:
        try:
            sent = await check_trial_nudges(bot, db)
            if sent:
                log.info("Тест-драйв: отправлено напоминаний — %d", sent)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле напоминаний тест-драйва")
        await asyncio.sleep(CHECK_EVERY_SEC)
