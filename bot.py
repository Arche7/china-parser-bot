"""
Точка входа. Запуск:  python bot.py

Что происходит при запуске:
  1. Проверяем настройки (.env / переменные Railway).
  2. Подключаемся к базе данных (старая база обновится сама).
  3. Создаём площадки (Goofish работает, 95分 — заготовка).
  4. Обновляем описание бота, меню команд и курс юаня.
  5. Запускаем монитор в фоне и бота в режиме polling.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, MenuButtonWebApp, WebAppInfo
from aiohttp import web

import config
import handlers
import rates
import texts
from avito import AvitoPrices
from db import Database
from monitor import Monitor
from sources import Fen95Source, GoofishSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
# Библиотеки пишут слишком много — оставим только предупреждения
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apify_client").setLevel(logging.WARNING)
log = logging.getLogger("bot")


async def set_profile(bot: Bot) -> None:
    """Меню команд и описание бота (то, что видно до нажатия «Старт»)."""
    await bot.set_my_commands([
        BotCommand(command="start", description="Главная"),
        BotCommand(command="feed", description="Лента находок"),
        BotCommand(command="add", description="Добавить бренд"),
        BotCommand(command="list", description="Мои бренды"),
        BotCommand(command="help", description="Как это работает"),
        BotCommand(command="paysupport", description="Вопросы по оплате"),
    ])
    try:
        await bot.set_my_name(config.BRAND_NAME)
    except Exception as e:  # Telegram ограничивает частоту смены имени — не критично
        log.warning("Не удалось обновить имя бота: %s", e)
    try:
        await bot.set_my_short_description(texts.BOT_SHORT_DESCRIPTION[:120])
        await bot.set_my_description(texts.BOT_DESCRIPTION[:512])
    except Exception as e:  # Telegram ограничивает частоту — не критично
        log.warning("Не удалось обновить описание бота: %s", e)


async def seed_admin_brands(db: Database) -> None:
    """
    Один раз добавляет админам бренды из ADMIN_SEED_BRANDS (по умолчанию
    Goyard, Tom Ford, Dior) с ценой «Готового набора». Если потом удалишь
    бренд — он не вернётся: бот запоминает, что уже добавлял.
    """
    import brands
    for admin_id in config.ADMIN_IDS:
        if not await db.get_user(admin_id):
            continue
        settings = await db.get_settings(admin_id)
        done = set(settings.get("seeded", []))
        added = []
        for keyword in config.ADMIN_SEED_BRANDS:
            key = brands.canonical(keyword)
            if key in done:
                continue
            if not await db.get_watch_by_keyword(admin_id, key):
                await db.add_watch(admin_id, key, float(brands.PRESET_PRICE_MIN), float(brands.PRESET_PRICE_MAX))
                added.append(key)
            done.add(key)
        await db.update_settings(admin_id, seeded=sorted(done))
        if added:
            log.info("Админу %s добавлены бренды: %s", admin_id, ", ".join(added))


async def main() -> None:
    config.check_config()

    db = Database(config.DB_PATH)
    await db.connect()
    log.info("База данных: %s", config.DB_PATH)

    sources = [
        GoofishSource(
            token=config.APIFY_TOKEN,
            actor_id=config.APIFY_ACTOR_ID,
            proxy_country=config.APIFY_PROXY_COUNTRY,
        ),
        Fen95Source(),
    ]
    sources_map = {s.name: s for s in sources}

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    monitor = Monitor(bot, db, sources)
    avito = AvitoPrices(db)

    dp = Dispatcher()
    # Эти объекты автоматически передаются в обработчики
    # (у функций в папке handlers есть параметры db, monitor, avito)
    dp["db"] = db
    dp["monitor"] = monitor
    dp["avito"] = avito
    dp["sources"] = sources_map
    dp.include_router(handlers.setup(db))

    await rates.refresh()
    await set_profile(bot)
    import brands
    fixed = await db.normalize_keywords(brands.canonical)
    if fixed:
        log.info("Бренды приведены к одному написанию: %d", fixed)
    await seed_admin_brands(db)

    # Мини-приложение HUNTR: веб-сервер в том же процессе (Railway даёт PORT и домен)
    from webapp.server import create_app
    runner = web.AppRunner(create_app(db, bot, monitor, avito, sources_map))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", config.WEB_PORT).start()
    log.info("Приложение HUNTR слушает порт %s, адрес: %s", config.WEB_PORT, config.WEBAPP_URL or "домен не задан")
    if config.WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
                text=config.BRAND_NAME, web_app=WebAppInfo(url=config.WEBAPP_URL)))
        except Exception as e:
            log.warning("Не удалось поставить кнопку приложения: %s", e)

    monitor_task = asyncio.create_task(monitor.run_forever())

    me = await bot.get_me()
    log.info("Бот @%s запущен. Админы: %s", me.username, config.ADMIN_IDS or "не заданы")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        monitor_task.cancel()
        await runner.cleanup()
        for source in sources:
            await source.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code not in (None, 0):
            raise
        log.info("Бот остановлен")
