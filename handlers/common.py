"""Common Telegram handlers for PC Remote.

Contains shared handler utilities and common command handlers used across
the application. All messages are plain text (no parse_mode) to avoid
Telegram Markdown entity-parse errors.
"""

from aiogram import types


async def send_welcome(message: types.Message) -> None:
    """Send a welcome message to the user.

    Args:
        message: The incoming message from the user.
    """
    user_name = message.from_user.first_name or "User"
    text = (
        f"Welcome to PC Remote, {user_name}!\n\n"
        "This is your Windows 11 monitoring and remote management bot.\n"
        "Use /help to see available commands, or the inline menu."
    )
    try:
        await message.answer(text, disable_web_page_preview=True)
    except Exception:
        pass


async def show_main_menu(message: types.Message) -> None:
    """Show the main menu inline keyboard.

    Args:
        message: The message to reply with the main menu.
    """
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="System Monitoring",
                callback_data="menu_system"
            ),
            types.InlineKeyboardButton(
                text="Launch Applications",
                callback_data="menu_apps"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Gaming Mode",
                callback_data="menu_gaming"
            ),
            types.InlineKeyboardButton(
                text="Power Management",
                callback_data="menu_power"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Screenshot",
                callback_data="menu_screenshot"
            ),
            types.InlineKeyboardButton(
                text="Running Processes",
                callback_data="menu_processes"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Alerts & History",
                callback_data="menu_alerts"
            ),
            types.InlineKeyboardButton(
                text="Charts",
                callback_data="chart_menu"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Files",
                callback_data="menu_files"
            ),
            types.InlineKeyboardButton(
                text="Disk Space",
                callback_data="menu_disk"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Media & Volume",
                callback_data="menu_media"
            ),
            types.InlineKeyboardButton(
                text="Network / VPN",
                callback_data="menu_network"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Scheduled Tasks",
                callback_data="menu_scheduler"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Clipboard",
                callback_data="menu_clipboard"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="Settings",
                callback_data="menu_settings"
            ),
        ],
    ])

    try:
        await message.answer("Main Menu - select a section:", reply_markup=keyboard, disable_web_page_preview=True)
    except Exception:
        pass


async def show_help(message: types.Message) -> None:
    """Show help information (plain text, no Markdown).

    Args:
        message: The incoming message from the user.
    """
    help_text = (
        "PC Remote - Bot Commands\n\n"
        "/start - start the bot and show the main menu\n"
        "/help - show this help message\n"
        "/status - show current system status\n\n"
        "Menu sections:\n"
        "- System Monitoring: CPU, RAM, GPU, disk metrics\n"
        "- Launch Applications: open configured applications\n"
        "- Gaming Mode: launch gaming profiles\n"
        "- Power Management: lock, sleep, hibernate, display off, shutdown, restart\n"
        "- Screenshot: take a desktop screenshot on demand\n"
        "- Running Processes: view top processes\n"
        "- Alerts & History: view alerts and monitoring history\n"
        "- Charts: CPU/GPU/RAM/disk history graphs (5m, 1h, 24h)\n"
        "- Files: browse folders, download files, search recursively, upload files to the PC\n"
        "- Disk Space: disk usage, temp/cache analysis and cleanup\n"
        "- Media & Volume: volume, mute and media transport keys\n"
        "- Network / VPN: active interfaces, Wi-Fi and VPN status\n"
        "- Scheduled Tasks: timed shutdown, restart, sleep, lock or profile launch\n"
        "- Clipboard: read, replace or clear the Windows clipboard text\n"
        "- Settings: change notifications, alerts, applications, profiles and backups\n\n"
        "Send a document or photo to the bot to save it in data/uploads.\n"
        "A settings backup is imported only from Settings -> Backup & restore.\n"
        "All actions require authorization. Only configured users can control this bot."
    )
    try:
        await message.answer(help_text, disable_web_page_preview=True)
    except Exception:
        pass