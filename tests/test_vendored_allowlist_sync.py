"""Drift guard — the forge vendored copy of the allowlist must match a fresh regeneration.

``doc-sync.py`` runs in a separate venv and imports
``host-forge-scripts/scripts/doc_cache_allowlist.py``, a vendored copy of
``doc_cache_mcp/allowlist.py``. If they drift, add-time and fetch-time policy diverge —
exactly the failure this single-source design prevents.

The copy is generated (see :mod:`doc_cache_mcp.vendoring`), so this asserts it equals
``render_vendored()`` rather than asserting byte-equality with the source. The previous
hard-link mechanism drifted twice without anything noticing; an atomic-replace write severs
a hard link silently. Skipped off-forge (e.g. in CI) where the vendored copy is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_cache_mcp.vendoring import DEFAULT_VENDORED_PATH, render_vendored, write_vendored

VENDORED = DEFAULT_VENDORED_PATH

pytestmark = pytest.mark.skipif(
    not VENDORED.exists(),
    reason="forge vendored allowlist copy not present (off-forge)",
)


def test_vendored_copy_matches_regeneration():
    assert VENDORED.read_text(encoding="utf-8") == render_vendored(), (
        f"{VENDORED} is out of date — regenerate it with "
        "`python -m doc_cache_mcp.vendoring --write` and commit the result to "
        "host-forge/scripts, so the cron's fetch-time guard matches the server's "
        "add-time guard. Do not hand-edit the vendored copy."
    )


def test_vendored_copy_is_marked_generated():
    """A reader who opens the vendored file must be told not to edit it.

    The drift test above only fails *after* someone has edited the wrong file; this is the
    part that stops them.
    """
    head = VENDORED.read_text(encoding="utf-8")[:1500]
    assert "GENERATED FILE — DO NOT EDIT" in head
    assert "doc_cache_mcp.vendoring --write" in head


def test_vendored_copy_still_imports_standalone():
    """The vendored copy must load as a plain top-level module, the way doc-sync.py loads it.

    doc-sync.py does ``sys.path.insert(0, <its dir>); import doc_cache_allowlist`` — no
    package context. A regeneration that introduced a relative import or a package-only
    dependency would pass a text comparison and still break the cron at 03:00.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("_vendored_allowlist_probe", VENDORED)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert hasattr(module, "validate_url")
    assert hasattr(module, "load_allowlist")
    assert issubclass(module.AllowlistError, ValueError)


def test_write_vendored_is_idempotent(tmp_path: Path):
    """A no-op regeneration must report unchanged and leave the file alone.

    Guards the cheap-rerun property: `--write` is safe to run unconditionally, and a
    regeneration that changed nothing must not show up as a diff in host-forge/scripts.
    """
    dest = tmp_path / "doc_cache_allowlist.py"

    path, changed = write_vendored(dest)
    assert path == dest
    assert changed is True

    mtime = dest.stat().st_mtime_ns
    _, changed_again = write_vendored(dest)
    assert changed_again is False
    assert dest.stat().st_mtime_ns == mtime, "no-op regeneration rewrote the file"


def test_drift_is_detected(tmp_path: Path):
    """The guard actually fails on drift — the property the hard link silently lost."""
    dest = tmp_path / "doc_cache_allowlist.py"
    write_vendored(dest)

    dest.write_text(
        dest.read_text(encoding="utf-8").replace(
            'raise AllowlistError(f"source url has no host: {url!r}")',
            "pass  # policy divergence",
        ),
        encoding="utf-8",
    )
    assert dest.read_text(encoding="utf-8") != render_vendored()
