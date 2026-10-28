"""Main entry point for PC Remote bot.

Initializes the application, sets up logging, and starts the Telegram bot.
"""

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command

from config import config, load_config
from database import Database
from handlers.init import cmd_start, cmd_help, cmd_status
from handlers.common import send_welcome, show_main_menu


def setup_logging() -> None:
    """Configure logging for the application."""
    log_dir = config["LOG_DIR"]
    log_dir.mkdir(parents=True, exist_ok=True)

    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_level = logging.INFO

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "bot.log", encoding="utf-8"),
        ],
    )

    # Set specific logger levels
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def main() -> None:
    """Main function to start the bot."""
    # Ensure configuration is loaded
    config = load_config()

    # Setup logging
    setup_logging()

    logger = logging.getLogger(__name__)
    logger.info("Starting PC Remote bot...")

    # Settings live in the bot database; fill any missing section from the
    # config folder once, at startup, before anything reads them.
    db_path = config["DATA_DIR"] / "pc_remote.db"
    from settings_store import ensure_defaults

    try:
        created = ensure_defaults(db_path)
        if created:
            logger.info(
                "Seeded settings sections: %s", ", ".join(sorted(created))
            )
    except Exception as e:
        logger.error(f"Failed to seed settings: {e}")

    background = None
    scheduler = None

    # Initialize database
    try:
        db = Database(db_path)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        sys.exit(1)

    # Create bot and dispatcher
    try:
        bot = Bot(token=config["BOT_TOKEN"])
        dispatcher = Dispatcher()

        # Register command handlers
        @dispatcher.message(Command("start"))
        async def cmd_start_handler(message: types.Message) -> None:
            await cmd_start(message)

        @dispatcher.message(Command("help"))
        async def cmd_help_handler(message: types.Message) -> None:
            await cmd_help(message)

        @dispatcher.message(Command("status"))
        async def cmd_status_handler(message: types.Message) -> None:
            await cmd_status(message)

        # Register inline keyboard handlers
        from handlers import register_init_callbacks, register_common_callbacks

        register_init_callbacks(dispatcher)
        register_common_callbacks(dispatcher)

        # Save documents and photos sent to the bot (authorization enforced
        # inside the handler).
        from handlers import features

        @dispatcher.message(F.document | F.photo)
        async def upload_handler(message: types.Message) -> None:
            await features.handle_upload(message)

        # Free-text answers: file search and clipboard prompts first, then
        # settings prompts. Registered last so commands and uploads keep
        # priority; harmless when nothing is pending.
        from handlers import clipboard_ui, settings_ui

        @dispatcher.message(F.text & ~F.text.startswith("/"))
        async def text_handler(message: types.Message) -> None:
            if await features.handle_text(message):
                return
            if await clipboard_ui.handle_text(message):
                return
            await settings_ui.handle_text(message)

        # Periodic sampling: writes history and pushes threshold alerts.
        from background import BackgroundMonitor
        from notifier import Notifier
        from scheduler import TaskScheduler

        notifier = Notifier()
        background = BackgroundMonitor(
            bot,
            db,
            user_ids=config["ALLOWED_USER_IDS"],
            notifier=notifier,
        )
        background.start()
        logger.info(
            "Background monitor scheduled (interval=%ss)",
            background.interval_seconds,
        )

        # Timed shutdown/restart/sleep/lock/profile tasks.
        scheduler = TaskScheduler(
            config["DATA_DIR"] / "scheduled_tasks.json",
            bot=bot,
            user_ids=config["ALLOWED_USER_IDS"],
            db=db,
        )
        features.set_scheduler(scheduler)
        scheduler.start()

    except Exception as e:
        logger.error(f"Failed to create bot: {e}")
        db.close()
        sys.exit(1)

    # Cleanup on shutdown
    try:
        await dispatcher.start_polling(bot, skip_updates=True)
    finally:
        if scheduler is not None:
            try:
                await scheduler.stop()
            except Exception as e:
                logger.error(f"Failed to stop scheduler: {e}")
        if background is not None:
            try:
                await background.stop()
            except Exception as e:
                logger.error(f"Failed to stop background monitor: {e}")
        try:
            await bot.session.close()
        except Exception:
            pass
        db.close()
        logger.info("Bot shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except SystemExit:
        raise
    except Exception as e:
        logging.getLogger(__name__).error(f"Fatal error: {e}")
        sys.exit(1)