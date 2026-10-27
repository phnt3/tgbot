"""Volume and media-key control for Windows 11.

Uses the Win32 ``keybd_event`` API to synthesize the standard multimedia keys
(volume up/down/mute, play/pause, next, previous, stop). No third-party
dependency is required and no arbitrary keys can be injected through the bot.
"""

import ctypes
import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_STOP = 0xB2
VK_MEDIA_PLAY_PAUSE = 0xB3

KEYEVENTF_KEYUP = 0x0002

MEDIA_ACTIONS: Dict[str, Tuple[str, int]] = {
    "vol_up": ("Volume up", VK_VOLUME_UP),
    "vol_down": ("Volume down", VK_VOLUME_DOWN),
    "mute": ("Mute toggle", VK_VOLUME_MUTE),
    "play_pause": ("Play/Pause", VK_MEDIA_PLAY_PAUSE),
    "next": ("Next track", VK_MEDIA_NEXT_TRACK),
    "prev": ("Previous track", VK_MEDIA_PREV_TRACK),
    "stop": ("Stop playback", VK_MEDIA_STOP),
}


def _press_vk(vk: int) -> None:
    """Send one key press/release pair for a virtual-key code."""
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def press_key(action: str) -> Tuple[bool, str]:
    """Execute a whitelisted media action.

    Args:
        action: One of the keys in ``MEDIA_ACTIONS``.

    Returns:
        Tuple of (success, message).
    """
    spec = MEDIA_ACTIONS.get(action)
    if spec is None:
        return False, f"Unknown media action: {action}"

    label, vk = spec
    try:
        _press_vk(vk)
        return True, f"{label} sent."
    except Exception as e:
        logger.exception("Failed to send media key %s", action)
        return False, f"Failed to send {label}: {e}"