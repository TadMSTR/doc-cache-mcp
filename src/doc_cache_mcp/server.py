"""doc-cache-mcp — FastMCP server exposing the forge docs cache as three scoped verbs.

Tool surface (and nothing else — that narrowness is the point):

  doc_cache_list_services   — read-only view of configured services + cache state
  doc_cache_add_service     — register a service + validated source URLs (SSRF guarded)
  doc_cache_sync            — ingest/refresh a configured service into the cache

The server holds no generic file/command primitive. Its only write surface is
``doc-sync.yml`` (structural YAML merge, atomic write, single-file git commit) and the
docs cache dir (via the shared ``doc-sync`` logic it imports). Every source URL is
validated against the allowlist before it can enter the trusted cache (see allowlist.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import time
from pathlib import Path

import structlog
import yaml
from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from . import __version__
from .allowlist import AllowlistError, load_allowlist, validate_url
from .config import get_settings
from .docsync import load_doc_sync
from .observability import configure_logging, emit_metric, init_tracing
from .push import identity_args as push_identity_args
from .push import push_config_commit

configure_logging()
log = structlog.get_logger()
init_tracing()

mcp = FastMCP("doc-cache-mcp")

_SERVICE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Topic goes verbatim into YAML frontmatter and slugged filenames — keep it to safe chars
# so it can never break out of the frontmatter block or the tags list.
_TOPIC_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_URL_LEN = 2048
# How often doc_cache_sync reports progress while the (blocking) sync runs. Some MCP
# clients treat a long gap with no response/progress as a hung call and abort — a service
# with many source URLs is fetched one at a time (30s timeout each), so a heartbeat well
# under any realistic idle-timeout keeps the call alive without changing what actually runs.
_SYNC_HEARTBEAT_INTERVAL_S = 15


class DocEntry(BaseModel):
    """One documentation source: a topic label and the https URL to fetch it from."""

    topic: str = Field(description="Short topic slug, e.g. 'overview' or 'docker-install'.")
    url: str = Field(description="https source URL; must pass the docs-cache allowlist.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_service(service: str) -> str | None:
    if not isinstance(service, str) or not _SERVICE_RE.match(service):
        return f"invalid service name {service!r}: must match {_SERVICE_RE.pattern}"
    return None


def _repo_root(path: Path) -> Path:
    r = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if r.returncode != 0:
        raise RuntimeError(f"{path} is not inside a git repo: {r.stderr.strip()}")
    return Path(r.stdout.strip())


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)  # atomic within the same directory


def _git_commit_config(config_path: Path, service: str) -> dict:
    """Commit exactly ``config_path`` with a fixed-argv git call. Never uses a shell.

    ``service`` is pre-validated against ``_SERVICE_RE`` so it is safe in the message.
    Returns a small status dict; does not raise for a clean tree (nothing to commit).

    Commits as the tool's own identity rather than as the host user (push.py guard 1), so
    tool commits are attributable and the foreign-commit guard has something to match on.
    Passed with ``-c`` per invocation, so this never rewrites the repo's configured
    identity for anyone else working in the same checkout.
    """
    settings = get_settings()
    repo = _repo_root(config_path)
    rel = str(config_path.resolve().relative_to(repo))
    ident = push_identity_args(settings.commit_identity_name, settings.commit_identity_email)
    add = subprocess.run(
        ["git", "-C", str(repo), "add", "--", rel],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if add.returncode != 0:
        # Keep git stderr in the log, not in the caller-facing response (F-03).
        log.error("git_add_failed", stderr=add.stderr.strip())
        return {"committed": False, "error": "git add failed"}
    commit = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            *ident,
            "commit",
            "-m",
            f"doc-cache: add {service}",
            "--",
            rel,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if commit.returncode != 0:
        out = (commit.stdout + commit.stderr).lower()
        if "nothing to commit" in out or "no changes added" in out:
            return {"committed": False, "note": "no changes to commit"}
        log.error("git_commit_failed", stderr=commit.stderr.strip())
        return {"committed": False, "error": "git commit failed"}
    rev = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = {"committed": True, "commit": rev.stdout.strip()}

    # vikunja#363: the commit used to stop here, on local main, permanently — the caller
    # that asked for it structurally cannot push. Finish the operation under guards.
    if not settings.git_push:
        out["pushed"] = False
        out["reason"] = "push disabled (git_push=false)"
        return out
    if settings.deploy_key_path is None:
        log.error("push_misconfigured", reason="git_push enabled with no deploy_key_path")
        out["pushed"] = False
        out["reason"] = "push enabled but no deploy key configured"
        return out

    out.update(
        push_config_commit(
            repo,
            allowed_path=rel,
            remote=settings.push_remote,
            branch=settings.push_branch,
            identity_email=settings.commit_identity_email,
            deploy_key=Path(settings.deploy_key_path).expanduser(),
            review_branch_prefix=settings.review_branch_prefix,
            audit_dir=(
                Path(settings.audit_log_dir).expanduser() if settings.audit_log_dir else None
            ),
            service=service,
        )
    )
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool
def doc_cache_list_services() -> dict:
    """List every service configured in the docs cache with its cache state.

    Read-only. For each service returns its topics + source URLs, the chunk count and
    last-synced date per topic (from doc-sync state), and per-service totals. Call this
    before add/sync to decide what to do.
    """
    ds = load_doc_sync()
    try:
        config = ds.load_config()
    except FileNotFoundError as e:
        log.error("load_config_failed", error=str(e))
        return {"error": "docs cache config not found"}
    state = ds.load_state()

    services = []
    for name, entries in (config.get("services") or {}).items():
        state_by_topic = {s["topic"]: s for s in state.get(name, [])}
        topics = []
        total_chunks = 0
        for e in entries:
            st = state_by_topic.get(e["topic"], {})
            chunks = int(st.get("chunks", 0) or 0)
            total_chunks += chunks
            topics.append(
                {
                    "topic": e["topic"],
                    "url": e["url"],
                    "chunks": chunks,
                    "last_synced": st.get("synced"),
                }
            )
        services.append(
            {
                "service": name,
                "topic_count": len(topics),
                "total_chunks": total_chunks,
                "topics": topics,
            }
        )

    services.sort(key=lambda s: s["service"])
    log.info("doc_cache_list_services", service_count=len(services))
    return {"services": services, "count": len(services)}


@mcp.tool
def doc_cache_add_service(service: str, entries: list[DocEntry]) -> dict:
    """Register a service + its documentation source URLs in the docs cache config.

    Every URL is validated against the docs-cache allowlist (https-only, host allowlist +
    explicit forge endpoints, private/loopback/rebind rejected) BEFORE anything is written.
    The write is a structural YAML merge (dedup by topic), an atomic file replace, and a
    single-file git commit — never a text edit. Does not fetch anything; call
    ``doc_cache_sync`` afterwards to ingest the new sources.

    Args:
        service: Service key, ``^[A-Za-z0-9_-]+$``.
        entries: List of ``{topic, url}``. ``topic`` is ``^[A-Za-z0-9._-]+$``; ``url`` must
                 pass the allowlist. Adding an existing topic replaces its URL (idempotent).
    """
    settings = get_settings()

    err = _validate_service(service)
    if err:
        return {"error": err}
    if not entries:
        return {"error": "entries must be a non-empty list of {topic, url}"}
    if len(entries) > settings.max_entries_per_add:
        return {"error": f"too many entries (max {settings.max_entries_per_add})"}

    # Load the allowlist fresh each call so sysadmin edits take effect without a restart.
    # doc-sync.py's fetch-time guard now does the same, keyed on the allowlist file's stat
    # identity (vikunja#374), so add-time and fetch-time agree on what "current" means.
    try:
        allowlist = load_allowlist(settings.allowlist_path)
    except AllowlistError as e:
        return {"error": f"allowlist unavailable: {e}"}

    # Validate ALL entries before writing anything (all-or-nothing).
    clean: list[dict] = []
    seen_topics: set[str] = set()
    for ent in entries:
        topic = (ent.topic or "").strip()
        url = (ent.url or "").strip()
        if not _TOPIC_RE.match(topic):
            return {"error": f"invalid topic {topic!r}: must match {_TOPIC_RE.pattern}"}
        if topic in seen_topics:
            return {"error": f"duplicate topic {topic!r} in this request"}
        if len(url) > _MAX_URL_LEN:
            return {"error": f"url for topic {topic!r} exceeds {_MAX_URL_LEN} chars"}
        try:
            validate_url(url, allowlist)
        except AllowlistError as e:
            return {"error": f"topic {topic!r}: {e}"}
        seen_topics.add(topic)
        clean.append({"topic": topic, "url": url})

    # Structural merge: safe_load -> mutate -> safe_dump. Dedup by topic, preserve order.
    real = Path(settings.config_path).resolve()
    try:
        config = yaml.safe_load(real.read_text()) or {}
    except (FileNotFoundError, yaml.YAMLError) as e:
        log.error("config_read_failed", error=str(e))
        return {"error": "cannot read docs cache config"}
    if not isinstance(config, dict):
        return {"error": "doc-sync.yml is not a YAML mapping"}

    services = config.setdefault("services", {})
    if not isinstance(services, dict):
        return {"error": "doc-sync.yml 'services' is not a mapping"}

    block = services.get(service) or []
    by_topic = {e["topic"]: dict(e) for e in block if isinstance(e, dict) and "topic" in e}
    for ent in clean:
        by_topic[ent["topic"]] = ent
    merged = list(by_topic.values())
    services[service] = merged

    text = yaml.safe_dump(
        config,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=1000,
    )
    _atomic_write(real, text)

    commit = {"committed": False, "note": "git_commit disabled"}
    if settings.git_commit:
        try:
            commit = _git_commit_config(real, service)
        except Exception as e:
            log.error("git_commit_error", error=str(e))
            commit = {"committed": False, "error": "git commit error"}

    log.info(
        "doc_cache_add_service",
        service=service,
        added=len(clean),
        total_topics=len(merged),
        committed=commit.get("committed"),
    )
    emit_metric(
        "doc_cache_tool",
        {"tool": "add_service"},
        {"added": len(clean), "total_topics": len(merged)},
    )
    return {
        "service": service,
        "entries": merged,
        "added": len(clean),
        "commit": commit,
    }


async def _heartbeat(ctx: Context | None, service: str) -> None:
    """Emit a progress notification every _SYNC_HEARTBEAT_INTERVAL_S while a sync runs.

    A no-op if the caller's client didn't send a progressToken on the request (FastMCP
    silently drops report_progress calls in that case — see fastmcp/docs/servers/progress).
    Cancelled by the caller once the sync completes; runs forever otherwise, so it must
    never be awaited to completion.
    """
    if ctx is None:
        return
    elapsed = 0
    while True:
        await asyncio.sleep(_SYNC_HEARTBEAT_INTERVAL_S)
        elapsed += _SYNC_HEARTBEAT_INTERVAL_S
        await ctx.report_progress(
            progress=elapsed,
            total=None,
            message=f"syncing {service}: {elapsed}s elapsed — fetching sources",
        )


@mcp.tool
async def doc_cache_sync(service: str, dry_run: bool = False, ctx: Context | None = None) -> dict:
    """Ingest / refresh a configured service into the docs cache.

    Fetches each of the service's source URLs, converts + chunks them, writes the chunks
    to the cache and updates state. The service must already exist in the config — add it
    first with ``doc_cache_add_service``.

    The new docs are cached immediately and become searchable in qmd's ``docs`` collection
    after the next hourly ``qmd-refresh`` (at :00). This tool does not index anything.

    Progress notifications are sent every 15s while a sync runs so MCP clients don't treat
    a slow fetch as a hung call; raise your client-side timeout if yours doesn't support
    progress notifications.

    Check ``ok`` before trusting a sync. It is false if any source failed to fetch
    (``errors`` counts them). If the underlying doc-sync still reports an index step,
    an ``index_error`` from it is surfaced at the top level and also makes ``ok`` false.

    Args:
        service: Service key to sync (must exist in doc-sync.yml).
        dry_run: If true, report what would be synced without fetching or writing.
    """
    err = _validate_service(service)
    if err:
        return {"error": err}

    ds = load_doc_sync()
    t0 = time.perf_counter()
    heartbeat = asyncio.create_task(_heartbeat(ctx, service))
    try:
        # sync_service (fetch + chunk + write) is synchronous doc-sync logic shared with
        # the CLI cron — run it off the event loop so the heartbeat task above can actually
        # keep ticking while it's in flight.
        result = await asyncio.to_thread(ds.sync_service, service, dry_run=dry_run)
    except ValueError as e:  # unknown service — safe, useful message (no paths)
        return {"error": str(e)}
    except FileNotFoundError as e:
        log.error("sync_config_missing", error=str(e))
        return {"error": "docs cache config or sync module not found"}
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
    duration = round(time.perf_counter() - t0, 3)

    result = _normalise_sync_result(result)
    index_error = result.get("index_error")
    log_event = log.error if index_error or not result["ok"] else log.info
    log_event(
        "doc_cache_sync",
        service=service,
        dry_run=dry_run,
        ok=result["ok"],
        entries_synced=result.get("entries_synced"),
        chunks=result.get("chunks"),
        errors=result.get("errors"),
        index_error=index_error,
        duration_s=duration,
    )
    fields = {
        "entries_synced": result.get("entries_synced", 0),
        "chunks": result.get("chunks", 0),
        "errors": result.get("errors", 0),
        "duration_s": duration,
    }
    # Only a doc-sync that still has an index step can report on it. Emitting a constant
    # 0 after that step is gone would read as "indexing is healthy" on a dashboard.
    if _reports_index(result):
        fields["index_failed"] = 1 if index_error else 0
    emit_metric("doc_cache_tool", {"tool": "sync", "service": service}, fields)
    result["duration_s"] = duration
    return result


def _reports_index(result: dict) -> bool:
    return "indexed" in result or "index_error" in result


def _normalise_sync_result(result: dict) -> dict:
    """Make ``ok`` trustworthy whether or not doc-sync still runs an index step.

    doc-sync.py's memsearch index step is being removed (memsearch-retirement-finish-2026-09
    part 2; qmd-refresh indexes the docs cache hourly). Until then its result carries
    ``indexed`` / ``index_error``; afterwards neither key exists. Both shapes must report
    a clean sync as ``ok: true`` (vikunja#921).

    * ``ok`` from doc-sync is passed through when present. Only when it is missing is it
      derived: no fetch errors and no ``index_error``.
    * ``index_error``, when doc-sync reports one, still makes ``ok`` false (vikunja#372):
      a failure one level down must not sit beside a summary that claims success.
    * No index keys are invented for a doc-sync that doesn't report them.
    """
    index_error = result.get("index_error")
    ok = result.get("ok")
    if ok is None:
        ok = not result.get("errors") and not index_error
    result["ok"] = bool(ok) and not index_error
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    cfg = get_settings()
    log.info(
        "doc_cache_mcp_start",
        version=__version__,
        transport=cfg.transport,
        host=cfg.host,
        port=cfg.port,
    )
    if cfg.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport=cfg.transport, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
