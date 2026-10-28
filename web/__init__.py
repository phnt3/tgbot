"""Local/LAN web panel for PC Remote.

The panel is an optional, password-protected HTTP interface that runs inside
the bot process. It lets the owner control the PC from a browser on the same
network when Telegram is unreachable (VPN or proxy down).

Only the standard library is used; there are no extra dependencies.
"""

from typing import Any, Callable, Optional

__all__ = ["start_panel"]

_started = False


def start_panel(
    *,
    data_dir: Any,
    db_path: Any,
    scheduler: Optional[Any] = None,
    telegram_status: Optional[Callable[[], str]] = None,
) -> Optional[Any]:
    """Start the web panel once and return the server, or ``None``.

    Import failures and bind errors must never take the bot down, so every
    problem is logged and reported as ``None``.
    """
    global _started
    if _started:
        return None

    import logging

    logger = logging.getLogger("web.panel")
    try:
        from .server import PanelServer

        server = PanelServer(
            data_dir=data_dir,
            db_path=db_path,
            scheduler=scheduler,
            telegram_status=telegram_status,
        )
        server.start()
        _started = True
        return server
    except Exception:
        logger.exception("Failed to start the web panel")
        return None