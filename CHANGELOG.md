# Changelog

All notable changes to doc-cache-mcp are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- Initial build (Phase 1). FastMCP server on `127.0.0.1:8503` exposing three verbs:
  `doc_cache_list_services`, `doc_cache_add_service`, `doc_cache_sync`.
- Source-URL allowlist (`allowlist.py`): https-only, name-based host allowlist, explicit
  forge endpoints, IP-literal rejection, and DNS-rebind re-check (SSRF / cache-poisoning
  guard).
- Structural YAML merge + atomic write + single-file git commit for `doc_cache_add_service`.
- Shared `flock` on the doc-sync state file so the MCP and the `doc-sync-daily` cron cannot
  race writes.
- Imports the shared `doc-sync.py` `sync_service()` as the single source of truth (no
  re-implemented chunking, no shelling out for its own logic).

### Changed
- `doc-sync.py` (host-forge-scripts) refactored to expose importable `sync_service()`,
  `load_config()`, and a shared `state_lock()` — CLI behaviour unchanged (regression-tested).
