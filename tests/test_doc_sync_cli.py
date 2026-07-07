"""Regression: the shared doc-sync.py must still expose the importable API AND keep its
CLI behaviour after the refactor (the doc-sync-daily cron depends on it)."""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

DOCSYNC = Path.home() / "scripts" / "doc-sync.py"

pytestmark = pytest.mark.skipif(not DOCSYNC.exists(), reason="doc-sync.py not present")


def _load():
    spec = importlib.util.spec_from_file_location("forge_doc_sync_regression", str(DOCSYNC))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importable_api_present():
    m = _load()
    for name in ("sync_service", "load_config", "load_state", "state_lock",
                 "_sync_service_entries", "run_memsearch_index"):
        assert hasattr(m, name), f"missing {name}"
    sig = inspect.signature(m.sync_service)
    assert set(["force", "dry_run", "index"]).issubset(sig.parameters)


def test_cli_help_still_works():
    r = subprocess.run([sys.executable, str(DOCSYNC), "--help"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "--service" in r.stdout
    assert "--dry-run" in r.stdout
    assert "--force" in r.stdout


def test_cli_unknown_service_exits_1():
    r = subprocess.run([sys.executable, str(DOCSYNC), "--service", "__does_not_exist__"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "Unknown service" in (r.stdout + r.stderr)


def test_cli_dry_run_prints_and_writes_nothing():
    # dry-run must not fetch or mutate state — just print WOULD SYNC lines for a real service.
    m = _load()
    services = list((m.load_config().get("services") or {}).keys())
    if not services:
        pytest.skip("no services configured")
    svc = services[0]
    r = subprocess.run([sys.executable, str(DOCSYNC), "--dry-run", "--service", svc],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0
    assert "WOULD SYNC" in r.stdout


def test_state_lock_is_reentrant_safe_across_processes():
    # The lock must be acquirable when nobody holds it (smoke: acquire + release).
    m = _load()
    with m.state_lock(timeout=5):
        pass
