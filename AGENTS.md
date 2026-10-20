# AGENTS.md — doc-cache-mcp

Capability-scoped MCP for the forge docs cache. Replaces research's generic system-ops
grant (ADR-0005) with five narrow verbs + a tiered source-URL policy.

## Layout

- `src/doc_cache_mcp/server.py` — FastMCP server + the five tools.
- `src/doc_cache_mcp/push.py` — the write path: precondition, commit, classify, guards,
  push, rollback. Every call ends in one `outcome`.
- `src/doc_cache_mcp/allowlist.py` — source-URL classification (SSRF, cache-poisoning and
  content-trust policy; open-edit floor; provisional tier). **The security core.** Changes
  here need a security re-look, and a regenerated vendored copy in host-forge/scripts
  (`python -m doc_cache_mcp.vendoring --write`).
- `src/doc_cache_mcp/docsync.py` — loads the shared `doc-sync.py` by path (hyphenated name,
  outside any package).
- `src/doc_cache_mcp/config.py` — env-var settings (`DOC_CACHE_MCP_*`).
- `src/doc_cache_mcp/observability.py` — structlog + optional OTEL/InfluxDB.

## Invariants (do not regress)

- The server exposes **only** list/add/update/remove/sync. Do not add a generic
  file/command tool.
- Every URL entering the config or the cache goes through `allowlist.classify_url`. Never
  bypass it. Deny is evaluated first and nothing later re-admits a denied URL. The
  open-edit floor lives in code and the allowlist file can only add to it.
- Config writes go through `push.commit_change` after `push.precondition`, under the
  server's write lock: structural YAML edit + atomic replace, never a text edit; fixed-argv
  git scoped to the single config file. Never reintroduce a path that leaves a commit on
  local `main` without saying so in `outcome`.
- A rollback only ever moves the ref by compare-and-swap from this call's commit to its
  parent. Never `git reset` in the shared checkout.
- Provisional content never goes under `~/.claude/memory` (qmd indexes that tree as agent
  memory).
- Chunking/fetch logic lives in `doc-sync.py` (single source of truth). Do not fork it here.
- `doc-sync.py` CLI behaviour must stay unchanged — the `doc-sync-daily` cron depends on it.

## Tests

`pytest`. Cover allowlist bypass cases, YAML merge/dedup idempotence, dry-run,
unknown-service, and the doc-sync CLI regression.

Tests that load the live `~/scripts/doc-sync.py` skip off-forge, including in GitHub CI.
`tests/fixtures/doc_sync_indexfree.py` is a verbatim copy of that script below a fixture
header, loaded through the real `load_doc_sync()` so CI exercises a real doc-sync.
A forge-local test fails if it stops mirroring the live script; re-copy it then, and don't
reformat it (ruff excludes `tests/fixtures`).

## Deploy

PM2 on `127.0.0.1:8503` (`ecosystem.config.js`). Manifest cutover (removing research's
`system-ops` grant) is a separate, security-gated sysadmin task — not part of this repo.
