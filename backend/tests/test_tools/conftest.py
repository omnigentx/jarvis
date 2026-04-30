"""Stubs for heavy tool dependencies unavailable in the test environment."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True, scope="session")
def _stub_lunar_python():
    """lunar_python is an optional runtime dep — stub it so tool tests run
    without the full C-extension wheel being installed."""
    stub = MagicMock()
    stub.Lunar = MagicMock()
    stub.Solar = MagicMock()
    sys.modules.setdefault("lunar_python", stub)
    yield
