"""Allowlist shim — re-exports the shared ``doc_cache_allowlist`` module.

The allowlist implementation is the single source of truth living next to ``doc-sync.py``
in host-forge-scripts, so the fetch-time guard (the cron / ``sync_service``) and this
server's add-time guard enforce identical policy with no drift. It is loaded by path (it
lives outside this package). ``from .allowlist import validate_url, load_allowlist,
AllowlistError`` continues to work via module ``__getattr__`` (PEP 562).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from .config import get_settings

_mod: ModuleType | None = None


def _shared() -> ModuleType:
    global _mod
    if _mod is not None:
        return _mod
    # Reuse the module the imported doc-sync may already have loaded under this name, so the
    # add-time and fetch-time guards share one AllowlistError class identity.
    if "doc_cache_allowlist" in sys.modules:
        _mod = sys.modules["doc_cache_allowlist"]
        return _mod
    path = Path(get_settings().allowlist_module_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"shared allowlist module not found at {path}")
    spec = importlib.util.spec_from_file_location("doc_cache_allowlist", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load allowlist module spec from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["doc_cache_allowlist"] = module
    spec.loader.exec_module(module)
    _mod = module
    return module


def __getattr__(name: str):
    # Delegates AllowlistError / validate_url / load_allowlist / … to the shared module.
    return getattr(_shared(), name)


def reset() -> None:
    """Test hook: drop the cached shared module."""
    global _mod
    _mod = None
    sys.modules.pop("doc_cache_allowlist", None)
