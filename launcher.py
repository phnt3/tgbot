"""Launcher (supervisor) for the PC Remote bot.

Started by the Windows Startup shortcut with pythonw.exe, so no console
window appears. Waits until Telegram is reachable (VPN/proxy may start
later than Windows), then runs main.py and restarts it after crashes.

Log file: logs/launcher.log
"""

import logging
import msvcrt
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
DATA_DIR = BASE_DIR / "data"
LOCK_FILE = DATA_DIR / "launcher.lock"

HOST = "api.telegram.org"
PORT = 443
CONNECT_TIMEOUT = 5
CHECK_INTERVAL = 15
RESTART_DELAY = 30
LOCK_BYTES = 1

LOG_DIR.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("launcher")
logger.setLevel(logging.INFO)
_handler = logging.FileHandler(LOG_DIR / "launcher.log", encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)


def acquire_lock():
    """Return an open locked file object, or None if a launcher is active."""
    lock_file = open(LOCK_FILE, "a+")
    try:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, LOCK_BYTES)
    except OSError:
        lock_file.close()
        return None

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"{os.getpid()}\n")
    lock_file.flush()
    return lock_file


def network_ready() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def wait_for_network() -> None:
    attempt = 0
    while not network_ready():
        attempt += 1
        if attempt == 1 or attempt % 4 == 0:
            logger.info("Telegram is unreachable, waiting (attempt %s)", attempt)
        time.sleep(CHECK_INTERVAL)
    logger.info("Telegram is reachable")


def run_bot() -> int:
    logger.info("Starting bot")
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "main.py")],
        cwd=str(BASE_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info("Bot exited with code %s", result.returncode)
    return result.returncode


def main() -> int:
    lock = acquire_lock()
    if lock is None:
        logger.info("Another launcher is already running, exiting")
        return 0

    logger.info("Launcher started (pid=%s)", os.getpid())
    while True:
        wait_for_network()
        code = run_bot()
        if code == 0:
            logger.info("Clean shutdown, launcher exiting")
            return 0
        logger.info("Restarting bot in %s seconds", RESTART_DELAY)
        time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)