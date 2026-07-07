# AGENTS.md — doc-cache-mcp

Capability-scoped MCP for the forge docs cache. Replaces research's generic system-ops
grant (ADR-0005) with three verbs + a source-URL allowlist.

## Layout

- `src/doc_cache_mcp/server.py` — FastMCP server + the three tools + git-commit helper.
- `src/doc_cache_mcp/allowlist.py` — source-URL validation (SSRF / cache-poisoning guard).
  **The security core.** Changes here need a security re-look.
- `src/doc_cache_mcp/docsync.py` — loads the shared `doc-sync.py` by path (hyphenated name,
  outside any package).
- `src/doc_cache_mcp/config.py` — env-var settings (`DOC_CACHE_MCP_*`).
- `src/doc_cache_mcp/observability.py` — structlog + optional OTEL/InfluxDB.

## Invariants (do not regress)

- The server exposes **only** list/add/sync. Do not add a generic file/command tool.
- Every URL entering the cache goes through `allowlist.validate_url`. Never bypass it.
- `doc_cache_add_service` writes via structural YAML merge + atomic replace, never a text
  edit; the git commit is fixed-argv and scoped to the single config file.
- Chunking/fetch logic lives in `doc-sync.py` (single source of truth). Do not fork it here.
- `doc-sync.py` CLI behaviour must stay unchanged — the `doc-sync-daily` cron depends on it.

## Tests

`pytest`. Cover allowlist bypass cases, YAML merge/dedup idempotence, dry-run,
unknown-service, and the doc-sync CLI regression.

## Deploy

PM2 on `127.0.0.1:8503` (`ecosystem.config.js`). Manifest cutover (removing research's
`system-ops` grant) is a separate, security-gated sysadmin task — not part of this repo.
