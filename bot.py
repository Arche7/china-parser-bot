"""
Точка входа. Запуск:  python bot.py

Что происходит при запуске:
  1. Проверяем настройки (.env / переменные Railway).
  2. Подключаемся к базе данных.
  3. Создаём площадки (Goofish работает, 95分 — заготовка).
  4. Запускаем монитор в фоне и бота в режиме polling.
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

import config
import handlers
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


async def set_commands(bot: Bot) -> None:
    """Меню команд, которое видно в Telegram по кнопке «Меню»."""
    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="add", description="Добавить бренд"),
        BotCommand(command="preset", description="Готовый набор брендов"),
        BotCommand(command="list", description="Мои бренды"),
        BotCommand(command="del", description="Удалить бренд"),
        BotCommand(command="pause", description="Пауза уведомлений"),
        BotCommand(command="resume", description="Включить уведомления"),
        BotCommand(command="help", description="Помощь"),
    ])


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

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    monitor = Monitor(bot, db, sources)

    dp = Dispatcher()
    # Эти объекты будут автоматически передаваться в обработчики
    # (в функциях handlers.py есть параметры db и monitor)
    dp["db"] = db
    dp["monitor"] = monitor

    access = handlers.AccessMiddleware(db)
    handlers.router.message.middleware(access)
    handlers.router.callback_query.middleware(access)
    dp.include_router(handlers.router)

    await set_commands(bot)
    monitor_task = asyncio.create_task(monitor.run_forever())

    me = await bot.get_me()
    log.info("Бот @%s запущен. Админы: %s", me.username, config.ADMIN_IDS or "не заданы")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        monitor_task.cancel()
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
