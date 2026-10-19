"""Shared fixtures — reset the cached settings and doc-sync module between tests so env
overrides applied per-test always take effect.
"""

from __future__ import annotations

import sys

import pytest

import doc_cache_mcp.config as config
import doc_cache_mcp.docsync as docsync


@pytest.fixture(autouse=True)
def _reset_state():
    config.reset_settings()
    docsync.reset()
    # doc-sync.py imports its vendored allowlist as a top-level module. Without this, the
    # first doc-sync a test loads (live or fixture) decides which copy every later one gets.
    sys.modules.pop("doc_cache_allowlist", None)
    yield
    config.reset_settings()
    docsync.reset()
    sys.modules.pop("doc_cache_allowlist", None)
