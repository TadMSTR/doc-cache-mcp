"""Drift guard — the forge vendored copy of the allowlist must stay byte-identical to the
in-package source of truth.

``doc-sync.py`` runs in a separate venv and imports
``host-forge-scripts/scripts/doc_cache_allowlist.py`` (hard-linked into ``~/scripts``). That
file is a vendored copy of ``doc_cache_mcp/allowlist.py``. If they drift, add-time and
fetch-time policy diverge — exactly the failure this single-source design prevents. Skipped
off-forge (e.g. in CI) where the vendored copy is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import doc_cache_mcp.allowlist as pkg_allowlist

VENDORED = Path.home() / "scripts" / "doc_cache_allowlist.py"

pytestmark = pytest.mark.skipif(
    not VENDORED.exists(),
    reason="forge vendored allowlist copy not present (off-forge)",
)


def test_vendored_copy_is_byte_identical():
    pkg_file = Path(pkg_allowlist.__file__)
    assert VENDORED.read_text() == pkg_file.read_text(), (
        f"{VENDORED} has drifted from {pkg_file} — re-vendor the allowlist so the cron's "
        "fetch-time guard matches the server's add-time guard."
    )
