"""vikunja#372 part two — a failed memsearch index must not be reported as a clean sync.

doc_cache_sync returned ``{"entries_synced": 5, "chunks": 37, "errors": 0,
"indexed": {"indexed": false, "returncode": 1}}``. The docs were fetched and written but
never indexed, so they were on disk and unsearchable — and ``errors: 0`` beside a healthy
chunk count reads as success. The failure was one level down, in a field callers do not
routinely inspect, and in a WARNING log line.

These exercise the real doc-sync.py, with only the network (``sync_entry``) and the
memsearch subprocess stubbed, so the reporting path under test is the shipped one.

Skipped off-forge, where doc-sync.py is absent.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

DOCSYNC = Path.home() / "scripts" / "doc-sync.py"

pytestmark = pytest.mark.skipif(not DOCSYNC.exists(), reason="doc-sync.py not present")


def _load():
    spec = importlib.util.spec_from_file_location("_doc_sync_index_probe", str(DOCSYNC))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ds(tmp_path, monkeypatch):
    """A doc-sync module with every path pointed at tmp and no network."""
    m = _load()

    config = tmp_path / "doc-sync.yml"
    config.write_text("services:\n  svc:\n    - topic: overview\n      url: https://x/README.md\n")
    state = tmp_path / "state.json"

    monkeypatch.setattr(m, "CONFIG_FILE", config)
    monkeypatch.setattr(m, "STATE_FILE", state)
    monkeypatch.setattr(m, "LOCK_FILE", tmp_path / "state.json.lock")
    monkeypatch.setattr(m, "MANIFEST", tmp_path / "manifest.md")
    monkeypatch.setattr(m, "CACHE_DIR", tmp_path / "cache")
    # No network: pretend each entry fetched and produced 3 chunks.
    monkeypatch.setattr(m, "sync_entry", lambda service, entry: 3)
    return m


def _stub_memsearch(m, monkeypatch, *, returncode: int, stderr: str = ""):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(m.subprocess, "run", fake_run)


def test_index_failure_is_not_reported_as_success(ds, monkeypatch):
    _stub_memsearch(ds, monkeypatch, returncode=1, stderr="boom")

    r = ds.sync_service("svc")

    assert r["ok"] is False, "sync reported success despite a failed index"
    assert r["index_error"], "no top-level index_error"
    assert "NOT searchable" in r["index_error"]
    assert r["indexed"]["returncode"] == 1

    # The docs really were written — errors keeps counting only fetch/convert failures.
    assert r["entries_synced"] == 1
    assert r["chunks"] == 3
    assert r["errors"] == 0


def test_clean_run_reports_ok(ds, monkeypatch):
    _stub_memsearch(ds, monkeypatch, returncode=0)

    r = ds.sync_service("svc")

    assert r["ok"] is True
    assert r["index_error"] is None
    assert r["indexed"]["indexed"] is True


def test_entry_failure_also_clears_ok(ds, monkeypatch):
    _stub_memsearch(ds, monkeypatch, returncode=0)
    monkeypatch.setattr(ds, "sync_entry", lambda service, entry: -1)  # fetch failed

    r = ds.sync_service("svc")

    assert r["ok"] is False
    assert r["errors"] == 1
    # Nothing synced, so indexing is skipped entirely — that is not an index failure.
    assert r["index_error"] is None


def test_index_timeout_surfaces_too(ds, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 900)

    monkeypatch.setattr(ds.subprocess, "run", fake_run)

    r = ds.run_memsearch_index(alert_on_timeout=False)
    assert r["timed_out"] is True
    assert r["indexed"] is False
    assert "timed out" in r["error"]
    assert "NOT searchable" in r["error"]


def test_dry_run_reports_ok(ds):
    r = ds.sync_service("svc", dry_run=True)
    assert r["ok"] is True
    assert r["index_error"] is None
    assert r["dry_run"] is True


def test_stderr_stays_out_of_the_caller_facing_message(ds, monkeypatch):
    """memsearch's stderr can carry local paths — it belongs in the log, not the response."""
    _stub_memsearch(ds, monkeypatch, returncode=1, stderr="/home/ted/secret/path exploded")

    r = ds.sync_service("svc")
    assert "/home/ted/secret/path" not in r["index_error"]


# --- the CLI half: doc-sync-daily exited 0 after indexing nothing ---------------------


def _run_cli(ds, monkeypatch, *, index_result):
    """Drive main() with the fetch and index steps stubbed. Returns the exit code."""
    import contextlib
    import sys

    monkeypatch.setattr(sys, "argv", ["doc-sync.py"])  # full run, no --service
    monkeypatch.setattr(ds, "state_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(ds, "load_state", lambda: {})
    monkeypatch.setattr(ds, "save_state", lambda state: None)
    monkeypatch.setattr(ds, "write_manifest", lambda state: None)
    monkeypatch.setattr(ds, "run_memsearch_index", lambda *a, **k: index_result)

    try:
        ds.main()
    except SystemExit as e:
        return e.code or 0
    return 0


def test_cli_exits_nonzero_when_the_index_fails(ds, monkeypatch):
    """The 03:00 cron discarded run_memsearch_index()'s result and exited 0 regardless.

    Everything got fetched, nothing got indexed, and the run looked clean — the same
    failure as the MCP path, in the scheduled job nobody watches.
    """
    code = _run_cli(
        ds,
        monkeypatch,
        index_result={
            "indexed": False,
            "returncode": 1,
            "timed_out": False,
            "error": "memsearch index exited 1 — docs are cached but NOT searchable",
        },
    )
    assert code == 1, "doc-sync-daily reported success after failing to index"


def test_cli_exits_zero_on_a_clean_run(ds, monkeypatch):
    code = _run_cli(
        ds,
        monkeypatch,
        index_result={"indexed": True, "returncode": 0, "timed_out": False, "error": None},
    )
    assert code == 0
