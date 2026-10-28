"""Main entry point for PC Remote bot.

Initializes the application, sets up logging, starts the LAN web panel and
then connects to Telegram. The panel starts first, so the PC stays reachable
from a browser even while the VPN or proxy keeps Telegram offline.
"""

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.filters import Command

from config import config, load_config
from database import Database
from handlers.init import cmd_start, cmd_help, cmd_status
from handlers.common import send_welcome, show_main_menu

TELEGRAM_RETRY_SECONDS = 15
TELEGRAM_STATUS_SECONDS = 60


async def watch_telegram(bot: Bot, state: dict) -> None:
    """Keep the panel's Telegram badge close to reality."""
    while True:
        try:
            await bot.get_me()
        except Exception:  # noqa: BLE001 - any network error means offline
            state["status"] = "offline"
        else:
            state["status"] = "online"
        await asyncio.sleep(TELEGRAM_STATUS_SECONDS)


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
    panel = None
    telegram_state = {"status": "connecting"}

    def telegram_status() -> str:
        return telegram_state["status"]

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

        # The panel does not depend on Telegram: start it before the
        # connectivity check so a browser can control the PC offline.
        from web import start_panel

        panel = start_panel(
            data_dir=config["DATA_DIR"],
            db_path=db_path,
            scheduler=scheduler,
            telegram_status=telegram_status,
        )
        if panel is not None:
            logger.info(
                "Web panel started: %s (LAN %s)",
                panel.panel_info["urls"].get("local"),
                panel.panel_info["urls"].get("lan"),
            )

    except Exception as e:
        logger.error(f"Failed to create bot: {e}")
        db.close()
        sys.exit(1)

    # Cleanup on shutdown
    watcher = None
    try:
        # Telegram may be blocked while the VPN/proxy is down. Retry here
        # instead of exiting, so the web panel keeps serving the LAN.
        while True:
            try:
                me = await bot.get_me()
            except TelegramUnauthorizedError:
                telegram_state["status"] = "error"
                logger.error("Telegram rejected BOT_TOKEN; stopping the bot")
                return
            except Exception as e:  # noqa: BLE001 - network/proxy errors
                telegram_state["status"] = "offline"
                logger.warning(
                    "Telegram is unreachable (%s); panel stays up, retrying in %ss",
                    e,
                    TELEGRAM_RETRY_SECONDS,
                )
                await asyncio.sleep(TELEGRAM_RETRY_SECONDS)
            else:
                telegram_state["status"] = "online"
                logger.info("Telegram connected as @%s", me.username)
                break

        watcher = asyncio.create_task(
            watch_telegram(bot, telegram_state), name="telegram-watch"
        )
        await dispatcher.start_polling(bot, skip_updates=True)
    finally:
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass
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
        if panel is not None:
            try:
                panel.stop()
            except Exception as e:
                logger.error(f"Failed to stop web panel: {e}")
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