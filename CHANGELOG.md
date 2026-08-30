# Changelog

All notable changes to doc-cache-mcp are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.1] — 2026-08-30

Telemetry that fails is now visible. Ports the InfluxDB half of the fixes proven in
dockhand-mcp v0.4.0 (TadMSTR/dockhand-mcp#6) — vikunja#575 item 3, #580.

Verified against a **genuinely dead backend**, not mocks: both of the defects in #580 were
invisible to mocked tests. Measured on 2026-08-30 with `INFLUXDB_URL=http://127.0.0.1:9/nope`,
five `emit_metric` calls produced **0** log lines before this change and exactly **1** after.

### Fixed
- **A configured-but-failing InfluxDB was completely silent** — `_get_influx()` swallowed
  every exception with `except Exception: pass`, leaving the client global at `None`, so
  nothing in the logs ever mentioned the broken backend and every subsequent `emit_metric()`
  re-entered the connect path. It now warns once and sets a negative-cache sentinel. An
  *unset* `INFLUXDB_URL` remains the intended disabled state and stays silent — "never tried"
  and "tried and failed" are distinct flags, not an overloaded `None`.
- **The write path was the half that actually mattered** (#580). `InfluxDBClient3` constructs
  lazily and never contacts the host, so against an unreachable URL `_get_influx()`
  *succeeds* and an init-only sentinel never fires — verified live against
  `http://127.0.0.1:9/nope`, where the client built fine and every `write()` raised
  `NewConnectionError` into a bare `except Exception: pass`. `emit_metric` now warns once on
  a failed write while continuing to attempt them, since a write failure is often transient.

### Security
- The new warnings carry the exception *class* only, never `str(exc)`, the URL or the token —
  an InfluxDB error can echo either. `exc_info=True` is kept **only** on `otel_init_failed`,
  whose config is a bare endpoint URL with no credential and whose try block spans five
  imports plus an exporter build; a test pins that exemption set at exactly one site. This
  follows the dockhand-mcp audit split from 2026-08-29 (LOW-1).

### Unchanged, deliberately
- **No third-party logger demotion.** dockhand-mcp and backrest-mcp needed one because they
  route stdlib logging through a root handler at `LOG_LEVEL`, so `httpx`/`mcp`/`nats`
  inherited it and drowned the app's own lines. This server's `configure_logging()` is
  `structlog.configure()` with a `PrintLoggerFactory` — no root `setLevel`, no stdlib
  handler — and its logs contain zero `"logger":` keys, confirming nothing routes through
  stdlib. Adding a demotion here would be a change with no defect behind it.
  `test_configure_logging_does_not_touch_stdlib_logging` pins that premise, so if the
  logging setup ever moves to stdlib routing, the test says so.

### Tests
- New `tests/test_observability.py` — 11 tests covering the init sentinel, the write
  warning, the lazy-client mechanism behind #580, and the no-credential-in-logs rule.
  `influxdb_client_3` is faked via `sys.modules` rather than imported, because CI installs
  only `.[dev]` — a test that needed the real package would skip exactly where it matters
  most.

## [0.2.0] — 2026-08-13

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

  Security audit of that module found two Medium issues, both fixed before release: the
  push sent the `HEAD` *ref* rather than the SHA the guards had inspected, so a commit
  landing in that window rode along unvalidated (reproduced, then closed by pinning the
  tip); and an exception from the push subprocess escaped and was relabelled upstream as
  a commit failure, reporting `committed: false` for a commit that was on disk. A guard
  that is skipped rather than passed is now reported in `notes` instead of being
  indistinguishable from a pass.
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
- `__version__` said `0.1.0` while `pyproject.toml` said `0.1.1`. `server.py` logs
  `__version__` at startup, so the running server reported a version it was not. Both are
  now `0.2.0`, and a parity test reads `pyproject.toml` directly — `importlib.metadata`
  reflects the last install, so it can pass on an editable venv while the file on disk
  disagrees, which is the drift this needs to catch.

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
