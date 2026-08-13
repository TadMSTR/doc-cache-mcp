"""Generate the vendored copy of :mod:`doc_cache_mcp.allowlist` that ``doc-sync.py`` imports.

``doc-sync.py`` runs from its own venv (``/opt/venvs/doc-sync``) and imports the allowlist
as a plain top-level module sitting next to it, so a copy of the policy has to exist in
``host-forge-scripts/scripts/``. Keeping that copy correct is a real problem: the two files
live in different git repos and either one can be opened in an ordinary editor.

**This used to be a hard link, and that failed silently — twice.** Any atomic-replace write
(write-new-then-rename, which is what most editors and formatters do) severs a hard link
without warning, after which the two files drift with nothing to notice. The link is not
recoverable by the thing that broke it, and a severed link looks exactly like a working one
until you stat the inodes.

So the copy is *generated* instead. The generated file carries a header saying so, and
``tests/test_vendored_allowlist_sync.py`` compares the on-disk copy against a fresh
regeneration rather than against a stored byte-copy. Drift is then a test failure with an
obvious fix, and editing the wrong file is caught rather than silently honoured.

Regenerate after any change to ``allowlist.py``::

    python -m doc_cache_mcp.vendoring --write

The vendored copy lives in a different repo (``host-forge/scripts``) — commit it there.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

#: Default destination. ``~/scripts`` is a symlink into
#: ``~/repos/gitea/host-forge-scripts/scripts``, so writing here writes into that repo.
DEFAULT_VENDORED_PATH = Path.home() / "scripts" / "doc_cache_allowlist.py"

#: Prepended to the generated copy. Comments before the module docstring do not displace
#: it — ``allowlist.__doc__`` is unchanged in the vendored module.
_HEADER = """\
# ======================================================================================
# GENERATED FILE — DO NOT EDIT.
#
# Vendored copy of doc_cache_mcp/allowlist.py, so doc-sync.py can enforce the same
# source-URL allowlist policy at fetch time that doc-cache-mcp enforces at add time.
#
#   Edit instead:  doc-cache-mcp/src/doc_cache_mcp/allowlist.py
#   Regenerate:    python -m doc_cache_mcp.vendoring --write
#
# doc-cache-mcp's tests/test_vendored_allowlist_sync.py regenerates this file in memory
# and fails if the result differs from what is on disk. An edit made here rather than to
# the source will be reported as drift, not silently kept.
#
# This was previously kept in sync by a hard link. That is not a workable mechanism:
# atomic-replace writes sever hard links silently, and it drifted twice before this
# generator replaced it.
# ======================================================================================

"""


def source_path() -> Path:
    """Absolute path to the in-package allowlist module — the single source of truth."""
    from . import allowlist

    return Path(allowlist.__file__)


def render_vendored(source_text: str | None = None) -> str:
    """Return the exact text the vendored copy should contain.

    Both the writer and the drift test go through here, so there is one definition of
    "correct" rather than two that can disagree.
    """
    if source_text is None:
        source_text = source_path().read_text(encoding="utf-8")
    return _HEADER + source_text


def write_vendored(dest: Path | None = None) -> tuple[Path, bool]:
    """Write the vendored copy. Returns ``(path, changed)``.

    Writes only when the content differs, so a no-op regeneration leaves the mtime alone
    and does not look like a change to git or to the daily cron.
    """
    dest = dest or DEFAULT_VENDORED_PATH
    rendered = render_vendored()
    if dest.exists() and dest.read_text(encoding="utf-8") == rendered:
        return dest, False
    dest.write_text(rendered, encoding="utf-8")
    return dest, True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m doc_cache_mcp.vendoring",
        description="Generate the vendored doc_cache_allowlist.py that doc-sync.py imports.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the file; without this, only report whether it is up to date",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_VENDORED_PATH,
        help=f"destination path (default: {DEFAULT_VENDORED_PATH})",
    )
    args = parser.parse_args(argv)

    if not args.write:
        rendered = render_vendored()
        if args.output.exists() and args.output.read_text(encoding="utf-8") == rendered:
            print(f"up to date: {args.output}")
            return 0
        print(f"OUT OF DATE: {args.output} — rerun with --write")
        return 1

    dest, changed = write_vendored(args.output)
    print(f"{'wrote' if changed else 'unchanged'}: {dest}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
