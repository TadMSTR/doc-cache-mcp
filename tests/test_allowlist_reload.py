"""vikunja#374 — the sync path must honour an allowlist edit without a restart.

``doc-sync.py::_get_allowlist`` memoised the parsed allowlist for the life of the process.
doc-cache-mcp is a long-lived PM2 daemon, so a sysadmin adding a host had no effect until
somebody restarted the server, and the refusal named the file that already contained the
host. These tests pin both halves: the cache reloads on any change to the file, and a
refusal says which snapshot refused it.

Skipped off-forge, where doc-sync.py is absent.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from doc_cache_mcp.allowlist import AllowlistError, load_allowlist, validate_url

DOCSYNC = Path.home() / "scripts" / "doc-sync.py"

pytestmark = pytest.mark.skipif(not DOCSYNC.exists(), reason="doc-sync.py not present")


def _load_doc_sync(allowlist_file: Path):
    """Import a fresh doc-sync module bound to ``allowlist_file``.

    ALLOWLIST_FILE is resolved from the environment at import time, and each import gets
    its own module object, so every test starts with empty cache globals.
    """
    os.environ["DOC_CACHE_ALLOWLIST_FILE"] = str(allowlist_file)
    spec = importlib.util.spec_from_file_location("_doc_sync_reload_probe", str(DOCSYNC))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def allowlist_file(tmp_path, monkeypatch):
    monkeypatch.setenv("DOC_CACHE_ALLOWLIST_FILE", str(tmp_path / "allow.yml"))
    p = tmp_path / "allow.yml"
    p.write_text("hosts:\n  - aaa.example.com\nforge_endpoints: []\n")
    return p


def test_edit_is_picked_up_without_restart(allowlist_file):
    """The bug, exactly: change the allowlist, and the running process must see it.

    The replacement host is deliberately the same length as the original. The file is then
    rewritten in place, so size and inode are both unchanged and the timestamp is the only
    stat field left — and on this filesystem consecutive writes routinely share one. A
    metadata-keyed cache cannot tell these two versions apart; only the contents differ.
    """
    m = _load_doc_sync(allowlist_file)
    assert "aaa.example.com" in m._get_allowlist()["hosts"]

    allowlist_file.write_text("hosts:\n  - bbb.example.com\nforge_endpoints: []\n")

    hosts = m._get_allowlist()["hosts"]
    assert "bbb.example.com" in hosts, "allowlist edit not picked up — cache is stale"
    assert "aaa.example.com" not in hosts


def test_atomic_replace_edit_is_picked_up(allowlist_file):
    """Write-new-then-rename is how most editors save. It changes the inode, not just mtime."""
    m = _load_doc_sync(allowlist_file)
    assert "aaa.example.com" in m._get_allowlist()["hosts"]

    tmp = allowlist_file.with_suffix(".yml.tmp")
    tmp.write_text("hosts:\n  - ccc.example.com\nforge_endpoints: []\n")
    os.replace(tmp, allowlist_file)

    assert "ccc.example.com" in m._get_allowlist()["hosts"]


def test_unchanged_file_is_not_reparsed(allowlist_file):
    """Caching must still work — the daily cron fetches many URLs per run."""
    m = _load_doc_sync(allowlist_file)
    first = m._get_allowlist()
    assert m._get_allowlist() is first, "allowlist re-parsed despite no change to the file"


def test_deleted_allowlist_fails_closed(allowlist_file):
    """A vanished allowlist must deny, not serve the last good snapshot indefinitely."""
    m = _load_doc_sync(allowlist_file)
    assert m._get_allowlist()["hosts"]

    allowlist_file.unlink()

    # m._allow is the *vendored* module, so its AllowlistError is a distinct class object
    # from the package's — assert against the one the code under test actually raises.
    with pytest.raises(m._allow.AllowlistError) as exc:
        m._get_allowlist()
    assert "refusing all source URLs" in str(exc.value)


def test_reload_retries_after_a_bad_edit(allowlist_file):
    """A malformed save must not latch the cache into a permanently broken state."""
    m = _load_doc_sync(allowlist_file)
    assert "aaa.example.com" in m._get_allowlist()["hosts"]

    allowlist_file.write_text("hosts: [unclosed\n")
    with pytest.raises(m._allow.AllowlistError):
        m._get_allowlist()

    allowlist_file.write_text("hosts:\n  - ddd.example.com\nforge_endpoints: []\n")
    assert "ddd.example.com" in m._get_allowlist()["hosts"]


# --- refusal message names the snapshot (the other half of #374) ---------------------


def test_refusal_names_the_snapshot(tmp_path):
    p = tmp_path / "allow.yml"
    p.write_text("hosts:\n  - docs.example.com\nforge_endpoints: []\n")
    al = load_allowlist(p)

    assert al["source"] == str(p)
    assert al["loaded_at"]

    with pytest.raises(AllowlistError) as exc:
        validate_url("https://other.example.com/x", al)

    msg = str(exc.value)
    assert str(p) in msg, "refusal does not say which file it consulted"
    assert al["loaded_at"] in msg, "refusal does not say when that file was read"
    assert "1 hosts" in msg
    assert "stale snapshot" in msg


def test_refusal_degrades_for_a_handbuilt_allowlist():
    """Callers that assemble an allowlist dict directly still get the old actionable text."""
    al = {"hosts": {"docs.example.com"}, "forge_endpoints": []}
    with pytest.raises(AllowlistError) as exc:
        validate_url("https://other.example.com/x", al)
    assert "Add it to doc-cache-allowlist.yml" in str(exc.value)
