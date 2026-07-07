"""Configuration via environment variables (prefix ``DOC_CACHE_MCP_``).

Deliberately narrow: the transport binding, the path to the shared ``doc-sync.py`` logic
this server imports, the ``doc-sync.yml`` config it edits, and the source-URL allowlist it
enforces. No credentials live here — the server runs as ``ted`` like the other host MCPs
and its only write surface is the docs cache config + cache dir.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOC_CACHE_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Transport binding. Loopback-only by default — this server has a filesystem write
    # surface (the docs cache config) and is never intended to be reachable off-host.
    transport: str = "http"
    host: str = "127.0.0.1"
    port: int = 8503

    # The shared doc-sync logic, imported as the single source of truth for chunk/write.
    # Hyphenated filename outside any package — loaded by path (see docsync.py).
    docsync_path: Path = Path.home() / "scripts" / "doc-sync.py"

    # The docs cache config the add-tool edits (symlink resolved before writing/committing).
    config_path: Path = Path.home() / "docs" / "doc-sync.yml"

    # Source-URL allowlist data file (git-backed, sysadmin-editable). Default-deny.
    allowlist_path: Path = (
        Path.home() / "repos" / "gitea" / "host-forge-scripts" / "doc-cache-allowlist.yml"
    )

    # The shared allowlist *module* (single source of truth, next to doc-sync.py). Both the
    # doc-sync-daily cron (fetch-time) and this server (add-time) import it, so add-time and
    # fetch-time policy can never drift. Hyphen-free name, but loaded by path since it lives
    # outside this package.
    allowlist_module_path: Path = Path.home() / "scripts" / "doc_cache_allowlist.py"

    # Commit doc-sync.yml to git after a successful add. Set false to stage-only / leave
    # commits to a human or the cron (see plan.md open question).
    git_commit: bool = True

    # Max source entries accepted in a single add_service call (abuse ceiling).
    max_entries_per_add: int = 50


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Test hook: drop the cached Settings so the next get_settings() re-reads the env."""
    global _settings
    _settings = None
