"""doc-cache-mcp — FastMCP server exposing the forge docs cache as a few scoped verbs.

Tool surface (and nothing else — that narrowness is the point):

  doc_cache_list_services   — health summary, or per-topic detail, of the docs cache
  doc_cache_add_service     — register new topics (strictly additive; allowlist-classified)
  doc_cache_update_topic    — repoint one existing topic at a new URL
  doc_cache_remove_topic    — remove topics (and their cached chunks)
  doc_cache_sync            — ingest/refresh a configured service into the cache

The server holds no generic file/command primitive. Its only write surface is
``doc-sync.yml`` (structural YAML edit, atomic write, single-file git commit) and the docs
cache dirs (via the shared ``doc-sync`` logic it imports). Every source URL is classified
by the allowlist before it is written (see allowlist.py), and every config change goes
through push.py's finish-or-fail sequence: each call returns an ``outcome``.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import subprocess
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import structlog
import yaml
from fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

from . import __version__
from .allowlist import PROVISIONAL, AllowlistError, classify_url, host_tier, load_allowlist
from .config import get_settings
from .docsync import load_doc_sync
from .observability import configure_logging, emit_metric, init_tracing
from .push import (
    COMMITTED_LOCAL,
    PUSHED,
    REFUSED,
    UNCHANGED,
    WRITTEN,
    GitTarget,
    _atomic_write,
    commit_change,
    emit_audit_event,
    precondition,
)

configure_logging()
log = structlog.get_logger()
init_tracing()

mcp = FastMCP("doc-cache-mcp")

# fullmatch, not match: `$` also matches before a trailing newline, so "svc\n" passed.
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

#: A topic whose last success is older than this is ``stale`` in the listing.
_STALE_DAYS = 3
#: Ceilings on the default (summary) listing, which must stay well under the MCP result
#: limit at forge's ~100 services: the full per-topic listing was 56.6 KB and spilled.
_SUMMARY_FAILING_MAX = 25
_SUMMARY_ERROR_CHARS = 100
#: Worst first. A service's status in the summary is its worst topic's.
_STATUS_ORDER = ("failing", "expired_provisional", "never_synced", "stale", "ok")

#: One config change at a time in this process: precondition → read → write → commit →
#: push is a single sequence, and two interleaved calls would each see the other's commit.
_WRITE_LOCK = threading.Lock()


class DocEntry(BaseModel):
    """One documentation source: a topic label and the https URL to fetch it from."""

    topic: str = Field(description="Short topic slug, e.g. 'overview' or 'docker-install'.")
    url: str = Field(description="https source URL; must pass the docs-cache allowlist.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refused(reason: str) -> dict:
    """A call that wrote nothing. ``error`` is kept beside ``reason`` for older callers."""
    return {"outcome": REFUSED, "reason": reason, "error": reason}


def _validate_service(service: str) -> str | None:
    if not isinstance(service, str) or not _SERVICE_RE.fullmatch(service):
        return f"invalid service name {service!r}: must match {_SERVICE_RE.pattern}"
    return None


def _validate_topic(topic: str) -> str | None:
    if not isinstance(topic, str) or not _TOPIC_RE.fullmatch(topic):
        return f"invalid topic {topic!r}: must match {_TOPIC_RE.pattern}"
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


def _git_target(config_path: Path) -> GitTarget:
    settings = get_settings()
    repo = _repo_root(config_path)
    return GitTarget(
        repo=repo,
        rel=str(config_path.relative_to(repo)),
        remote=settings.push_remote,
        branch=settings.push_branch,
        identity_name=settings.commit_identity_name,
        identity_email=settings.commit_identity_email,
        push=settings.git_push,
        deploy_key=(
            Path(settings.deploy_key_path).expanduser() if settings.deploy_key_path else None
        ),
        audit_dir=Path(settings.audit_log_dir).expanduser() if settings.audit_log_dir else None,
    )


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _dump(config: dict) -> str:
    return yaml.safe_dump(
        config, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000
    )


def _classify(url: str, allowlist: dict, topic: str) -> tuple[str | None, str | None]:
    """``(trust, None)`` or ``(None, error)`` for one candidate URL."""
    url = (url or "").strip()
    if len(url) > _MAX_URL_LEN:
        return None, f"url for topic {topic!r} exceeds {_MAX_URL_LEN} chars"
    try:
        return classify_url(url, allowlist).trust, None
    except AllowlistError as e:
        return None, f"topic {topic!r}: {e}"


def _source_entry(topic: str, url: str, trust: str, added: str | None = None) -> dict:
    """The doc-sync.yml entry. Trusted entries keep the v0.2 two-key shape exactly.

    ``trust``/``added`` are informational: doc-sync recomputes trust from the allowlist on
    every fetch, and reads ``added`` only for a topic it classifies provisional (TTL).
    """
    entry = {"topic": topic, "url": url}
    if trust == PROVISIONAL:
        entry["trust"] = PROVISIONAL
        entry["added"] = added or date.today().isoformat()
    return entry


def _provisional_cap(config: dict, new_hosts: set[str], allowlist: dict) -> str | None:
    """Refusal if adding ``new_hosts`` would take the provisional tier past ``max_hosts``."""
    cap = allowlist["provisional"]["max_hosts"]
    current = {
        _host(e.get("url", ""))
        for entries in (config.get("services") or {}).values()
        for e in entries or []
        if isinstance(e, dict) and host_tier(_host(e.get("url", "")), allowlist) == PROVISIONAL
    }
    added = new_hosts - current
    if added and len(current | added) > cap:
        return (
            f"provisional.max_hosts is {cap} and {len(current)} provisional host(s) are "
            f"already configured; adding {sorted(added)} would exceed it. Promote or remove "
            "a provisional host first."
        )
    return None


def _duplicate_url_warnings(block: list[dict], topic: str, url: str) -> list[str]:
    return [
        f"url for {topic!r} is already registered under topic {e['topic']!r} in this "
        "service; both will index the same content"
        for e in block
        if e.get("url") == url and e.get("topic") != topic
    ]


def _write_config(
    mutate,
    *,
    service: str,
    message: str,
    expected: str,
) -> dict:
    """Run one config change through the finish-or-fail sequence (push.py), under the lock.

    ``mutate(config) -> dict`` edits the loaded config in place and returns tool-specific
    result fields; if it returns ``{"outcome": ...}`` the change stops there (a refusal, or
    ``unchanged``) and nothing is written.
    """
    settings = get_settings()
    real = Path(settings.config_path).resolve()
    with _WRITE_LOCK:
        target = None
        if settings.git_commit:
            if settings.git_push and settings.deploy_key_path is None:
                log.error("push_misconfigured", reason="git_push enabled with no deploy_key_path")
                return _refused("push enabled but no deploy key configured")
            try:
                target = _git_target(real)
            except (RuntimeError, ValueError, subprocess.SubprocessError, OSError) as e:
                log.error("git_target_failed", error=str(e))
                return _refused("docs cache config is not inside a usable git checkout")
            reason = precondition(target)
            if reason:
                return _refused(reason)

        try:
            config = yaml.safe_load(real.read_text()) or {}
        except (FileNotFoundError, yaml.YAMLError) as e:
            log.error("config_read_failed", error=str(e))
            return _refused("cannot read docs cache config")
        if not isinstance(config, dict):
            return _refused("doc-sync.yml is not a YAML mapping")
        if not isinstance(config.setdefault("services", {}), dict):
            return _refused("doc-sync.yml 'services' is not a mapping")

        fields = mutate(config)
        if "outcome" in fields:
            return fields

        text = _dump(config)
        if target is None:
            _atomic_write(real, text)
            result = {"outcome": WRITTEN, "sha": None, "change": expected, "reason": None}
        else:
            result = commit_change(
                target, new_text=text, message=message, expected=expected, service=service
            )
    return {**fields, **result}


def _landed(result: dict) -> bool:
    return result.get("outcome") in (PUSHED, COMMITTED_LOCAL, WRITTEN)


def _announce_provisional(service: str, topics: list[str], hosts: set[str], result: dict) -> None:
    """``doc-cache.provisional.added`` — Ted's promotion queue, with no new UI."""
    if not topics or not _landed(result):
        return
    settings = get_settings()
    audit_dir = Path(settings.audit_log_dir).expanduser() if settings.audit_log_dir else None
    err = emit_audit_event(
        audit_dir,
        f"provisional docs source(s) added for {service!r}: {', '.join(sorted(hosts))}",
        {"service": service, "topics": topics, "hosts": sorted(hosts), "sha": result.get("sha")},
        event="doc-cache.provisional.added",
    )
    if err:
        result["audit_error"] = err


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def _topic_status(st: dict | None, today: date) -> str:
    if not st:
        return "never_synced"
    if st.get("expired"):
        return "expired_provisional"
    if int(st.get("consecutive_failures", 0) or 0) > 0:
        return "failing"
    synced = st.get("synced")
    if not synced:
        return "never_synced"
    try:
        age = (today - date.fromisoformat(str(synced))).days
    except ValueError:
        return "stale"
    return "stale" if age > _STALE_DAYS else "ok"


def _trust_label(url: str, allowlist: dict | None, recorded: str | None = None) -> str:
    """The tier the chunks were written under, if doc-sync recorded one; else the tier the
    configured URL's host would get. ``recorded`` wins because it is what is on disk."""
    if recorded in ("trusted", "provisional"):
        return recorded
    if allowlist is None:
        return "unknown"
    return host_tier(_host(url), allowlist) or "refused"


@mcp.tool
def doc_cache_list_services(service: str | None = None, detail: bool = False) -> dict:
    """Health of the docs cache. Call this before add/update/remove/sync.

    By default a compact **summary**: ``services`` maps each service name to its topic
    count, chunk total and worst status, plus ``health`` (topic counts by status) and
    ``failing`` (each failing ``service/topic`` with its last error and consecutive failure
    count).

    Pass ``service`` or ``detail=true`` for per-topic rows: ``topic``, ``url``, ``status``,
    ``chunks``, ``last_success``, ``last_error``, ``consecutive_failures``, ``trust``.

    Statuses: ``ok``; ``stale`` (last success over 3 days ago); ``failing`` (the last sync
    of it failed); ``never_synced``; ``expired_provisional`` (unvetted source past its TTL,
    no longer synced — promote its host or remove it).

    Args:
        service: Only this service (implies per-topic detail).
        detail: Per-topic rows for every service. Large: ~90 KB on forge; prefer ``service``.
    """
    if service is not None:
        err = _validate_service(service)
        if err:
            return _refused(err)
    ds = load_doc_sync()
    try:
        config = ds.load_config()
    except FileNotFoundError as e:
        log.error("load_config_failed", error=str(e))
        return _refused("docs cache config not found")
    state = ds.load_state()
    try:
        allowlist = load_allowlist(get_settings().allowlist_path)
    except AllowlistError:
        allowlist = None

    configured = config.get("services") or {}
    if service is not None and service not in configured:
        return _refused(f"unknown service {service!r}")
    names = sorted([service] if service is not None else configured)
    today = date.today()

    health = dict.fromkeys(_STATUS_ORDER, 0)
    failing: list[dict] = []
    services = []
    for name in names:
        state_by_topic = {s["topic"]: s for s in state.get(name, []) if "topic" in s}
        rows = []
        for e in configured[name] or []:
            st = state_by_topic.get(e["topic"])
            status = _topic_status(st, today)
            health[status] += 1
            st = st or {}
            if status == "failing":
                failing.append(
                    {
                        "topic": f"{name}/{e['topic']}",
                        "last_error": st.get("last_error"),
                        "consecutive_failures": st.get("consecutive_failures"),
                    }
                )
            rows.append(
                {
                    "topic": e["topic"],
                    "url": e["url"],
                    "status": status,
                    "chunks": int(st.get("chunks", 0) or 0),
                    "last_success": st.get("synced"),
                    "last_error": st.get("last_error"),
                    "consecutive_failures": int(st.get("consecutive_failures", 0) or 0),
                    "trust": _trust_label(e["url"], allowlist, st.get("trust")),
                }
            )
        worst = min((r["status"] for r in rows), key=_STATUS_ORDER.index, default="ok")
        entry = {
            "service": name,
            "topics": len(rows),
            "chunks": sum(r["chunks"] for r in rows),
            "status": worst,
        }
        if service is not None or detail:
            entry["topics"] = rows
            entry["topic_count"] = len(rows)
        services.append(entry)

    out: dict = {"health": health, "count": len(services), "services": services}
    if service is None and not detail:
        # Keyed by name, not a list of {"service": name, ...}: the repeated key alone is
        # ~1 KB at forge's size, and the summary has to fit well under the result limit.
        out["services"] = {e.pop("service"): e for e in services}
        for f in failing:
            if f["last_error"] and len(f["last_error"]) > _SUMMARY_ERROR_CHARS:
                f["last_error"] = f["last_error"][:_SUMMARY_ERROR_CHARS] + "…"
        out["failing"] = failing[:_SUMMARY_FAILING_MAX]
        if len(failing) > _SUMMARY_FAILING_MAX:
            out["failing_truncated"] = len(failing) - _SUMMARY_FAILING_MAX
    else:
        out["failing"] = failing
    if allowlist is None:
        out["warning"] = "allowlist unavailable; trust shown as 'unknown'"
    log.info("doc_cache_list_services", service_count=len(services), failing=len(failing))
    return out


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


@mcp.tool
def doc_cache_add_service(service: str, entries: list[DocEntry]) -> dict:
    """Register NEW topics for a service (creating the service if needed). Strictly additive.

    Every URL is classified by the docs-cache allowlist before anything is written:
    ``trusted`` (an allowlisted host or forge endpoint) or ``provisional`` (any other public
    host, when the provisional tier is enabled; cached apart, labelled unvetted, expires
    after the allowlist's TTL unless Ted promotes the host). Refused: non-https, IP
    literals, userinfo, non-443 ports, open-edit wikis, disallowed path shapes, excluded
    hosts, anything resolving to a private address.

    A topic that already exists with the same URL is left alone (``unchanged``). One that
    exists with a DIFFERENT url is an error: use ``doc_cache_update_topic`` to repoint it.
    All-or-nothing: one bad entry refuses the whole call.

    Does not fetch anything; call ``doc_cache_sync`` afterwards.

    Returns ``outcome`` — ``pushed`` / ``committed_local`` / ``written`` / ``unchanged`` /
    ``rolled_back`` / ``refused`` / ``stranded`` — with ``reason`` for anything but success,
    plus ``added``, ``provisional`` and any ``warnings``.

    Args:
        service: Service key, ``^[A-Za-z0-9_-]+$``.
        entries: List of ``{topic, url}``. ``topic`` is ``^[A-Za-z0-9._-]+$``.
    """
    settings = get_settings()
    err = _validate_service(service)
    if err:
        return _refused(err)
    if not entries:
        return _refused("entries must be a non-empty list of {topic, url}")
    if len(entries) > settings.max_entries_per_add:
        return _refused(f"too many entries (max {settings.max_entries_per_add})")

    # Loaded fresh each call so allowlist edits take effect without a restart (vikunja#374).
    try:
        allowlist = load_allowlist(settings.allowlist_path)
    except AllowlistError as e:
        return _refused(f"allowlist unavailable: {e}")

    clean: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for ent in entries:
        topic, url = (ent.topic or "").strip(), (ent.url or "").strip()
        err = _validate_topic(topic)
        if err:
            return _refused(err)
        if topic in seen:
            return _refused(f"duplicate topic {topic!r} in this request")
        trust, err = _classify(url, allowlist, topic)
        if err:
            return _refused(err)
        seen.add(topic)
        clean.append((topic, url, trust))

    prov_topics = [t for t, _, trust in clean if trust == PROVISIONAL]
    prov_hosts = {_host(u) for _, u, trust in clean if trust == PROVISIONAL}

    def mutate(config: dict) -> dict:
        services = config["services"]
        block = list(services.get(service) or [])
        by_topic = {e["topic"]: e for e in block if isinstance(e, dict) and "topic" in e}
        added, unchanged, warnings = [], [], []
        for topic, url, trust in clean:
            existing = by_topic.get(topic)
            if existing is not None:
                if existing.get("url") == url:
                    unchanged.append(topic)
                    continue
                return _refused(
                    f"topic {topic!r} already exists in {service!r} with a different url "
                    f"({existing.get('url')}); use doc_cache_update_topic to repoint it"
                )
            warnings += _duplicate_url_warnings(block, topic, url)
            entry = _source_entry(topic, url, trust)
            block.append(entry)
            by_topic[topic] = entry
            added.append(topic)
        fields = {"service": service, "added": added, "unchanged": unchanged}
        if warnings:
            fields["warnings"] = warnings
        if not added:
            return {**fields, "outcome": UNCHANGED, "sha": None, "change": None, "reason": None}
        new_prov = {_host(by_topic[t]["url"]) for t in added if t in prov_topics}
        cap = _provisional_cap(config, new_prov, allowlist) if new_prov else None
        if cap:
            return _refused(cap)
        services[service] = block
        fields["entries"] = block
        fields["provisional"] = [t for t in added if t in prov_topics]
        return fields

    result = _write_config(
        mutate,
        service=service,
        message=f"doc-cache: add {service} ({len(clean)} topic(s))",
        expected="additive",
    )
    _announce_provisional(service, result.get("provisional") or [], prov_hosts, result)
    log.info(
        "doc_cache_add_service",
        service=service,
        outcome=result.get("outcome"),
        added=len(result.get("added") or []),
        provisional=len(result.get("provisional") or []),
    )
    emit_metric(
        "doc_cache_tool",
        {"tool": "add_service", "outcome": str(result.get("outcome"))},
        {"added": len(result.get("added") or [])},
    )
    return result


@mcp.tool
def doc_cache_update_topic(service: str, topic: str, url: str) -> dict:
    """Repoint one EXISTING topic at a new URL.

    The new URL is classified exactly as ``doc_cache_add_service`` classifies one. The
    topic keeps its name and position; its cached chunks are replaced on the next
    ``doc_cache_sync``. A provisional topic keeps its original ``added`` date, so
    repointing does not restart its TTL.

    Returns ``outcome`` (see ``doc_cache_add_service``) and ``previous_url``.

    Args:
        service: Existing service key.
        topic: Existing topic in that service.
        url: The new https source URL.
    """
    settings = get_settings()
    for err in (_validate_service(service), _validate_topic(topic)):
        if err:
            return _refused(err)
    try:
        allowlist = load_allowlist(settings.allowlist_path)
    except AllowlistError as e:
        return _refused(f"allowlist unavailable: {e}")
    url = (url or "").strip()
    trust, err = _classify(url, allowlist, topic)
    if err:
        return _refused(err)

    def mutate(config: dict) -> dict:
        block = list(config["services"].get(service) or [])
        idx = next((i for i, e in enumerate(block) if e.get("topic") == topic), None)
        if idx is None:
            return _refused(
                f"topic {topic!r} does not exist in {service!r}; use doc_cache_add_service"
            )
        old = block[idx]
        fields = {"service": service, "topic": topic, "previous_url": old.get("url")}
        if old.get("url") == url:
            return {**fields, "outcome": UNCHANGED, "sha": None, "change": None, "reason": None}
        if trust == PROVISIONAL:
            cap = _provisional_cap(config, {_host(url)}, allowlist)
            if cap:
                return _refused(cap)
        keep_added = old.get("added") if old.get("trust") == PROVISIONAL else None
        block[idx] = _source_entry(topic, url, trust, keep_added)
        warnings = _duplicate_url_warnings(block, topic, url)
        if warnings:
            fields["warnings"] = warnings
        config["services"][service] = block
        fields["trust"] = trust
        return fields

    result = _write_config(
        mutate, service=service, message=f"doc-cache: repoint {service}/{topic}", expected="repoint"
    )
    if trust == PROVISIONAL:
        _announce_provisional(service, [topic], {_host(url)}, result)
    log.info("doc_cache_update_topic", service=service, topic=topic, outcome=result.get("outcome"))
    emit_metric(
        "doc_cache_tool", {"tool": "update_topic", "outcome": str(result.get("outcome"))}, {"n": 1}
    )
    return result


@mcp.tool
def doc_cache_remove_topic(service: str, topics: list[str], remove_service: bool = False) -> dict:
    """Remove topics from a service, and their cached chunks and sync state.

    All-or-nothing: every named topic must exist. Removing the last topic of a service
    removes the service, and needs ``remove_service=true`` — as does passing an empty
    ``topics`` list to remove a whole service. At most ``max_remove_per_call`` (20) topics
    per call.

    The config change is made first (``outcome`` as for ``doc_cache_add_service``). Only if
    it lands are the chunks deleted from both cache tiers, the state entries dropped and the
    metadata index pruned; ``cache`` reports that half. If ``cache.ok`` is false, call this
    again with the same topics: a topic already gone from the config but still in sync state
    is cleaned up (``outcome: unchanged``, listed under ``orphans``).

    Args:
        service: Existing service key.
        topics: Topics to remove. Empty only with ``remove_service=true``.
        remove_service: Required when the call would leave the service with no topics.
    """
    settings = get_settings()
    err = _validate_service(service)
    if err:
        return _refused(err)
    names = list(dict.fromkeys((t or "").strip() for t in topics or []))
    for t in names:
        err = _validate_topic(t)
        if err:
            return _refused(err)
    if not names and not remove_service:
        return _refused("topics is empty; pass remove_service=true to remove the whole service")

    # A topic already gone from the config but still in sync state is the leftover of a
    # removal whose cache half failed (lock timeout, OSError). Naming it again cleans it up;
    # before, the retry was refused and nothing else ever deleted those chunks (audit F-06).
    leftovers = _state_topics(service)

    def mutate(config: dict) -> dict:
        services = config["services"]
        block = list(services.get(service) or [])
        present = [e.get("topic") for e in block]
        missing = [t for t in names if t not in present]
        orphans = [t for t in missing if t in leftovers]
        unknown = [t for t in missing if t not in leftovers]
        if unknown:
            if service not in services:
                return _refused(f"unknown service {service!r}")
            return _refused(f"topic(s) not in {service!r}: {unknown}")
        names_present = [t for t in names if t in present]
        if orphans and not names_present:
            return {
                "service": service,
                "removed": [],
                "orphans": orphans,
                "outcome": UNCHANGED,
                "sha": None,
                "change": None,
                "reason": "config already lacks these topics; cleaning up their cache only",
            }
        if service not in services:
            return _refused(f"unknown service {service!r}")
        doomed = present if (remove_service and not names) else names_present
        if remove_service and set(doomed) != set(present):
            return _refused(
                f"remove_service=true removes every topic; list none, or all {len(present)} of them"
            )
        if len(doomed) > settings.max_remove_per_call:
            return _refused(
                f"would remove {len(doomed)} topics; max_remove_per_call is "
                f"{settings.max_remove_per_call}"
            )
        remaining = [e for e in block if e.get("topic") not in doomed]
        if not remaining and not remove_service:
            return _refused(
                f"this removes every topic of {service!r}; pass remove_service=true to "
                "remove the service"
            )
        if remaining:
            services[service] = remaining
        else:
            del services[service]
        out = {"service": service, "removed": doomed, "service_removed": not remaining}
        if orphans:
            out["orphans"] = orphans
        return out

    result = _write_config(
        mutate, service=service, message=f"doc-cache: remove from {service}", expected="remove"
    )
    to_clean = (result.get("removed") or []) + (result.get("orphans") or [])
    if to_clean and (_landed(result) or result.get("outcome") == UNCHANGED):
        result["cache"] = _remove_cached(service, to_clean)
    log.info(
        "doc_cache_remove_topic",
        service=service,
        outcome=result.get("outcome"),
        removed=len(result.get("removed") or []),
    )
    emit_metric(
        "doc_cache_tool",
        {"tool": "remove_topic", "outcome": str(result.get("outcome"))},
        {"removed": len(result.get("removed") or [])},
    )
    return result


def _state_topics(service: str) -> set[str]:
    """Topics doc-sync has state for in ``service``. Empty if state cannot be read."""
    try:
        state = load_doc_sync().load_state()
    except (FileNotFoundError, ImportError, OSError, ValueError, AttributeError) as e:
        log.error("state_read_failed", error=str(e))
        return set()
    return {e.get("topic") for e in state.get(service, []) if isinstance(e, dict)}


def _remove_cached(service: str, topics: list[str]) -> dict:
    """The cache half of a removal, via doc-sync's own ``remove_topics``."""
    try:
        ds = load_doc_sync()
    except (FileNotFoundError, ImportError) as e:
        log.error("remove_cache_load_failed", error=str(e))
        return {"ok": False, "error": "doc-sync module not available; chunks left in place"}
    remover = getattr(ds, "remove_topics", None)
    if remover is None:
        return {
            "ok": False,
            "error": "this doc-sync.py has no remove_topics (older than doc-cache-mcp 0.3.0 "
            "expects); chunks and state left in place",
        }
    try:
        out = remover(service, topics)
    except (OSError, ValueError, TimeoutError) as e:
        log.error("remove_cache_failed", error=str(e))
        return {"ok": False, "error": f"cache cleanup failed ({type(e).__name__}; see server log)"}
    out["ok"] = out.get("pruned") is not False
    return out


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


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
    (``errors`` counts them). Each of ``results`` carries a ``status`` — ``ok``,
    ``failing`` (with ``error``) or ``expired_provisional`` (skipped: an unvetted source
    past its TTL; not an error) — and the ``trust`` tier its chunks were written under.
    A call that could not run at all returns ``outcome: refused`` with a ``reason``.
    If the underlying doc-sync still reports an index step,
    an ``index_error`` from it is surfaced at the top level and also makes ``ok`` false.

    Args:
        service: Service key to sync (must exist in doc-sync.yml).
        dry_run: If true, report what would be synced without fetching or writing.
    """
    err = _validate_service(service)
    if err:
        return _refused(err)

    ds = load_doc_sync()
    t0 = time.perf_counter()
    heartbeat = asyncio.create_task(_heartbeat(ctx, service))
    try:
        # sync_service (fetch + chunk + write) is synchronous doc-sync logic shared with
        # the CLI cron — run it off the event loop so the heartbeat task above can actually
        # keep ticking while it's in flight.
        result = await asyncio.to_thread(ds.sync_service, service, dry_run=dry_run)
    except ValueError as e:  # unknown service — safe, useful message (no paths)
        return _refused(str(e))
    except FileNotFoundError as e:
        log.error("sync_config_missing", error=str(e))
        return _refused("docs cache config or sync module not found")
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
