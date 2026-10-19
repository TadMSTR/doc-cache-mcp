"""Configuration via environment variables (prefix ``DOC_CACHE_MCP_``).

Deliberately narrow: the transport binding, the path to the shared ``doc-sync.py`` logic
this server imports, the ``doc-sync.yml`` config it edits, and the source-URL allowlist it
enforces. No credentials live here — the server runs as ``ted`` like the other host MCPs
and its only write surface is the docs cache config + cache dirs.
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

    # Commit doc-sync.yml to git after a successful add. Set false to stage-only / leave
    # commits to a human or the cron (see plan.md open question).
    git_commit: bool = True

    # Push that commit. OFF by default: pushing to a shared branch unattended is a real
    # privilege and should be opted into per deployment, not inherited by anyone who pip
    # installs this. Requires deploy_key_path — see push.py for the guards.
    git_push: bool = False

    # Dedicated ssh key, write-scoped to the target repo alone. Used with
    # IdentitiesOnly=yes so ssh cannot fall back to the host user's ambient key, which
    # would reach every repo that key can reach. Mode 0600, owned by the service user.
    deploy_key_path: Path | None = None

    push_remote: str = "origin"
    push_branch: str = "main"

    # Identity for tool commits. Distinct from the host user so tool commits are
    # attributable, and so the foreign-commit guard has something to match on.
    commit_identity_name: str = "doc-cache-mcp"
    commit_identity_email: str = "doc-cache-mcp@forge"

    # Optional append-only JSONL audit sink for push decisions (agent-bus schema), giving
    # a trail outside git that survives a force-push. Unset = no-op.
    audit_log_dir: Path | None = None

    # Max source entries accepted in a single add_service call (abuse ceiling).
    max_entries_per_add: int = 50

    # Max topics removed by a single remove_topic call.
    max_remove_per_call: int = 20

    # Where chunks from provisional (unvetted-host) sources are written. Deliberately
    # OUTSIDE ~/.claude/memory: qmd's agent-memory collection and the memory pipeline walk
    # that whole tree, so a directory there would be served and promoted as agent memory.
    # doc-sync.py reads the same env var with the same default; load_doc_sync() also hands
    # it this value, so a setting from .env (which never reaches os.environ) still agrees.
    provisional_cache_dir: Path = Path.home() / ".claude" / "docs-provisional"


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
