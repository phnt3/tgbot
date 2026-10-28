"""Pytest configuration: make the project root importable (absolute imports)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def clean_pending_confirmations():
    """Start every test with no armed confirmations or text prompts.

    This state is module-level, so leaking it between tests could make a later
    test execute a destructive action or swallow an unrelated text message.
    """
    import handlers
    from handlers import clipboard_ui, features

    handlers._PENDING_CONFIRMATIONS.clear()
    clipboard_ui.clear_state()
    features._SEARCH_PENDING.clear()
    features._SEARCH_STATE.clear()
    yield
    handlers._PENDING_CONFIRMATIONS.clear()
    clipboard_ui.clear_state()
    features._SEARCH_PENDING.clear()
    features._SEARCH_STATE.clear()
