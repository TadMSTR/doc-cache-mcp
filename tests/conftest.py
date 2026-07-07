"""Shared fixtures — reset the cached settings and doc-sync module between tests so env
overrides applied per-test always take effect.
"""

from __future__ import annotations

import pytest

import doc_cache_mcp.config as config
import doc_cache_mcp.docsync as docsync


@pytest.fixture(autouse=True)
def _reset_state():
    config.reset_settings()
    docsync.reset()
    yield
    config.reset_settings()
    docsync.reset()
