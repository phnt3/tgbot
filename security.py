"""Security module for PC Remote bot.

Handles authorization, input validation, and security checks.
"""

import logging
from typing import Callable, Optional, TypeVar

from config import is_user_authorized

T = TypeVar("T")

logger = logging.getLogger(__name__)


def authorize_handler(
    handler_func: Callable[..., T],
) -> Callable[..., T]:
    """Decorator to authorize a handler function.

    Wraps a handler to check if the user is authorized before execution.
    If the user is not authorized, the handler returns None and logs the rejection.

    Args:
        handler_func: The handler function to wrap.

    Returns:
        Wrapped function that checks authorization.
    """

    async def wrapper(message_or_callback, *args, **kwargs) -> Optional[T]:
        # Extract user_id from message or callback
        user_id: Optional[int] = None
        from aiogram import types

        if isinstance(message_or_callback, types.Message):
            user_id = message_or_callback.from_user.id
        elif isinstance(message_or_callback, types.CallbackQuery):
            user_id = message_or_callback.from_user.id
        else:
            logger.warning("Received update without Message or CallbackQuery")
            return None

        if user_id is None:
            logger.warning("Could not extract user_id from update")
            return None

        if not is_user_authorized(user_id):
            logger.warning(
                f"Unauthorized access attempt from user_id={user_id}"
            )
            # Send rejection message to the user
            if isinstance(message_or_callback, types.Message):
                try:
                    await message_or_callback.answer(
                        "❌ You are not authorized to use this bot.",
                        show_alert=True,
                    )
                except Exception:
                    pass
            elif isinstance(message_or_callback, types.CallbackQuery):
                try:
                    await message_or_callback.answer(
                        "❌ You are not authorized to use this bot.",
                        show_alert=True,
                    )
                except Exception:
                    pass
            return None

        return await handler_func(message_or_callback, *args, **kwargs)

    return wrapper


def validate_callback_data(
    callback_data: str,
    expected_prefix: str,
) -> bool:
    """Validate callback data format and prefix.

    Args:
        callback_data: The callback data string to validate.
        expected_prefix: The expected prefix for the callback data.

    Returns:
        True if the callback data is valid, False otherwise.
    """
    if not callback_data:
        return False
    if not expected_prefix:
        # Just check it's not empty and has reasonable format
        return len(callback_data) <= 100 and len(callback_data) > 0
    return callback_data.startswith(expected_prefix)


def validate_application_name(name: str) -> bool:
    """Validate an application name from the allowlist.

    Args:
        name: Application name to validate.

    Returns:
        True if the name is valid, False otherwise.
    """
    if not name or not name.strip():
        return False
    # Allow alphanumeric, spaces, dots, dashes, underscores, pluses
    import re
    return bool(re.match(r"^[a-zA-Z0-9\s\.\-_\+]+$", name.strip()))


def validate_path_safe(path_str: str) -> bool:
    """Validate that a path string is safe and doesn't contain suspicious patterns.

    Args:
        path_str: Path string to validate.

    Returns:
        True if the path appears safe, False otherwise.
    """
    if not path_str or not path_str.strip():
        return False
    # Check for common injection patterns
    import re
    dangerous_patterns = [
        r"[;&|`]",  # Shell metacharacters
        r"\.\.",   # Path traversal
        r"<",      # Redirect/less than
        r">",      # Redirect/greater than
        r"\|",     # Pipe
    ]
    for pattern in dangerous_patterns:
        if re.search(pattern, path_str):
            return False
    # Check that it looks like a valid path (letters, digits, separators)
    # Whitespace and parentheses allowed: Windows paths like "Program Files (x86)"
    # Colon allowed for drive letters: "C:\..."
    if not re.match(r"^[a-zA-Z0-9\\/.\-_:()+\s]+$", path_str):
        return False
    return True