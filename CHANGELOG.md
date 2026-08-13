# Changelog

All notable changes to doc-cache-mcp are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **`doc_cache_add_service` can now finish what it starts** (vikunja#363). It committed
  `doc-sync.yml` to the local branch and stopped, because the agent calling it has no git
  write access — the very gap this server exists to bridge. `committed: true` therefore
  described an operation nobody could complete, and the commits accumulated.

  Pushing is **off by default** (`DOC_CACHE_MCP_GIT_PUSH`), because an MCP writing to a
  shared branch unattended is a real privilege. When enabled it runs behind seven
  fail-closed guards — one that cannot be *evaluated* refuses rather than passes — and
  degrades to a review branch plus a PR URL rather than failing or stranding the commit.
  See "Pushing" in the README. New module: `doc_cache_mcp.push`.
- Tool commits are authored as `doc-cache-mcp` rather than as the host user, so they are
  attributable and distinguishable from human commits.
- Optional JSONL audit sink for push decisions (`DOC_CACHE_MCP_AUDIT_LOG_DIR`) — a trail
  outside git that survives a force-push.
- `doc_cache_mcp.vendoring` generates the vendored allowlist copy that `doc-sync.py`
  imports, replacing a hard link that had silently drifted twice (vikunja#374). A hard
  link cannot survive an atomic-replace write, and a severed one is indistinguishable from
  a working one without stat'ing inodes. Regenerate with
  `python -m doc_cache_mcp.vendoring --write`.

### Fixed
- **A failed memsearch index no longer reads as a clean sync** (vikunja#372).
  `doc_cache_sync` returned `errors: 0` beside a healthy chunk count while the index had
  exited non-zero, leaving the docs cached but unsearchable; the failure was reachable only
  as `result["indexed"]["returncode"]` and logged at info level with the rest of a
  successful sync. The result now carries top-level `ok` and `index_error`, the sync logs
  at error level when the index fails, and an `index_failed` metric is emitted.
- **Allowlist refusals now say which snapshot refused** (vikunja#374). The message sent the
  operator to edit a file that already contained the host, because the process was serving
  a snapshot from before the edit and no edit could take effect. `load_allowlist()` records
  the source path and load time, and the refusal reports both plus the host count, so "not
  in the file" and "not in the copy I loaded" are no longer indistinguishable.

## [0.1.1] — 2026-07-07

### Fixed
- `doc_cache_sync` exceeded MCP client idle-timeouts (typically 300s) on every call,
  because the memsearch reindex step (whole docs cache — 6788 chunks across 59 services
  at time of report) dominates runtime regardless of how small the synced service is. The
  tool call succeeded server-side but was reported as hung/failed client-side. Found by
  research during Phase 5 verify (task `3a8f6098`).
  - `doc_cache_sync` is now async and accepts an optional FastMCP `Context`; it reports
    progress every 15s while the sync runs (`asyncio.to_thread` off the event loop, so the
    heartbeat can actually tick during the blocking memsearch subprocess). No-op if the
    caller's client doesn't send a `progressToken` — never errors, never changes behavior
    for clients that don't support progress.
  - Docstring now states the reindex covers the whole cache and can take several minutes.
  - **Deliberately not implemented**: narrowing the memsearch index call to just the synced
    service's directory. `memsearch index` does "stale cleanup" (deletes chunks for files
    no longer on disk) and its docs don't specify whether that's scoped to the passed
    `PATHS` or the whole collection — guessing wrong against the shared production Milvus
    collection risks silently deleting other services' cached docs. Left as a candidate
    fast-follow, gated on verifying that behavior against a non-production collection.

## [0.1.0] — 2026-07-07

Initial build. Capability-scoped docs-cache MCP replacing research's generic system-ops
grant (ADR-0005). Security-audited (2026-07-07): 1 High + 3 Low + 3 Info; High + 3 Low + 2
Info remediated, 1 Info accepted.

### Added
- FastMCP server on `127.0.0.1:8503` exposing three verbs: `doc_cache_list_services`,
  `doc_cache_add_service`, `doc_cache_sync`.
- Source-URL allowlist (shared `doc_cache_allowlist` module): https-only, name-based host
  allowlist, explicit forge endpoints, IP-literal rejection, DNS resolve-and-recheck
  (SSRF / cache-poisoning guard).
- Structural YAML merge + atomic write + single-file fixed-argv git commit for
  `doc_cache_add_service`.
- Shared `flock` on the doc-sync state file so the MCP and the `doc-sync-daily` cron cannot
  race writes.
- Imports the shared `doc-sync.py` `sync_service()` as the single source of truth (no
  re-implemented chunking, no shelling out for its own logic).

### Security
- **F-01 (High)** — moved the SSRF boundary to the fetch layer. `doc-sync.py` now fetches
  via `safe_fetch`, which re-validates the URL **and every redirect hop** against the
  allowlist at fetch time (`allow_redirects=False`, manual per-hop re-validation). Both the
  MCP and the `doc-sync-daily` cron are now covered, closing the add-time/fetch-time TOCTOU
  and the redirect bypass. The allowlist is now a single shared module imported by both.
- **F-02 (Low)** — `forge_endpoint` path match is now boundary-aware and traversal-safe
  (path normalisation + `/`-boundary), so `docs.json.backup` and `docs.json/../tasks` no
  longer match `docs.json`.
- **F-03 (Low)** — tool error responses no longer leak filesystem paths or git stderr; full
  detail goes to the structlog line only.
- **F-04 (Low)** — `_assert_resolves_public` now unwraps IPv4-mapped / 6to4 IPv6 before
  classification, so a resolver returning `::ffff:10.x` cannot smuggle a private address.
- **F-05 (Info)** — `save_state` now writes atomically (`.tmp` + `os.replace`).
- **F-07 (Info)** — `doc-sync.py` validates service keys (`^[A-Za-z0-9_-]+$`) before using
  them as cache directory names.
- **F-06 (Info, accepted)** — `add_service` uses `yaml.safe_dump`, which drops `doc-sync.yml`
  comments; documented tradeoff (structural, not text, merge).

### Changed
- `doc-sync.py` (host-forge-scripts) refactored to expose importable `sync_service()`,
  `load_config()`, and a shared `state_lock()`, and to enforce the allowlist at fetch time —
  CLI behaviour otherwise unchanged (regression-tested). The cron now depends on
  `doc-cache-allowlist.yml`.
