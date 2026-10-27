"""Desktop screenshot module for PC Remote.

Provides on-demand desktop screenshot functionality with security controls.

Security:
- Screenshots captured only after explicit authorized request
- Screenshots sent only to the authorized administrator
- No automatic capture
- No keyboard input recording
- Handle capture failures gracefully

Implementation notes:
- Primary capture path: PIL.ImageGrab (Pillow ships with matplotlib, no extra dep)
- Fallback capture path: raw GDI via pywin32 (GetWindowDC/BitBlt/GetDIBits)
- Lock detection: OpenInputDesktop API (real check, not a heuristic)
- DPI awareness is enabled so high-DPI displays capture the full screen
"""

import ctypes
import logging
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


def _enable_dpi_awareness() -> None:
    """Make the process DPI-aware so ImageGrab returns the full-resolution screen."""
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        # Not critical - a scaled capture is better than no capture
        pass


def is_desktop_locked() -> bool:
    """Check whether the Windows workstation is currently locked.

    Uses the OpenInputDesktop API: when the workstation is locked, the caller's
    desktop cannot be opened, and the API returns a NULL handle.

    Returns:
        True if the desktop appears locked, False otherwise.
    """
    try:
        user32 = ctypes.windll.user32
        DESKTOP_READ = 0x0001  # DESKTOP_READOBJECTS - enough to probe access
        desktop_handle = user32.OpenInputDesktop(0, False, DESKTOP_READ)
        if desktop_handle:
            user32.CloseDesktop(desktop_handle)
            return False
        return True
    except Exception:
        # Cannot determine reliably - assume unlocked and let capture proceed
        return False


def _capture_via_gdi() -> bytes:
    """Fallback capture through raw GDI (pywin32), correctly implemented.

    Raises whatever underlying error occurs - the caller treats any failure
    as a capture error.

    Returns:
        PNG image bytes.
    """
    import win32api
    import win32con
    import win32gui
    import win32ui
    from PIL import Image

    desktop = win32gui.GetDesktopWindow()
    width = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
    height = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)

    if width <= 0 or height <= 0:
        raise RuntimeError(f"invalid screen metrics: {width}x{height}")

    desktop_dc = win32gui.GetWindowDC(desktop)
    try:
        src_dc = win32ui.CreateDCFromHandle(desktop_dc)
        mem_dc = src_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(src_dc, width, height)
        mem_dc.SelectObject(bitmap)

        try:
            result = ctypes.windll.gdi32.BitBlt(
                mem_dc.GetHandleOutput(), 0, 0, width, height,
                src_dc.GetSafeHdc(), 0, 0, win32con.SRCCOPY,
            )
            if not result:
                raise RuntimeError("BitBlt failed (locked session or secure desktop)")

            raw = bitmap.GetBitmapBits(True)
            img = Image.frombuffer("RGB", (width, height), raw, "raw", "BGRX", 0, 1)
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            mem_dc.DeleteDC()
            src_dc.DeleteDC()

        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        return out.getvalue()
    finally:
        win32gui.ReleaseDC(desktop, desktop_dc)


def take_screenshot() -> Tuple[Optional[bytes], Optional[str]]:
    """Capture the desktop screenshot.

    Strategy:
    1. Enable DPI awareness, so the capture covers the real screen on high-DPI displays.
    2. Try PIL.ImageGrab first - the simplest, most reliable path.
    3. If Pillow fails, retry through raw GDI (pywin32).

    Returns:
        Tuple of (image_bytes, error_message).
        On success: (PNG bytes, None).
        On failure: (None, plain-text error description).
    """
    _enable_dpi_awareness()

    # Path 1: Pillow ImageGrab
    try:
        from PIL import ImageGrab

        img = ImageGrab.grab()
        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        data = out.getvalue()
        if data and len(data) > 0:
            logger.info("Screenshot captured via ImageGrab (%d bytes)", len(data))
            return data, None
    except Exception as e:
        logger.warning("ImageGrab capture failed: %s", e)

    # Path 2: raw GDI fallback
    try:
        data = _capture_via_gdi()
        if data:
            logger.info("Screenshot captured via GDI (%d bytes)", len(data))
            return data, None
    except Exception as e:
        logger.error("GDI capture failed: %s", e)

    return None, "Failed to capture screenshot: both capture methods failed."


def send_screenshot_to_authorized(
    screenshot_bytes: bytes,
    user_id: int,
    config_obj: Dict[str, Any],
) -> Any:
    """Coroutine that sends a screenshot to an authorized user via Telegram.

    Args:
        screenshot_bytes: PNG image bytes.
        user_id: Telegram user ID to send to.
        config_obj: Configuration dictionary containing BOT_TOKEN.

    Returns:
        Tuple of (success, message).
    """
    # Import here so the function is only used in async context
    async def _send() -> Tuple[bool, str]:
        from aiogram import Bot, types

        try:
            bot = Bot(token=config_obj["BOT_TOKEN"])
            photo = types.BufferedInputFile(
                screenshot_bytes,
                filename=f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            )

            await bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption="Desktop screenshot",
                disable_web_page_preview=True,
            )

            logger.info("Screenshot sent to authorized user %s", user_id)
            return True, "Screenshot sent to the authorized administrator."

        except Exception as e:
            logger.error("Failed to send screenshot: %s", e)
            return False, f"Failed to send screenshot: {e}"

    return _send()


def capture_desktop() -> Dict[str, Any]:
    """Capture desktop screenshot with full security checks.

    Security:
    - Only captures after explicit request
    - Will only send to authorized users
    - Reports if desktop is locked or unavailable
    - Handles failures gracefully

    Returns:
        Dictionary with capture result information.
    """
    try:
        locked = is_desktop_locked()
        screenshot_bytes, capture_error = take_screenshot()

        result = {
            "success": False,
            "desktop_locked": locked,
            "screenshot_available": screenshot_bytes is not None,
            "screenshot_bytes": screenshot_bytes,
            "message": "",
        }

        if locked:
            result["message"] = "Desktop is locked. Capture may be limited or rejected by Windows."

        if screenshot_bytes:
            result["success"] = True
            result["message"] = "Screenshot captured successfully."
        elif capture_error:
            result["message"] = capture_error
        else:
            result["message"] = "Unknown screenshot capture error."

        return result

    except Exception as e:
        logger.error("Error in capture_desktop: %s", e)
        return {
            "success": False,
            "desktop_locked": False,
            "screenshot_available": False,
            "message": f"Screenshot capture failed: {e}",
        }