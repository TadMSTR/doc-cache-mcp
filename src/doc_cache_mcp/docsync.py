"""Load the shared ``doc-sync.py`` logic as the single source of truth.

``doc-sync.py`` lives outside any package (hard-linked into ``~/scripts``) and its name
contains a hyphen, so it cannot be imported normally. We load it by path once and cache
the module. The MCP calls its ``sync_service`` / ``load_config`` / ``load_state`` rather
than re-implementing chunking or shelling out to the script.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .config import get_settings

_module: ModuleType | None = None


def load_doc_sync() -> ModuleType:
    """Import and cache the doc-sync module from the configured path."""
    global _module
    if _module is not None:
        return _module

    path = Path(get_settings().docsync_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"doc-sync.py not found at {path}")

    spec = importlib.util.spec_from_file_location("forge_doc_sync", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load doc-sync module spec from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["forge_doc_sync"] = module
    spec.loader.exec_module(module)
    _module = module
    return module


def reset() -> None:
    """Test hook: drop the cached module so the next load re-reads the configured path."""
    global _module
    _module = None
    sys.modules.pop("forge_doc_sync", None)
