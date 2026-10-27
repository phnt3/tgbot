"""Pytest configuration: make the project root importable (absolute imports)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


@pytest.fixture(autouse=True)
def clean_pending_confirmations():
    """Start every test with no armed confirmations.

    Confirmation state is module-level in handlers, so leaking it between
    tests could make a later test execute a destructive action.
    """
    import handlers

    handlers._PENDING_CONFIRMATIONS.clear()
    yield
    handlers._PENDING_CONFIRMATIONS.clear()
