# Changelog

All notable changes to doc-cache-mcp are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.0] — 2026-10-19

Every doc-cache call now finishes or fails, and says which. Topics can be repointed and
removed through the tool. A failing topic shows up in the listing. A provisional trust tier
lets research cache a new vendor's docs without an operator commit, labelled as unvetted.
This is doc-cache-friction-2026-10 part 1 (vikunja#653; folds #457, #647, #1167, #380 code
half, #581 doc-cache half). **Needs host-forge/scripts with the matching `doc-sync.py`**
(failure state, `remove_topics`, provisional tier, regenerated `doc_cache_allowlist.py`).

### Fixed
- **A single repoint no longer poisons every later push** (#653, #457). v0.2 degraded any
  guard refusal to a pushed review branch and left the commit on local `main`. Every later
  call's push range then contained it, so every later call degraded too, reporting
  `pushed: false` with a `reason` that described the earlier call. Reproduced on 0.2.2: a
  purely additive add returned "refuses to change the url of existing topic 't'".
- **A failed sync is recorded instead of freezing `last_synced`** (#647). Needs the
  matching doc-sync.py: state now carries `last_attempt`, `last_error` and
  `consecutive_failures`, and `synced` stays the date of the last success. On 2026-10-19
  the listing showed 0 failing topics while that night's run had 15 errors.
- **Open-edit wikis are refused at add time**, not only at fetch time (#1167). The rule set
  moved from doc-sync.py into `allowlist.py`, so both boundaries run the same code.
- **The audit sink failing is counted**: `doc_cache_audit_sink_errors_total` plus an
  ERROR log, alongside the existing `audit_error` in the result (#380, code half).
- `.gitignore` ignores `core.[0-9]*` instead of `core.*`, which also matched source files
  (#581).

### Changed — breaking
- **Write tools return `outcome`** — `pushed`, `committed_local`, `written`, `unchanged`,
  `rolled_back`, `refused` or `stranded` — with `sha`, `change` and `reason` at the top
  level. The `commit` sub-dict, `pushed`, `pushed_sha`, `review_branch` and `pr_url` are
  gone. Refusals keep an `error` key alongside `reason`.
- **The review-branch path is removed.** A call either pushes or rolls back its own commit
  (compare-and-swap on the ref, never a reset). Before writing, the checkout must be clean
  for `doc-sync.yml` and level with the remote; otherwise the call is refused and names the
  condition. `DOC_CACHE_MCP_REVIEW_BRANCH_PREFIX` is removed.
- **The additive-only rule is no longer a push blocker.** Each tool is held to its own kind
  of change instead: add only adds, update only repoints, remove only removes. Anything
  else is rolled back.
- **`doc_cache_add_service` is strictly additive.** An existing topic with a different URL
  is refused with a pointer to `doc_cache_update_topic`; the same URL is `unchanged`. A URL
  already registered under another topic of the service gets a warning.
- **`doc_cache_list_services` returns a health summary by default**: per service (keyed by
  name) its topic count, chunk total and worst status, plus `health` and `failing`. It
  stays under 10 KB at 104 services; the old full listing was 56.6 KB. `service=` or
  `detail=true` gives per-topic rows with `status`, `last_success`, `last_error`,
  `consecutive_failures` and `trust`.
- **URLs with userinfo or a port other than 443 are refused.**

### Added
- `doc_cache_update_topic(service, topic, url)` and
  `doc_cache_remove_topic(service, topics, remove_service=False)` (at most 20 per call;
  removes chunks in both tiers, sync state and metadata-index rows via doc-sync's
  `remove_topics`).
- **Provisional trust tier** (D1 = A). With `provisional.enabled` in the allowlist, any
  public host not on `provisional_exclude` is cached to `~/.claude/docs-provisional/`
  (outside `~/.claude/memory`) with `trust: provisional` frontmatter, capped by
  `max_hosts`, `ttl_days` and `max_bytes`. A redirect never raises trust. Promotion is one
  line in `hosts`. Each provisional add emits `doc-cache.provisional.added`. A v1 allowlist
  (no `provisional` block) behaves exactly as before.
- **Allowlist v2:** leading-dot suffix hosts, `deny.open_edit` (adds to the built-in
  floor), `deny.path_shapes`, `provisional`, `provisional_exclude`.
- `classify_url`, `host_tier`, `lower_trust` in `allowlist.py`. `validate_url` keeps its
  meaning: it accepts only what `classify_url` calls trusted.
- Settings `DOC_CACHE_MCP_PROVISIONAL_CACHE_DIR` and `DOC_CACHE_MCP_MAX_REMOVE_PER_CALL`.

### Security (audit round 1 remediation)
- **`urllib3>=2.8.0`** (F-01, Medium). 2.7.0's streaming API had an infinite loop and an
  unbounded chunk-size buffer that a hostile provisional server could trigger through the
  capped read. The deployed venvs still need upgrading before the provisional tier is
  enabled.
- **A trusted URL that redirects to an unvetted host is refused** (F-02). It used to be
  cached as provisional with no TTL, no `max_hosts` count and no promotion event. The
  listing's `trust` now reads the tier doc-sync recorded.
- **ssh runs with `-F /dev/null`** (F-03). `IdentitiesOnly=yes` alone still offered the
  `IdentityFile` from `~/.ssh/config`: on forge, the host user's account key.
- **A provisional fetch is refused if TLS verification is off** (F-04). Certificate
  verification is what stops a DNS rebind between the recheck and the connection;
  SECURITY.md now says so instead of calling the TOCTOU closed.
- **Demoting a host deletes its trusted chunks too** (F-05).
- **`remove_topic` can retry a failed cache cleanup** (F-06): a topic gone from the config
  but still in sync state is cleaned up, `outcome: unchanged`, under `orphans`.
- **A rejected push is checked against the remote before rolling back** (F-07), and git
  runs in its own process group so a timeout also kills its ssh child.
- **`fec0::/10` is refused** (F-08); the previous comment claimed it already was.
- The rollback's file-restore check also compares the index (F-10). Refusals no longer echo
  the private address a name resolved to (F-11). A `;` anywhere in the path is refused,
  raw or percent-encoded; mid-path it got past two boundary-anchored floor rules (F-12,
  R2-04).
- **Round 2:** an allowlist that enables provisional but lists no hosts and no forge
  endpoints is refused, since every topic would expire (R2-01). A landed push counts as
  landed if the remote has moved past it (R2-05, `merge-base --is-ancestor`). A whole-service
  removal can be retried with an empty `topics` list (R2-03).

### Tests
- The new tests were run against 0.2.2 first: 38 of the 39 server-level ones fail there.
  `test_push_guards.py` uses real git with a pre-receive hook to reject pushes, and a real
  concurrent commit to prove the rollback never discards someone else's work.
- `tests/fixtures/doc_sync_indexfree.py` is re-copied from the new doc-sync.py, and its
  drift guard now expects an exact API match (the index step is gone from both).
- The forge-local SSRF tests pin their allowlist. They set only the cache, so they had been
  reloading whatever allowlist file the host had.

## [0.2.2] — 2026-09-29

doc-cache-mcp no longer depends on doc-sync.py's memsearch index step (vikunja#921). This is
memsearch-retirement-finish-2026-09 part 1. Part 2 removes that step from doc-sync.py.

### Fixed
- **A clean sync is `ok: true` whether or not doc-sync still has an index step.** memsearch
  was retired on 2026-09-17, so every `doc_cache_sync` has since returned `ok: false` with
  `index_error: "memsearch index exited 1 — docs are cached but NOT searchable…"`. Neither
  half was true: the docs were cached, and qmd-refresh indexes `~/.claude/memory/docs/`
  every hour. The server now treats `indexed` / `index_error` as optional. `ok` from
  doc-sync is passed through, and derived from `errors` only when doc-sync omits it. An
  `index_error`, when doc-sync reports one, still clears `ok` (vikunja#372).
  - The live result keeps today's `index_error` until part 2 lands. This release makes the
    server correct for both shapes. It doesn't change what the current doc-sync returns.
- **The tool description and progress message no longer talk about memsearch.** They say
  the docs are cached and become searchable in qmd after the next hourly `qmd-refresh`.

### Changed
- **The `index_failed` metric is emitted only when doc-sync reports an index result.**
  After part 2 it disappears from `doc_cache_tool` points for `tool=sync`, rather than
  sitting at a constant 0 that reads as healthy indexing. Dashboards that plot it will
  stop getting new points.
- A sync with fetch errors now logs `doc_cache_sync` at error level. Before, only an index
  failure did, and a sync with every source failing logged at info.

### Tests
- **CI now runs doc_cache_sync against a real doc-sync.py.** Every test that loaded
  `~/scripts/doc-sync.py` skipped in GitHub CI, so CI stayed green while the forge-local
  suite asserted doc-sync's index internals, and it would have stayed green when part 2
  broke them. `tests/fixtures/doc_sync_indexfree.py` is the live script without its index
  step. `test_indexfree_doc_sync.py` loads it through `load_doc_sync()` under a throwaway
  `$HOME`, unskipped, with only the network stubbed. A forge-local drift guard fails if the
  fixture stops mirroring the live script's API less the index step.
- `test_doc_sync_cli.py` no longer requires `run_memsearch_index` or an `index` kwarg. The
  server never passes one.
- `test_index_failure_reporting.py` now tests the server-level contract with fakes: an
  `index_error` is surfaced and clears `ok`, and a result with no index keys is `ok`. It
  used to drive the live doc-sync's memsearch subprocess and CLI exit code, which is
  doc-sync's own behaviour. That behaviour is removed in part 2.

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
