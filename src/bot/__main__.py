import asyncio
import logging
import sys
import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.core.config import settings
from src.core.database import init_db, async_sessionmaker
from src.services.drive_loader import sync_drive_documents
from .handlers import get_routers
from .middlewares.db import DbSessionMiddleware


def setup_logging():
    """Налаштування єдиного структурованого логування для проєкту."""
    if sys.platform == "win32":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


async def main():
    setup_logging()
    logger = structlog.get_logger("forest_bot.main")
    logger.info("forest_bot_starting")

    # 1. Ініціалізація таблиць у базі даних
    logger.info("initializing_database_tables")
    await init_db()
    logger.info("database_tables_initialized")

    # 2. Синхронізація стартових документів із Google Диску або initial_docs
    logger.info("starting_initial_drive_sync")
    async with async_sessionmaker() as session:
        await sync_drive_documents(session=session)
    logger.info("initial_drive_sync_completed")

    # 3. Налаштування Telegram бота та диспетчера
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        )
    )
    dp = Dispatcher()
    dp.include_routers(*get_routers())
    dp.update.middleware(DbSessionMiddleware(session_maker=async_sessionmaker))

    logger.info("forest_bot_ready_and_polling")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        structlog.get_logger("forest_bot.main").info("forest_bot_stopped")