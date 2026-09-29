#!/usr/bin/env python3
# ======================================================================================
# TEST FIXTURE — not the live script. doc-cache-mcp never loads this in production.
#
# A copy of host-forge-scripts/scripts/doc-sync.py (as of 5f00749) with the memsearch
# index step removed: no run_memsearch_index, no `index` kwarg, no MEMSEARCH_BIN, and no
# `indexed` / `index_error` keys in sync_service()'s result. That is the doc-sync.py
# memsearch-retirement-finish-2026-09 part 2 ships, and the shape doc-cache-mcp must keep
# reporting correctly once it does.
#
# It exists because the tests that load the live script skip wherever it is absent — which
# includes GitHub CI — so nothing CI ran could see doc-cache-mcp break against an
# index-free doc-sync. tests/test_indexfree_doc_sync.py loads this through the real
# load_doc_sync() path instead, unskipped.
# ======================================================================================
"""
doc-sync: Fetch, convert, and cache documentation for homelab services.

Saves chunked markdown files to ~/.claude/memory/docs/<service>/. qmd's `docs` collection
indexes that directory hourly (qmd-refresh), so there is no index step here.

Config: ~/docs/doc-sync.yml
Cache:  ~/.claude/memory/docs/
Log:    ~/docs/doc-sync.log

Reusable API (imported by doc-cache-mcp — single source of truth for chunk/write logic):
    load_config() -> dict
    load_state()  -> dict
    sync_service(service, *, force=False, dry_run=False) -> dict

The `sync_service()` entry point and the CLI `main()` share the same per-service core
(`_sync_service_entries`) and the same `state_lock()`, so the doc-cache-mcp server and the
`doc-sync-daily` cron never race writes to doc-sync-state.json.
"""

import os
import re
import sys
import json
import time
import fcntl
import hashlib
import yaml
import logging
import requests
import html2text
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

# The shared source-URL allowlist lives next to this script. When doc-sync.py is imported
# by-path (e.g. by doc-cache-mcp) its own directory is not on sys.path — add it so the
# fetch-time SSRF guard resolves the SAME module the CLI cron uses (single source of truth).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_cache_allowlist as _allow

CONFIG_FILE  = Path.home() / "docs" / "doc-sync.yml"
CACHE_DIR    = Path.home() / ".claude" / "memory" / "docs"
MANIFEST     = Path.home() / "docs" / "cache-manifest.md"
LOG_FILE     = Path.home() / "docs" / "doc-sync.log"
STATE_FILE   = Path.home() / "docs" / "doc-sync-state.json"
LOCK_FILE    = STATE_FILE.parent / (STATE_FILE.name + ".lock")

# Source-URL allowlist (git-backed, sysadmin-editable). Enforced at fetch time below.
ALLOWLIST_FILE = Path(os.environ.get(
    "DOC_CACHE_ALLOWLIST_FILE",
    Path.home() / "repos" / "gitea" / "host-forge-scripts" / "doc-cache-allowlist.yml",
))

# Service keys become directory names under CACHE_DIR — validate before trusting them (F-07).
_SERVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "forge-doc-sync/1.0 (homelab agent doc cache)"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ── State lock ────────────────────────────────────────────────────────────────

@contextmanager
def state_lock(timeout: float = 120.0):
    """Exclusive flock over the doc-sync state, shared by the CLI cron and the MCP.

    Both `doc-sync-daily` (main) and doc-cache-mcp (sync_service) take this lock around
    their load -> sync -> save critical section, so the two cannot interleave writes to
    doc-sync-state.json. Blocks up to `timeout` seconds waiting for the lock, then raises
    TimeoutError.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire doc-sync state lock within {timeout}s "
                        f"({LOCK_FILE})"
                    )
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch(url: str) -> str:
    """Low-level fetch (no allowlist). Prefer safe_fetch for anything sourced from config."""
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text


_ALLOWLIST_CACHE       = None
_ALLOWLIST_FINGERPRINT = None


def _allowlist_fingerprint():
    """Hash of the allowlist file's current contents, or None if it cannot be read.

    Contents, not stat metadata. A stat key of (mtime_ns, size, inode) looks like it should
    be enough and is not: the timestamps this filesystem actually hands out are far coarser
    than the nanosecond field implies, and an in-place rewrite keeps the inode. Two edits of
    the same length therefore produce a byte-identical stat key across a real content
    change, which is indistinguishable from "nothing happened".

    That is measured, not assumed — a stat-keyed version of this function was written first
    and test_reload_retries_after_a_bad_edit caught it serving a stale allowlist after two
    of three writes landed on the same mtime_ns.

    Reading ~1KB is nothing beside the HTTP request the caller is about to make.
    """
    try:
        return hashlib.sha256(ALLOWLIST_FILE.read_bytes()).hexdigest()
    except OSError:
        return None


def _get_allowlist() -> dict:
    """Load the docs-cache allowlist, reloading it whenever the file's contents change.

    Still cached between calls — the daily cron fetches many URLs and should not re-parse
    the YAML for each one — but keyed on content rather than memoised for the life of the
    process.

    The process-lifetime memo this replaces was a real bug (vikunja#374): doc-cache-mcp is a
    long-lived PM2 daemon, so a sysadmin's allowlist edit could not take effect until
    somebody restarted it, and the refusal it produced pointed the operator at the very file
    that already contained the host.

    If the file cannot be read the cache is bypassed and ``load_allowlist`` is called anyway,
    so a deleted or unreadable allowlist fails closed with its own explicit message rather
    than quietly serving the last good snapshot forever.
    """
    global _ALLOWLIST_CACHE, _ALLOWLIST_FINGERPRINT
    fingerprint = _allowlist_fingerprint()
    if (
        _ALLOWLIST_CACHE is None
        or fingerprint is None
        or fingerprint != _ALLOWLIST_FINGERPRINT
    ):
        # Assign only on success: if load_allowlist raises, the fingerprint is left
        # unchanged so the next call retries rather than latching onto a broken state.
        _ALLOWLIST_CACHE = _allow.load_allowlist(ALLOWLIST_FILE)
        _ALLOWLIST_FINGERPRINT = fingerprint
    return _ALLOWLIST_CACHE


def safe_fetch(url: str, max_redirects: int = 5) -> str:
    """Fetch a URL with the docs-cache allowlist enforced AT FETCH TIME (SSRF / cache
    poisoning guard, F-01).

    The URL — and every redirect hop — is validated against the allowlist immediately
    before the request, in this process, so the DNS resolve-and-recheck actually protects
    the fetch (closing the add-time/fetch-time TOCTOU). Redirects are never auto-followed:
    each ``Location`` is re-validated before it is fetched, so an allowlisted host cannot
    301/302 the fetcher to an internal address.

    Raises ``doc_cache_allowlist.AllowlistError`` (refused), a requests error, or
    ``requests.TooManyRedirects``.
    """
    allowlist = _get_allowlist()
    current = url
    for _ in range(max_redirects + 1):
        _allow.validate_url(current, allowlist)
        r = SESSION.get(current, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            if not loc:
                r.raise_for_status()
                return r.text
            current = urljoin(current, loc)
            continue
        r.raise_for_status()
        return r.text
    raise requests.TooManyRedirects(f"exceeded {max_redirects} redirects fetching {url}")


def to_markdown(content: str, url: str) -> str:
    """Convert HTML to markdown. Pass-through if already markdown."""
    if any(x in url for x in ["raw.githubusercontent.com", "llms.txt", ".md"]):
        return content

    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.ignore_tables = False
    h.body_width = 0
    h.unicode_snob = True
    return h.handle(content)


# ── Chunking ──────────────────────────────────────────────────────────────────

def chunk_by_headings(content: str, min_size: int = 150, max_size: int = 4000) -> list[dict]:
    lines = content.splitlines()
    chunks = []
    current_title = "Overview"
    current_lines = []

    def flush(title, lines):
        body = "\n".join(lines).strip()
        if len(body) >= min_size:
            chunks.append({"title": title, "body": body})

    for line in lines:
        if re.match(r"^## ", line):
            flush(current_title, current_lines)
            current_title = line.lstrip("# ").strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    flush(current_title, current_lines)

    result = []
    for chunk in chunks:
        if len(chunk["body"]) <= max_size:
            result.append(chunk)
            continue
        sub_title = chunk["title"]
        sub_lines = []
        for line in chunk["body"].splitlines():
            if re.match(r"^### ", line):
                body = "\n".join(sub_lines).strip()
                if len(body) >= min_size:
                    result.append({"title": sub_title, "body": body})
                sub_title = f"{chunk['title']} — {line.lstrip('# ').strip()}"
                sub_lines = [line]
            else:
                sub_lines.append(line)
        body = "\n".join(sub_lines).strip()
        if len(body) >= min_size:
            result.append({"title": sub_title, "body": body})

    return result


# ── Writing ───────────────────────────────────────────────────────────────────

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def write_chunks(service: str, topic: str, url: str, chunks: list[dict]) -> list[Path]:
    # Belt-and-suspenders: the service key becomes a directory name. The MCP already
    # validates it, but the cron iterates raw config keys, so validate here too (F-07).
    if not _SERVICE_KEY_RE.match(service):
        raise ValueError(f"unsafe service key {service!r}: refusing to use as a directory name")

    today      = date.today().isoformat()
    expires    = (date.today() + timedelta(days=90)).isoformat()
    out_dir    = CACHE_DIR / service
    out_dir.mkdir(parents=True, exist_ok=True)

    for old in out_dir.glob(f"{slug(topic)}-*.md"):
        old.unlink()

    written = []
    for i, chunk in enumerate(chunks):
        fname    = f"{slug(topic)}-{i:02d}-{slug(chunk['title'])}.md"
        fpath    = out_dir / fname
        frontmatter = (
            f"---\n"
            f"type: doc-cache\n"
            f"tier: working\n"
            f"service: {service}\n"
            f"topic: {topic}\n"
            f"section: {chunk['title']}\n"
            f"source_url: {url}\n"
            f"created: {today}\n"
            f"expires: {expires}\n"
            f"tags: [doc-cache, {service}, docs]\n"
            f"---\n\n"
        )
        fpath.write_text(frontmatter + chunk["body"])
        written.append(fpath)

    return written


# ── Manifest ──────────────────────────────────────────────────────────────────

def write_manifest(state: dict):
    today = date.today().isoformat()
    lines = [f"# Doc Cache Manifest\n\nLast updated: {today}\n"]
    for service, entries in sorted(state.items()):
        lines.append(f"\n## {service}\n")
        for entry in entries:
            lines.append(
                f"- **{entry['topic']}** — {entry['chunks']} chunks — "
                f"synced {entry['synced']} — `~/.claude/memory/docs/{service}/`  \n"
                f"  Source: {entry['url']}"
            )
    MANIFEST.write_text("\n".join(lines) + "\n")


# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    # Atomic write: state metadata is regenerable, but a crash mid-write should not leave a
    # truncated file (F-05). flock (state_lock) already serialises the cron and the MCP.
    tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    os.replace(tmp, STATE_FILE)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load and parse doc-sync.yml. Raises FileNotFoundError if absent."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_FILE}")
    return yaml.safe_load(CONFIG_FILE.read_text()) or {}


# ── Sync core ─────────────────────────────────────────────────────────────────

def sync_entry(service: str, entry: dict) -> int:
    topic = entry["topic"]
    url   = entry["url"]

    log.info(f"[{service}] {topic} — {url}")
    try:
        raw      = safe_fetch(url)
        md       = to_markdown(raw, url)
        chunks   = chunk_by_headings(md)
        if not chunks:
            log.warning(f"[{service}] {topic} — no chunks extracted, skipping")
            return 0
        written  = write_chunks(service, topic, url, chunks)
        log.info(f"[{service}] {topic} — {len(written)} chunks written")
        return len(written)
    except Exception as e:
        log.error(f"[{service}] {topic} — FAILED: {e}")
        return -1


def _sync_service_entries(service: str, entries: list[dict], state: dict) -> dict:
    """Sync one service's entries into `state` (mutated in place). No lock / no disk
    persistence of state here — the caller owns state_lock(), save_state() and the
    manifest. Returns a per-service summary.
    """
    today = date.today().isoformat()
    if service not in state:
        state[service] = []

    synced = errors = chunks = 0
    results = []
    for entry in entries:
        topic = entry["topic"]
        url   = entry["url"]
        n = sync_entry(service, entry)
        if n >= 0:
            synced += 1
            chunks += n
            state[service] = [e for e in state[service] if e["topic"] != topic]
            state[service].append({"topic": topic, "url": url, "chunks": n, "synced": today})
            results.append({"topic": topic, "url": url, "chunks": n})
        else:
            errors += 1
            results.append({"topic": topic, "url": url, "error": "fetch/convert failed"})

    return {"entries_synced": synced, "chunks": chunks, "errors": errors, "results": results}


def sync_service(service: str, *, force: bool = False, dry_run: bool = False) -> dict:
    """Ingest one configured service into the docs cache. Single-service public API.

    Imported by doc-cache-mcp instead of shelling out. `force` is accepted for API
    symmetry — doc-sync has no up-to-date short-circuit and always re-fetches, so it is a
    no-op today (matching the CLI's historical `--force` behaviour).

    Returns:
        {service, entries_synced, chunks, errors, ok, dry_run, results:[...]}

        `errors` counts per-entry fetch/convert failures. `ok` is False if any entry
        failed.

    Raises:
        FileNotFoundError if doc-sync.yml is missing.
        ValueError if `service` is not present in doc-sync.yml.
    """
    config = load_config()
    services = config.get("services", {})
    if service not in services:
        raise ValueError(f"Unknown service: {service}")
    entries = services[service]

    if dry_run:
        return {
            "service": service,
            "entries_synced": 0,
            "chunks": 0,
            "errors": 0,
            "ok": True,
            "dry_run": True,
            "results": [
                {"topic": e["topic"], "url": e["url"], "would_sync": True} for e in entries
            ],
        }

    with state_lock():
        state = load_state()
        summary = _sync_service_entries(service, entries, state)
        save_state(state)
        write_manifest(state)

    return {
        "service": service,
        "entries_synced": summary["entries_synced"],
        "chunks": summary["chunks"],
        "errors": summary["errors"],
        "ok": summary["errors"] == 0,
        "dry_run": False,
        "results": summary["results"],
    }


# ── Main (CLI) ────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sync documentation for homelab agents")
    parser.add_argument("--force",    action="store_true", help="Re-sync even if up to date")
    parser.add_argument("--service",  help="Only sync this service")
    parser.add_argument("--dry-run",  action="store_true", help="Show what would be synced")
    args = parser.parse_args()

    try:
        config = load_config()
    except FileNotFoundError as e:
        log.error(str(e))
        sys.exit(1)

    services = config.get("services", {})
    if args.service:
        if args.service not in services:
            log.error(f"Unknown service: {args.service}")
            sys.exit(1)
        services = {args.service: services[args.service]}

    if args.dry_run:
        for service, entries in services.items():
            for entry in entries:
                print(f"  WOULD SYNC  [{service}] {entry['topic']}")
        return

    total_synced = 0
    total_errors = 0

    with state_lock():
        state = load_state()
        for service, entries in services.items():
            summary = _sync_service_entries(service, entries, state)
            total_synced += summary["entries_synced"]
            total_errors += summary["errors"]
        save_state(state)
        write_manifest(state)

    log.info(f"Sync complete. {total_synced} entries synced, {total_errors} errors.")

    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
