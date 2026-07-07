# Changelog

All notable changes to doc-cache-mcp are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
