"""Initial Telegram handlers for PC Remote.

Contains handlers for bot startup, configuration, and initial setup.
All messages are plain text (no parse_mode) to avoid Telegram Markdown
entity-parse errors.
"""

import asyncio
import logging
from aiogram import types

from .common import show_main_menu, send_welcome

logger = logging.getLogger(__name__)


def fmt_uptime(seconds: float) -> str:
    """Format uptime seconds into a human-readable string."""
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m"


async def cmd_start(message: types.Message) -> None:
    """Handle /start command - shown on first run.

    Args:
        message: The incoming message from the user.
    """
    from security import is_user_authorized

    user_id = message.from_user.id

    if not is_user_authorized(user_id):
        logger.warning(f"Unauthorized /start from user_id={user_id}")
        await message.answer(
            "You are not authorized to use this bot.\n"
            "Contact the bot administrator.",
        )
        return

    await show_main_menu(message)


async def cmd_help(message: types.Message) -> None:
    """Handle /help command.

    Args:
        message: The incoming message from the user.
    """
    from security import is_user_authorized

    user_id = message.from_user.id

    if not is_user_authorized(user_id):
        logger.warning(f"Unauthorized /help from user_id={user_id}")
        await message.answer("You are not authorized to use this bot.")
        return

    await send_welcome(message)


async def cmd_status(message: types.Message) -> None:
    """Handle /status command - full system status (plain text).

    Args:
        message: The incoming message from the user.
    """
    from security import is_user_authorized
    from monitor import get_all_metrics

    user_id = message.from_user.id

    if not is_user_authorized(user_id):
        logger.warning(f"Unauthorized /status from user_id={user_id}")
        await message.answer("You are not authorized to use this bot.")
        return

    def md_safe(v) -> str:
        return str(v).replace("*", "").replace("_", "").replace("`", "")

    try:
        metrics = await asyncio.to_thread(get_all_metrics)
        lines = ["=== SYSTEM STATUS ===", ""]

        hostname = metrics.get("hostname", "N/A")
        uptime = metrics.get("uptime")
        internet = metrics.get("internet")

        lines.append(f"Hostname: {md_safe(hostname)}")

        if uptime is not None:
            days = int(uptime // 86400)
            hours = int((uptime % 86400) // 3600)
            minutes = int((uptime % 3600) // 60)
            uptime_str = f"{days}d {hours}h {minutes}m" if days > 0 else f"{hours}h {minutes}m"
        else:
            uptime_str = "N/A"
        lines.append(f"Uptime: {uptime_str}")
        lines.append(f"Internet: {'Connected' if internet else 'Disconnected'}")
        lines.append("Agent: ONLINE")
        lines.append("")

        cpu = metrics.get("cpu", {})
        lines.append("-- CPU --")
        cpu_util = cpu.get("utilization", "N/A")
        if cpu_util != "N/A" and cpu_util is not None:
            lines.append(f"Utilization: {cpu_util}%")
        else:
            lines.append("Utilization: N/A")
        cpu_temp = cpu.get("temperature")
        if cpu_temp and cpu_temp != "N/A":
            lines.append(f"Temperature: {cpu_temp} C")
        else:
            lines.append("Temperature: N/A")
        freq = cpu.get("frequency")
        if freq and freq != "N/A":
            lines.append(f"Frequency: {int(freq)} MHz")
        else:
            lines.append("Frequency: N/A")
        lines.append(f"Cores: {cpu.get('physical_cores', 'N/A')} physical, {cpu.get('logical_cores', 'N/A')} logical")
        lines.append("")

        ram = metrics.get("ram", {})
        lines.append("-- RAM --")
        ram_util = ram.get("utilization_percent", "N/A")
        if ram_util != "N/A" and ram_util is not None:
            total = (ram.get("total_mb", 0) or 0) / 1024
            used = (ram.get("used_mb", 0) or 0) / 1024
            lines.append(f"Used: {used:.1f}/{total:.1f} GB ({ram_util:.1f}%)")
        else:
            lines.append("RAM: N/A")
        lines.append("")

        gpu = metrics.get("gpu", {})
        lines.append("-- GPU --")
        gpu_parts = []
        if gpu.get("utilization") != "N/A" and gpu.get("utilization") is not None:
            gpu_parts.append(f"Util: {gpu['utilization']}%")
        if gpu.get("temperature") != "N/A" and gpu.get("temperature") is not None:
            gpu_parts.append(f"Temp: {gpu['temperature']} C")
        if gpu.get("vram_used_mb") != "N/A" and gpu.get("vram_total_mb") != "N/A" and gpu.get("vram_used_mb") is not None:
            gpu_parts.append(f"VRAM: {gpu['vram_used_mb']:.1f}/{gpu['vram_total_mb']:.1f} GB")
        if gpu.get("power") != "N/A" and gpu.get("power") is not None:
            gpu_parts.append(f"Power: {gpu['power']}W")
        lines.append(", ".join(gpu_parts) if gpu_parts else "N/A")
        lines.append("")

        disks = metrics.get("disk", {})
        lines.append("-- Disk --")
        if disks:
            for drive, info in disks.items():
                if isinstance(info, dict) and info.get("utilization_percent", "N/A") != "N/A":
                    lines.append(f"{md_safe(drive)}: {info.get('utilization_percent')}%")
        else:
            lines.append("No disk data available")

        await message.answer("\n".join(lines), disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Error in cmd_status: {e}")
        await message.answer(
            "Failed to retrieve system status. Please try again later.",
            disable_web_page_preview=True,
        )