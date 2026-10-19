#!/usr/bin/env python3
# ======================================================================================
# TEST FIXTURE — not the live script. doc-cache-mcp never loads this in production.
#
# A verbatim copy of host-forge-scripts/scripts/doc-sync.py (doc-cache-friction-2026-10
# p1: provisional tier, failure state, remove_topics). The memsearch index step it was
# first copied without is gone from the live script too, so it is no longer a
# "less the index step" copy — just a copy.
#
# It exists because the tests that load the live script skip wherever it is absent — which
# includes GitHub CI — so nothing CI ran could see doc-cache-mcp break against the doc-sync
# it ships with. tests/test_indexfree_doc_sync.py loads this through the real
# load_doc_sync() path instead, unskipped, and on forge checks it still matches live.
# ======================================================================================
"""
doc-sync: Fetch, convert, and cache documentation for homelab services.

Saves chunked markdown files to ~/.claude/memory/docs/<service>/, which qmd's `docs`
collection indexes (host-forge-scripts/index.yml). Agents search it through qmd.

Sources the allowlist classifies as *provisional* (unvetted hosts, doc-cache-friction-2026-10)
go to ~/.claude/docs-provisional/<service>/ instead. That directory is deliberately outside
~/.claude/memory: qmd's agent-memory collection and the memory pipeline walk that whole tree,
so a directory there would be served, and promoted, as agent memory.

Config: ~/docs/doc-sync.yml
Cache:  ~/.claude/memory/docs/ (trusted), ~/.claude/docs-provisional/ (provisional)
Log:    ~/docs/doc-sync.log

Reusable API (imported by doc-cache-mcp — single source of truth for chunk/write logic):
    load_config() -> dict
    load_state()  -> dict
    sync_service(service, *, force=False, dry_run=False) -> dict
    remove_topic(service, topic) -> dict
    remove_topics(service, topics) -> dict

State entries carry `synced` (date of the last SUCCESS, or None if there never was one) and,
while a topic is failing, `last_attempt`, `last_error` and `consecutive_failures`. A failure
used to leave the entry untouched, so a dead source showed its last good date forever.

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
import subprocess
import yaml
import logging
import requests
import html2text
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

# The shared source-URL allowlist lives next to this script. When doc-sync.py is imported
# by-path (e.g. by doc-cache-mcp) its own directory is not on sys.path — add it so the
# fetch-time SSRF guard resolves the SAME module the CLI cron uses (single source of truth).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import doc_cache_allowlist as _allow

CONFIG_FILE  = Path.home() / "docs" / "doc-sync.yml"
CACHE_DIR    = Path.home() / ".claude" / "memory" / "docs"
# Must agree with doc-cache-mcp's `provisional_cache_dir` setting: same env var, same default.
PROVISIONAL_CACHE_DIR = Path(os.environ.get(
    "DOC_CACHE_MCP_PROVISIONAL_CACHE_DIR",
    Path.home() / ".claude" / "docs-provisional",
)).expanduser()
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


def refuse_open_edit(url: str) -> None:
    """Raise AllowlistError if the URL is an open-edit source (a public wiki).

    The rule set lives in doc_cache_allowlist now (vikunja#1167), so add time and fetch time
    run the same code; this wrapper keeps the old name for callers and tests.
    """
    try:
        allowlist = _get_allowlist()
    except _allow.AllowlistError:
        allowlist = None  # the floor still applies; classify_url refuses the URL anyway
    _allow.refuse_open_edit(url, allowlist)


def fetch_classified(url: str, max_redirects: int = 5) -> tuple[str, str]:
    """Fetch a URL with the docs-cache allowlist enforced AT FETCH TIME (SSRF / cache
    poisoning guard, F-01). Returns ``(text, trust)``.

    The URL — and every redirect hop — is classified against the allowlist immediately
    before the request, in this process, so the DNS resolve-and-recheck actually protects
    the fetch (closing the add-time/fetch-time TOCTOU). Redirects are never auto-followed:
    each ``Location`` is re-classified before it is fetched, so an allowlisted host cannot
    301/302 the fetcher to an internal address.

    ``trust`` is the LOWEST tier of any hop: a provisional URL that redirects to a trusted
    host is still provisional content, because the provisional host chose where it went.
    The other direction is REFUSED: a trusted URL that redirects to a provisional host would
    otherwise yield unvetted content that no TTL, ``max_hosts`` count or promotion event
    ever sees, because all three key on the configured URL (audit F-02). That is also what
    v1 did with the same redirect.

    A provisional fetch is capped at the allowlist's ``provisional.max_bytes``; a trusted
    one is read exactly as it always was. TLS certificate verification is the control that
    stops a DNS rebind between the recheck and the connection (an internal service would
    need a valid certificate for the attacker's name), so a session with it disabled is
    refused for provisional fetches (audit F-04).

    Raises ``doc_cache_allowlist.AllowlistError`` (refused), a requests error,
    ``requests.TooManyRedirects``, or ``ValueError`` (provisional body over the cap).
    """
    allowlist = _get_allowlist()
    current = url
    trust = _allow.TRUSTED
    for _ in range(max_redirects + 1):
        verdict = _allow.classify_url(current, allowlist)
        if current != url and trust == _allow.TRUSTED and verdict.trust == _allow.PROVISIONAL:
            raise _allow.AllowlistError(
                f"a trusted source redirected to an unvetted host ({verdict.host!r}); "
                "refused — register that URL itself if it should be cached"
            )
        trust = _allow.lower_trust(trust, verdict.trust)
        if trust == _allow.PROVISIONAL and getattr(SESSION, "verify", True) is False:
            raise _allow.AllowlistError(
                "TLS verification is disabled; provisional fetches rely on it (rebind guard)"
            )
        if trust == _allow.PROVISIONAL:
            r = SESSION.get(current, timeout=30, allow_redirects=False, stream=True)
        else:
            r = SESSION.get(current, timeout=30, allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location")
            if loc:
                _close(r)
                current = urljoin(current, loc)
                continue
        r.raise_for_status()
        if trust == _allow.PROVISIONAL:
            return _read_capped(r, allowlist["provisional"]["max_bytes"], current), trust
        return r.text, trust
    raise requests.TooManyRedirects(f"exceeded {max_redirects} redirects fetching {url}")


def _close(r) -> None:
    close = getattr(r, "close", None)
    if close:
        close()


def _read_capped(r, max_bytes: int, url: str) -> str:
    """Read at most ``max_bytes`` of a streamed body; refuse rather than truncate.

    Truncating would cache half a page as if it were the page. An unvetted host serving
    more than the cap is a refusal with the cap named, not a silent partial.
    """
    try:
        declared = int(r.headers.get("Content-Length", "0"))
    except ValueError:
        declared = 0
    if declared > max_bytes:
        _close(r)
        raise ValueError(f"provisional source exceeds max_bytes ({declared} > {max_bytes}): {url}")
    buf = bytearray()
    for chunk in r.iter_content(chunk_size=65536):
        buf.extend(chunk)
        if len(buf) > max_bytes:
            _close(r)
            raise ValueError(f"provisional source exceeds max_bytes ({max_bytes}): {url}")
    return bytes(buf).decode(r.encoding or "utf-8", errors="replace")


def safe_fetch(url: str, max_redirects: int = 5) -> str:
    """:func:`fetch_classified` for callers that only want the text."""
    return fetch_classified(url, max_redirects)[0]


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


def _tier_dir(trust: str) -> Path:
    return PROVISIONAL_CACHE_DIR if trust == _allow.PROVISIONAL else CACHE_DIR


def _check_service_key(service: str) -> None:
    # Belt-and-suspenders: the service key becomes a directory name. The MCP already
    # validates it, but the cron iterates raw config keys, so validate here too (F-07).
    if not _SERVICE_KEY_RE.fullmatch(service):  # `$` alone admits a trailing newline
        raise ValueError(f"unsafe service key {service!r}: refusing to use as a directory name")


def _topic_files(out_dir: Path, topic: str) -> list[Path]:
    """The chunk files that belong to ``topic`` — by their frontmatter, not just the name.

    The ``<slug>-*.md`` glob alone also matches every topic whose slug extends this one
    (``install`` matches ``install-docker-compose-00-....md``), so writing or removing one
    topic used to delete its neighbour's chunks too.
    """
    out = []
    for f in out_dir.glob(f"{slug(topic)}-*.md"):
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                head = [next(fh, "") for _ in range(8)]
        except OSError:
            continue
        if f"topic: {topic}\n" in head:
            out.append(f)
    return out


def _unlink_topic(service: str, topic: str, trust: str) -> list[Path]:
    out_dir = _tier_dir(trust) / service
    if not out_dir.is_dir():
        return []
    gone = _topic_files(out_dir, topic)
    for f in gone:
        f.unlink()
    return gone


def write_chunks(
    service: str, topic: str, url: str, chunks: list[dict], trust: str = "trusted"
) -> list[Path]:
    _check_service_key(service)

    today      = date.today().isoformat()
    expires    = (date.today() + timedelta(days=90)).isoformat()
    out_dir    = _tier_dir(trust) / service
    out_dir.mkdir(parents=True, exist_ok=True)

    olds = _topic_files(out_dir, topic)
    for old in olds:
        old.unlink()
    # A topic that changed tier (promoted, or repointed onto an unvetted host) must not
    # leave its chunks in the other tier's directory, still labelled as they were.
    other = _allow.TRUSTED if trust == _allow.PROVISIONAL else _allow.PROVISIONAL
    olds += _unlink_topic(service, topic, other)

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
            f"trust: {trust}\n"
            f"created: {today}\n"
            f"expires: {expires}\n"
            f"tags: [doc-cache, {service}, docs]\n"
            f"---\n\n"
        )
        fpath.write_text(frontmatter + chunk["body"])
        written.append(fpath)

    REMOVED_PATHS.update(set(olds) - set(written))
    return written


# ── Metadata index prune (vikunja#1162) ───────────────────────────────────────
#
# memory-expire prunes .metadata.db at 03:00, the same minute this cron starts
# deleting and re-chunking files, so rows for files removed here outlived the
# prune by a day and tripped the 02:30 memory-archive-mirror. doc-sync now
# prunes after itself whenever it removed a file. A rewrite to the same path
# leaves its row valid, so only paths that were not rewritten count.

REMOVED_PATHS: set[Path] = set()
INDEX_SCRIPT = Path(__file__).resolve().parent / "memory-metadata-index.py"
PRUNE_RETRY_DELAY = 15.0


def prune_metadata_index(retry_delay: float | None = None) -> bool:
    """Run memory-metadata-index.py --prune. False (logged) on failure.

    One retry: memory-expire's own prune can hold the SQLite lock in the
    same minute, and a lock collision is not worth a CronJobDown page.
    """
    if not INDEX_SCRIPT.is_file():
        log.error(f"index prune: {INDEX_SCRIPT} not found")
        return False
    for attempt in (1, 2):
        try:
            result = subprocess.run(
                [sys.executable, str(INDEX_SCRIPT), "--prune"],
                capture_output=True, text=True, timeout=600,
            )
        except (subprocess.SubprocessError, OSError) as e:
            log.error(f"index prune: attempt {attempt} failed to run: {e}")
        else:
            if result.returncode == 0:
                log.info(f"index prune: ok ({len(REMOVED_PATHS)} file(s) removed this run)")
                return True
            tail = (result.stderr or "").strip().splitlines()[-3:]
            log.error(f"index prune: attempt {attempt} exited {result.returncode}: {' | '.join(tail)}")
        if attempt == 1:
            time.sleep(PRUNE_RETRY_DELAY if retry_delay is None else retry_delay)
    return False


# ── Manifest ──────────────────────────────────────────────────────────────────

def write_manifest(state: dict):
    today = date.today().isoformat()
    lines = [f"# Doc Cache Manifest\n\nLast updated: {today}\n"]
    for service, entries in sorted(state.items()):
        lines.append(f"\n## {service}\n")
        for entry in entries:
            lines.append(
                f"- **{entry['topic']}** — {entry.get('chunks', 0)} chunks — "
                f"synced {entry.get('synced') or 'never'} — `{_tier_dir(entry.get('trust', 'trusted'))}/{service}/`  \n"
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

def _provisional_expired(entry: dict, ttl_days: int) -> bool:
    """True if a provisional entry is past its TTL. A missing or bad `added` is expired:
    unvetted content with no provenance date fails closed, not open-ended."""
    try:
        added = date.fromisoformat(str(entry.get("added")))
    except ValueError:
        return True
    return date.today() > added + timedelta(days=ttl_days)


def sync_entry_detail(service: str, entry: dict) -> dict:
    """Sync one entry. Returns ``{status, chunks, trust, error}``; never raises.

    ``status`` is ``ok``, ``failing`` or ``expired_provisional``. Trust is recomputed from
    the allowlist here, on every run: the entry's own ``trust`` field is informational, so
    promoting a host in the allowlist is the whole promotion step.
    """
    topic = entry["topic"]
    url   = entry["url"]

    log.info(f"[{service}] {topic} — {url}")
    try:
        allowlist = _get_allowlist()
        tier = _allow.classify_url(url, allowlist).trust
        if tier == _allow.PROVISIONAL and _provisional_expired(
            entry, allowlist["provisional"]["ttl_days"]
        ):
            # Both tiers: a host Ted DEMOTED from `hosts` lands here too (it has no
            # `added`), and its chunks are in the trusted, indexed directory (audit F-05).
            gone = _unlink_topic(service, topic, _allow.PROVISIONAL)
            gone += _unlink_topic(service, topic, _allow.TRUSTED)
            REMOVED_PATHS.update(gone)
            log.warning(
                f"[{service}] {topic} — provisional source past its TTL and not promoted; "
                f"skipped, {len(gone)} chunk(s) removed"
            )
            return {"status": "expired_provisional", "chunks": 0, "trust": tier, "error": None}
        raw, trust = fetch_classified(url)
        md       = to_markdown(raw, url)
        chunks   = chunk_by_headings(md)
        if not chunks:
            log.warning(f"[{service}] {topic} — no chunks extracted, skipping")
            return {"status": "ok", "chunks": 0, "trust": trust, "error": None}
        written  = write_chunks(service, topic, url, chunks, trust)
        log.info(f"[{service}] {topic} — {len(written)} chunks written ({trust})")
        return {"status": "ok", "chunks": len(written), "trust": trust, "error": None}
    except Exception as e:
        log.error(f"[{service}] {topic} — FAILED: {e}")
        return {"status": "failing", "chunks": -1, "trust": None, "error": str(e)[:200]}


def sync_entry(service: str, entry: dict) -> int:
    """Chunk count, or -1 on failure. See :func:`sync_entry_detail`."""
    return sync_entry_detail(service, entry)["chunks"]


def _sync_service_entries(service: str, entries: list[dict], state: dict) -> dict:
    """Sync one service's entries into `state` (mutated in place). No lock / no disk
    persistence of state here — the caller owns state_lock(), save_state() and the
    manifest. Returns a per-service summary.

    A failure records `last_attempt`, `last_error` and `consecutive_failures` and leaves
    `synced` (the last success) alone; a success clears them. An expired provisional topic
    is a skip, not an error: it does not count toward `errors` or the CLI exit code.
    """
    today = date.today().isoformat()
    if service not in state:
        state[service] = []

    synced = errors = chunks = expired = 0
    results = []
    for entry in entries:
        topic = entry["topic"]
        url   = entry["url"]
        d = sync_entry_detail(service, entry)
        prev = next((e for e in state[service] if e["topic"] == topic), None)
        state[service] = [e for e in state[service] if e["topic"] != topic]
        if d["status"] == "ok":
            synced += 1
            chunks += d["chunks"]
            state[service].append({
                "topic": topic, "url": url, "chunks": d["chunks"], "synced": today,
                "trust": d["trust"],
            })
            results.append({"topic": topic, "url": url, "chunks": d["chunks"],
                            "status": "ok", "trust": d["trust"]})
        elif d["status"] == "expired_provisional":
            expired += 1
            state[service].append({
                "topic": topic, "url": url, "chunks": 0,
                "synced": (prev or {}).get("synced"), "trust": d["trust"],
                "expired": True,
            })
            results.append({"topic": topic, "url": url, "chunks": 0,
                             "status": "expired_provisional", "trust": d["trust"]})
        else:
            errors += 1
            kept = dict(prev) if prev else {"topic": topic, "url": url, "chunks": 0, "synced": None}
            kept.pop("expired", None)
            kept.update({
                "last_attempt": today,
                "last_error": d["error"],
                "consecutive_failures": int(kept.get("consecutive_failures", 0) or 0) + 1,
            })
            state[service].append(kept)
            results.append({"topic": topic, "url": url, "status": "failing",
                            "error": d["error"]})

    return {"entries_synced": synced, "chunks": chunks, "errors": errors,
            "expired": expired, "results": results}


def sync_service(service: str, *, force: bool = False, dry_run: bool = False) -> dict:
    """Ingest one configured service into the docs cache. Single-service public API.

    Imported by doc-cache-mcp instead of shelling out. `force` is accepted for API
    symmetry — doc-sync has no up-to-date short-circuit and always re-fetches, so it is a
    no-op today (matching the CLI's historical `--force` behaviour).

    Returns:
        {service, entries_synced, chunks, errors, ok, dry_run, results:[...]}

        `errors` counts per-entry fetch/convert failures. `ok` is False if any entry
        failed. The memsearch index step, and with it the `indexed` / `index_error`
        keys, were removed when memsearch retired (memsearch-retirement-finish-2026-09
        p2); qmd's `docs` collection indexes the cache on its own schedule.

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
        "expired": summary.get("expired", 0),
        "ok": summary["errors"] == 0,
        "dry_run": False,
        "results": summary["results"],
    }


# ── Removal ───────────────────────────────────────────────────────────────────

def remove_topics(service: str, topics: list[str]) -> dict:
    """Delete these topics' chunks (both tiers) and state entries, then prune the index.

    The config entry is the caller's to remove (doc-cache-mcp edits doc-sync.yml through
    its guarded commit path); this removes everything doc-sync itself wrote. Removing a
    topic that has no chunks or state is not an error — the result says what was found.
    """
    _check_service_key(service)
    removed: list[Path] = []
    state_removed: list[str] = []
    with state_lock():
        state = load_state()
        for topic in topics:
            for tier in (_allow.TRUSTED, _allow.PROVISIONAL):
                removed += _unlink_topic(service, topic, tier)
            before = len(state.get(service, []))
            state[service] = [e for e in state.get(service, []) if e["topic"] != topic]
            if len(state[service]) != before:
                state_removed.append(topic)
        if service in state and not state[service]:
            del state[service]
        for tier in (_allow.TRUSTED, _allow.PROVISIONAL):
            d = _tier_dir(tier) / service
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        save_state(state)
        write_manifest(state)
    REMOVED_PATHS.update(removed)
    pruned = prune_metadata_index() if removed else None
    return {
        "service": service,
        "topics": list(topics),
        "files_removed": len(removed),
        "state_removed": state_removed,
        "pruned": pruned,
    }


def remove_topic(service: str, topic: str) -> dict:
    """:func:`remove_topics` for one topic."""
    return remove_topics(service, [topic])


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
    total_expired = 0

    with state_lock():
        state = load_state()
        for service, entries in services.items():
            summary = _sync_service_entries(service, entries, state)
            total_synced += summary["entries_synced"]
            total_errors += summary["errors"]
            total_expired += summary.get("expired", 0)
        save_state(state)
        write_manifest(state)

    log.info(
        f"Sync complete. {total_synced} entries synced, {total_errors} errors, "
        f"{total_expired} expired provisional skipped."
    )

    # A failed prune fails the run, unlike memory-expire's warning: a stale row
    # surfaces a day later in a different job, which is how #1162 hid.
    if REMOVED_PATHS and not prune_metadata_index():
        total_errors += 1

    # Any fetch/convert error is a failed run, so the 03:00 doc-sync-daily cron exits 1.
    # This used to also cover a failed memsearch index (vikunja#372); that step went with
    # memsearch (memsearch-retirement-finish-2026-09 p2) and qmd indexes the cache itself.
    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
