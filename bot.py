"""Telegram bot implementation for PC Remote.

Provides the Telegram bot interface with command handlers and callback
query handlers for system monitoring and remote management.
"""

from aiogram import Router, types
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery

from .security import authorize_handler, validate_callback_data
from . import database

router = Router()


# /start command handler
@router.message(Command("start"))
@authorize_handler
async def cmd_start(message: Message) -> None:
    """Handle /start command.

    Sends a welcome message with the main menu.
    """
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"

    welcome_text = (
        f"👋 Hello, {user_name}!\n\n"
        "Welcome to PC Remote - your Windows 11 monitoring and remote management assistant.\n\n"
        "I can help you monitor system resources, launch applications, "
        "manage power settings, and more.\n\n"
        "Use /help to see available commands, or use the inline menu below."
    )

    # Create main menu keyboard
    keyboard = types.InlineKeyboardMarkup(inline_keyboard=[
        [
            types.InlineKeyboardButton(
                text="📊 System Monitoring",
                callback_data="menu_system"
            ),
            types.InlineKeyboardButton(
                text="🚀 Launch Applications",
                callback_data="menu_apps"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="🎮 Gaming Mode",
                callback_data="menu_gaming"
            ),
            types.InlineKeyboardButton(
                text="⚡ Power Management",
                callback_data="menu_power"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="📸 Screenshot",
                callback_data="menu_screenshot"
            ),
            types.InlineKeyboardButton(
                text="⚙️ Running Processes",
                callback_data="menu_processes"
            ),
        ],
        [
            types.InlineKeyboardButton(
                text="🚨 Alerts & History",
                callback_data="menu_alerts"
            ),
            types.InlineKeyboardButton(
                text="⚙️ Settings",
                callback_data="menu_settings"
            ),
        ],
    ])

    try:
        await message.answer(welcome_text, reply_markup=keyboard, disable_web_page_preview=True)
    except Exception as e:
        # Log but don't crash - user might have blocked the bot
        import logging
        logging.getLogger(__name__).error(f"Failed to send start message: {e}")


# /help command handler
@router.message(Command("help"))
@authorize_handler
async def cmd_help(message: Message) -> None:
    """Handle /help command.

    Lists available commands and features.
    """
    help_text = (
        "🛟 *PC Remote - Bot Commands*\n\n"
        "/start - Start the bot and show main menu\n"
        "/help - Show this help message\n"
        "/status - Show current system status\n\n"
        "Menu options:\n"
        "• System Monitoring - CPU, RAM, GPU, disk metrics\n"
        "• Launch Applications - Open configured applications\n"
        "• Gaming Mode - Launch gaming applications profiles\n"
        "• Power Management - Lock, sleep, shutdown, restart\n"
        "• Screenshot - Take desktop screenshot on demand\n"
        "• Running Processes - View and manage processes\n"
        "• Alerts & History - View alerts and monitoring history\n"
        "• Settings - Configure bot preferences\n\n"
        "All actions require authorization. Only configured users can control this bot."
    )

    try:
        await message.answer(help_text, disable_web_page_preview=True)
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to send help message: {e}")


# /status command handler
@router.message(Command("status"))
@authorize_handler
async def cmd_status(message: Message) -> None:
    """Handle /status command.

    Shows current system status summary.
    """
    from .monitor import get_system_metrics

    try:
        metrics = await get_system_metrics()

        status_text = "🖥️ *System Status*\n\n"

        # System metrics
        status_text += "📋 *System Information*\n"
        if metrics.get("hostname"):
            status_text += f"• Hostname: {metrics['hostname']}\n"
        else:
            status_text += "• Hostname: N/A\n"

        if metrics.get("uptime"):
            uptime = metrics["uptime"]
            # Format uptime
            days = int(uptime / 86400)
            hours = int((uptime % 86400) / 3600)
            minutes = int((uptime % 3600) / 60)
            if days > 0:
                status_text += f"• Uptime: {days}d {hours}h {minutes}m\n"
            else:
                status_text += f"• Uptime: {hours}h {minutes}m\n"
        else:
            status_text += "• Uptime: N/A\n"

        if metrics.get("internet") is not None:
            internet_str = "✅ Connected" if metrics["internet"] else "❌ Disconnected"
            status_text += f"• Internet: {internet_str}\n"
        else:
            status_text += "• Internet: N/A\n"

        status_text += "\n🌡️ *Monitoring Agent Status:* ✅ Online\n"

        # Display monitoring metrics
        status_text += "\n📊 *Current Metrics*\n"

        # CPU
        if metrics.get("cpu"):
            cpu = metrics["cpu"]
            cpu_str = cpu.get("utilization", "N/A")
            if cpu_str != "N/A":
                cpu_str += "%"
            status_text += f"• CPU Utilization: {cpu_str}\n"
            if cpu.get("temperature") is not None:
                status_text += f"• CPU Temperature: {cpu['temperature']}°C\n"
            if cpu.get("frequency") is not None:
                status_text += f"• CPU Frequency: {cpu['frequency']} MHz\n"
            status_text += f"• Cores: {cpu.get('physical_cores', 0)} physical, "
            status_text += f"{cpu.get('logical_cores', 0)} logical\n"

        # RAM
        if metrics.get("ram"):
            ram = metrics["ram"]
            used = ram.get("used", 0)
            total = ram.get("total", 0)
            if total and total > 0:
                util = (used / total) * 100
                status_text += f"• RAM: {used:.1f}/{total:.1f} GB ({util:.1f}%)\n"
            else:
                status_text += f"• RAM: {used:.1f}/{total:.1f} GB (N/A)\n"

        # GPU
        if metrics.get("gpu"):
            gpu = metrics["gpu"]
            gpu_parts = []
            if gpu.get("utilization") is not None:
                gpu_parts.append(f"Util: {gpu['utilization']}%")
            if gpu.get("temperature") is not None:
                gpu_parts.append(f"Temp: {gpu['temperature']}°C")
            if gpu.get("memory_used") is not None and gpu.get("memory_total") is not None:
                gpu_parts.append(
                    f"VRAM: {gpu['memory_used']:.1f}/{gpu['memory_total']:.1f} GB"
                )
            if gpu.get("power") is not None:
                gpu_parts.append(f"Power: {gpu['power']}W")
            status_text += f"• GPU: {', '.join(gpu_parts) if gpu_parts else 'N/A'}\n"

        # Disk
        if metrics.get("disk"):
            disk = metrics["disk"]
            if isinstance(disk, dict):
                for drive, info in disk.items():
                    if info.get("total", 0) > 0:
                        util = (info.get("used", 0) / info.get("total", 1)) * 100
                        status_text += (
                            f"• {drive}: {info.get('used', 0):.1f}/{info.get('total', 0):.1f} GB "
                            f"({util:.1f}%)\n"
                        )
                    else:
                        status_text += f"• {drive}: N/A\n"
            elif isinstance(disk, str):
                status_text += f"• Disk: {disk}\n"

        try:
            await message.answer(status_text, disable_web_page_preview=True)
        except Exception:
            pass

    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error getting system status: {e}")
        try:
            await message.answer(
                "❌ Failed to retrieve system status. Please try again later.",
                disable_web_page_preview=True,
            )
        except Exception:
            pass


# Callback query handler for main menu
@router.callback_query()
@authorize_handler
async def callback_query_handler(callback: CallbackQuery) -> None:
    """Handle callback queries from inline keyboard.

    Routes to appropriate handler based on callback data prefix.
    """
    data = callback.data

    # Validate callback data
    if not validate_callback_data(data, "menu_"):
        await callback.answer("❌ Invalid callback data", show_alert=True)
        return

    # Route to menu handlers
    routing = {
        "menu_system": lambda: callback.message.answer("📊 System monitoring selected"),
        "menu_apps": lambda: callback.message.answer("🚀 Application launching selected"),
        "menu_gaming": lambda: callback.message.answer("🎮 Gaming mode selected"),
        "menu_power": lambda: callback.message.answer("⚡ Power management selected"),
        "menu_screenshot": lambda: callback.message.answer("📸 Screenshot selected"),
        "menu_processes": lambda: callback.message.answer("⚙️ Processes selected"),
        "menu_alerts": lambda: callback.message.answer("🚨 Alerts & history selected"),
        "menu_settings": lambda: callback.message.answer("⚙️ Settings selected"),
    }

    handler = routing.get(data)
    if handler:
        await handler()
    else:
        await callback.answer("❌ Unknown menu option", show_alert=True)

    # Always answer the callback to stop the loading spinner
    try:
        await callback.answer()
    except Exception:
        pass