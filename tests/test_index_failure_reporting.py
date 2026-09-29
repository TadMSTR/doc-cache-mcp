"""vikunja#372 — an index failure reported by doc-sync must not read as a clean sync.

doc_cache_sync once returned ``{"entries_synced": 5, "chunks": 37, "errors": 0,
"indexed": {"indexed": false, "returncode": 1}}``: the failure sat one level down, beside
summary fields that claimed success. The server now promotes ``index_error`` to the top
level and lets it clear ``ok``.

vikunja#921 is the other half: once memsearch was retired, *every* sync failed its index
step, and once doc-sync drops that step there are no index keys at all. A clean sync must
then report ``ok: true`` — not fail for want of a field.

These are the generic contract, exercised against fakes so they run everywhere. They used
to drive the live ``~/scripts/doc-sync.py`` with a stubbed memsearch subprocess, which
skipped in CI and tested doc-sync's internals rather than this server; that step is being
removed from doc-sync (memsearch-retirement-finish-2026-09 part 2).
"""

from __future__ import annotations

import pytest

import doc_cache_mcp.server as server

_BASE = {
    "service": "svc",
    "entries_synced": 1,
    "chunks": 3,
    "errors": 0,
    "dry_run": False,
    "results": [{"topic": "overview", "url": "https://x/README.md", "chunks": 3}],
}


class _DocSync:
    def __init__(self, **overrides):
        self.result = {**_BASE, **overrides}

    def sync_service(self, service, *, force=False, dry_run=False):
        return dict(self.result)


@pytest.fixture
def metrics(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(server, "emit_metric", lambda name, tags, fields: seen.append(fields))
    return seen


def _use(monkeypatch, **overrides):
    monkeypatch.setattr(server, "load_doc_sync", lambda: _DocSync(**overrides))


async def test_index_error_is_surfaced_and_clears_ok(monkeypatch, metrics):
    _use(
        monkeypatch,
        indexed={"indexed": False, "returncode": 1},
        index_error="memsearch index exited 1",
        ok=False,
    )
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is False
    assert r["index_error"] == "memsearch index exited 1"
    assert r["errors"] == 0, "errors keeps counting fetch failures only"
    assert metrics[-1]["index_failed"] == 1


async def test_index_error_clears_ok_even_if_doc_sync_says_ok(monkeypatch, metrics):
    """A summary claiming success beside a reported failure is the #372 shape exactly."""
    _use(monkeypatch, index_error="index timed out", ok=True)
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is False


async def test_index_error_clears_a_derived_ok(monkeypatch, metrics):
    _use(monkeypatch, index_error="index timed out")  # no `ok` from doc-sync
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is False


async def test_clean_sync_with_a_passing_index_is_ok(monkeypatch, metrics):
    _use(monkeypatch, indexed={"indexed": True, "returncode": 0}, index_error=None, ok=True)
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is True
    assert r["index_error"] is None
    assert metrics[-1]["index_failed"] == 0


async def test_clean_sync_without_an_index_step_is_ok(monkeypatch, metrics):
    """#921: no index keys at all is a clean sync, not a failure and not a missing `ok`."""
    _use(monkeypatch)  # no ok, no indexed, no index_error
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is True
    assert "index_error" not in r, "an index field was invented for a doc-sync without one"
    assert "indexed" not in r
    assert "index_failed" not in metrics[-1], "constant 0 would read as healthy indexing"


async def test_fetch_errors_without_an_index_step_clear_a_derived_ok(monkeypatch, metrics):
    _use(monkeypatch, errors=1, entries_synced=0, chunks=0)
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is False


async def test_doc_sync_ok_is_passed_through(monkeypatch, metrics):
    """doc-sync owns the definition of `ok`; the server only derives it when absent."""
    _use(monkeypatch, ok=False)
    r = await server.doc_cache_sync("svc")
    assert r["ok"] is False
