"""``__version__`` and pyproject's version must agree.

They did not: `pyproject.toml` said 0.1.1 while `__init__.py` said 0.1.0, and `server.py`
logs `__version__` at startup — so the running server reported a version it was not. Same
"the summary field disagrees with reality" shape as the tickets this build fixed, and it
drifted precisely because nothing checked.

Reads pyproject.toml directly rather than `importlib.metadata.version()`: metadata reflects
whatever was last installed, so on an editable venv it can pass here while the file on disk
says something else — which is the failure mode this test exists to catch.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import doc_cache_mcp

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"

pytestmark = pytest.mark.skipif(
    not PYPROJECT.is_file(), reason="pyproject.toml not present (installed, not a checkout)"
)


def test_version_matches_pyproject():
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert doc_cache_mcp.__version__ == declared, (
        f"__init__.py says {doc_cache_mcp.__version__}, pyproject.toml says {declared} — "
        "the server logs __version__ at startup, so this drift is visible in production logs"
    )
