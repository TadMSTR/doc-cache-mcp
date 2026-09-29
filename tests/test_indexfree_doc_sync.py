"""doc_cache_sync against a doc-sync.py with no index step — runs in CI, not skipped.

Every other test that loads a real doc-sync.py loads the live ``~/scripts/doc-sync.py`` and
skips wherever it is absent, which includes GitHub CI. So CI stayed green on ``main`` while
the forge-local suite depended on doc-sync's memsearch index step — and would have gone red
the day that step was removed, with nothing in this repo's diff to explain it (vikunja#921).

This loads ``tests/fixtures/doc_sync_indexfree.py`` — the live script with the index step
removed — through the server's real ``load_doc_sync()`` path, with only the network stubbed.
Chunking, writing, state and the lock are the shipped code.

The last test is a forge-local drift guard: the fixture's API must be the live script's API
less the index step, so a doc-sync change that adds or renames something the fixture lacks
fails here instead of leaving the fixture describing a script that no longer exists.
"""

from __future__ import annotations

import importlib.util
import inspect
import shutil
from pathlib import Path

import pytest

import doc_cache_mcp.docsync as docsync
import doc_cache_mcp.server as server
from doc_cache_mcp.vendoring import render_vendored

FIXTURE = Path(__file__).parent / "fixtures" / "doc_sync_indexfree.py"
LIVE = Path.home() / "scripts" / "doc-sync.py"

_MARKDOWN = "# Title\n\n" + "\n\n".join(
    f"## Section {i}\n\n" + ("Body text for a section long enough to be kept. " * 5)
    for i in range(3)
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway $HOME holding the fixture as doc-sync.py beside its allowlist module.

    The script resolves every path (config, cache, state, log) from ``Path.home()`` at
    import time, so pointing HOME at tmp keeps it off the real docs cache entirely.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy(FIXTURE, scripts / "doc-sync.py")
    (scripts / "doc_cache_allowlist.py").write_text(render_vendored(), encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "doc-sync.yml").write_text(
        "services:\n  svc:\n    - topic: overview\n      url: https://x/README.md\n"
    )
    monkeypatch.setenv("DOC_CACHE_MCP_DOCSYNC_PATH", str(scripts / "doc-sync.py"))
    return tmp_path


@pytest.fixture
def ds(home, monkeypatch):
    """The fixture module as the server loads it, with the network stubbed out."""
    m = docsync.load_doc_sync()
    assert m.__file__ == str(home / "scripts" / "doc-sync.py")
    monkeypatch.setattr(m, "safe_fetch", lambda url, max_redirects=5: _MARKDOWN)
    return m


@pytest.fixture
def metrics(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(server, "emit_metric", lambda name, tags, fields: seen.append(fields))
    return seen


def test_fixture_has_no_index_step(home):
    m = _load(home / "scripts" / "doc-sync.py", "_indexfree_probe")
    assert not hasattr(m, "run_memsearch_index")
    assert "index" not in inspect.signature(m.sync_service).parameters


async def test_clean_sync_is_ok(ds, home, metrics):
    r = await server.doc_cache_sync("svc")

    assert r["ok"] is True, r
    assert r["errors"] == 0
    assert r["entries_synced"] == 1
    assert r["chunks"] == 3
    assert "index_error" not in r
    assert "indexed" not in r
    assert "index_failed" not in metrics[-1]
    # The docs really were written by the shipped chunk/write code.
    written = sorted((home / ".claude" / "memory" / "docs" / "svc").glob("overview-*.md"))
    assert len(written) == 3


async def test_fetch_failure_is_not_ok(ds, monkeypatch, metrics):
    def boom(url, max_redirects=5):
        raise OSError("unreachable")

    monkeypatch.setattr(ds, "safe_fetch", boom)
    r = await server.doc_cache_sync("svc")

    assert r["ok"] is False
    assert r["errors"] == 1
    assert "index_error" not in r


async def test_dry_run_is_ok(ds, metrics):
    r = await server.doc_cache_sync("svc", dry_run=True)
    assert r["ok"] is True
    assert r["dry_run"] is True
    assert "index_error" not in r


async def test_unknown_service_is_an_error(ds):
    r = await server.doc_cache_sync("nope")
    assert "Unknown service" in r["error"]


# --- forge-local: keep the fixture honest about the live script ---------------------

#: What the index step contributed to the live script's namespace. Present in live until
#: memsearch-retirement-finish-2026-09 part 2 lands, absent in the fixture.
_INDEX_STEP_NAMES = {
    "run_memsearch_index",
    "MEMSEARCH_BIN",
    "OQP_BASE_URL",
    "_INDEX_FAILURE_CONSEQUENCE",
    "subprocess",
}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _api(m) -> set[str]:
    return {n for n in vars(m) if not n.startswith("__")}


@pytest.mark.skipif(not LIVE.exists(), reason="live doc-sync.py not present (off-forge)")
def test_fixture_matches_live_less_the_index_step(home):
    # `home` for the fixture's allowlist sibling and log dir; LIVE was resolved at import,
    # before HOME moved, so it still names the real script.
    live = _load(LIVE, "_live_doc_sync_drift_probe")
    fixture = _load(home / "scripts" / "doc-sync.py", "_fixture_doc_sync_drift_probe")

    assert _api(live) - _INDEX_STEP_NAMES == _api(fixture), (
        "tests/fixtures/doc_sync_indexfree.py no longer mirrors ~/scripts/doc-sync.py — "
        "re-copy the live script and remove only its index step"
    )
    live_params = set(inspect.signature(live.sync_service).parameters) - {"index"}
    assert live_params == set(inspect.signature(fixture.sync_service).parameters)
