"""Configuration module for PC Remote bot.

Loads settings from .env file and provides centralized configuration access.
"""

import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv


def load_config() -> dict:
    """Load and validate configuration from .env file.

    Returns:
        dict with configuration values.

    Raises:
        ValueError: If required configuration is missing or invalid.
    """
    PROJECT_ROOT = Path(__file__).parent
    dotenv_path = PROJECT_ROOT / ".env"
    load_dotenv(dotenv_path=dotenv_path, override=True)

    bot_token = os.getenv("BOT_TOKEN")
    allowed_users_str = os.getenv("ALLOWED_USER_IDS", "")

    if not bot_token:
        raise ValueError("BOT_TOKEN environment variable is required")

    # Parse allowed user IDs
    allowed_user_ids: List[int] = []
    if allowed_users_str.strip():
        for part in allowed_users_str.split(","):
            part = part.strip()
            if part:
                try:
                    allowed_user_ids.append(int(part))
                except ValueError:
                    raise ValueError(
                        f"Invalid ALLOWED_USER_IDS value: '{part}'. "
                        "Must be comma-separated integer Telegram user IDs."
                    )

    config = {
        "BOT_TOKEN": bot_token,
        "ALLOWED_USER_IDS": allowed_user_ids,
        "BASE_DIR": PROJECT_ROOT,
        "DATA_DIR": PROJECT_ROOT / "data",
        "CONFIG_DIR": PROJECT_ROOT / "config",
        "LOG_DIR": PROJECT_ROOT / "logs",
    }

    # Validate allowed users are not empty
    if not config["ALLOWED_USER_IDS"]:
        import warnings
        warnings.warn(
            "ALLOWED_USER_IDS is empty. The bot will reject all users. "
            "Configure ALLOWED_USER_IDS in .env for production use.",
            UserWarning,
        )

    return config


# Module-level config (loaded on import)
config = load_config()


def is_user_authorized(user_id: int) -> bool:
    """Check if a Telegram user ID is authorized.

    Args:
        user_id: Telegram user ID to check.

    Returns:
        True if the user is authorized, False otherwise.
    """
    return user_id in config["ALLOWED_USER_IDS"]