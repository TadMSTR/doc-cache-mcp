# doc-cache-mcp

A capability-scoped [FastMCP](https://github.com/jlowin/fastmcp) server for the forge
**documentation cache**. It exposes five narrow verbs — **list**, **add**, **update**,
**remove**, **sync** — over the shared docs cache, and classifies every source URL against
an allowlist before it can enter that cache. Every write call finishes or fails, and says
which.

It exists to replace the research agent's generic `read_file` / `edit_file` /
`run_command` grant (ADR-0005) with a purpose-built tool. After cutover, research holds no
generic file/command primitive at all, and the docs cache gains source-URL validation the
old deny-filter model could not provide.

## Why

The old model gave research broad primitives (`read_file`/`edit_file`/`run_command` on
`homelab-ops-mcp`) fenced by two deny-list argument filters. That is the weaker pattern:

- `run_command` is arbitrary execution gated only by a regex on the command string.
- The broad primitives persist in research's toolset — any filter gap re-widens the blast
  radius.
- The path filter constrained *where* an edit landed, **not the YAML content** — research
  could write **any** `url:` into the trusted cache every agent searches. That is a live
  cache-poisoning / SSRF-shaped gap the filter approach had nowhere to close.

`doc-cache-mcp` flips this to **capability-scoped**: a narrow, typed surface, with URL
validation as a first-class control.

## Tools

| Tool | Behaviour |
|------|-----------|
| `doc_cache_list_services(service=None, detail=False)` | Read-only. By default a compact **health summary**: per service its topic count, chunk total and worst status, plus `health` (topic counts by status) and `failing` (each failing topic with its last error). `service=` or `detail=true` gives per-topic rows: `status`, `last_success`, `last_error`, `consecutive_failures`, `trust`. |
| `doc_cache_add_service(service, entries)` | Registers **new** topics `[{topic, url}]`. Strictly additive: an existing topic with the same URL is `unchanged`; one with a different URL is refused with a pointer to `update_topic`. Warns when a URL is already registered under another topic. Never fetches. |
| `doc_cache_update_topic(service, topic, url)` | Repoints one existing topic. Same URL checks as add. |
| `doc_cache_remove_topic(service, topics, remove_service=False)` | Removes topics (at most 20 per call), then their cached chunks in both tiers, their sync state, and their metadata-index rows. Emptying a service needs `remove_service=true`. |
| `doc_cache_sync(service, dry_run=False)` | Ingests/refreshes a configured service: fetch → convert → chunk → cache. Each result carries `status` (`ok` / `failing` / `expired_provisional`), `trust`, and on failure the real `error`. The docs become searchable in qmd after the next hourly `qmd-refresh` (at :00). |

Topic statuses: `ok`; `stale` (last success over 3 days ago); `failing` (its last sync
failed; `last_success` keeps the date of the last good one); `never_synced`;
`expired_provisional` (an unvetted source past its TTL, no longer synced).

### Outcomes

Every write tool returns `outcome`, plus `sha`, `change` (`additive` / `repoint` /
`remove`) and `reason`. `reason` describes **this call only**.

| `outcome` | Meaning |
|-----------|---------|
| `pushed` | Committed and on the remote branch. |
| `committed_local` | Committed; pushing is disabled (`GIT_PUSH=false`, the default). |
| `written` | File written; git is disabled (`GIT_COMMIT=false`). |
| `unchanged` | The requested state already holds. Nothing written. |
| `rolled_back` | A step after the write failed (e.g. the push was rejected). The commit and the file were undone; nothing is left behind. |
| `refused` | An input, policy or precondition check failed. Nothing was written. |
| `stranded` | The push failed **and** rolling back was unsafe (someone committed concurrently). `reason` says exactly what is left where; the next call refuses until it is reconciled. |

## Source-URL policy (the security core)

Every URL is classified by [`allowlist.py`](src/doc_cache_mcp/allowlist.py) — at add time
by this server, and at fetch time, per redirect hop, by `doc-sync.py`, which imports a
generated copy of the same module. Evaluation order; **deny wins**:

1. **Structural.** `https` only; a host *name* (IP literals refused); no userinfo; port 443
   or none.
2. **Deny**, absolute, every tier:
   - *Open-edit sources* — public wikis anyone can edit (GitHub/GitLab wikis, `wiki.atom`
     feeds, `wiki.debian.org`). A **built-in floor** in code: the allowlist's
     `deny.open_edit` can add to it, never remove from it. Paths are percent-decoded
     (repeatedly), slash-collapsed, dot-resolved and lowercased before matching.
   - *Path shapes* — `deny.path_shapes` names the only paths a host may be fetched on
     (e.g. `github.com` only on `/<o>/<r>/(blob|raw|tree|releases)/…`, not issues or PRs).
3. **Forge endpoints** (exact host + boundary-checked path prefix) are trusted and may
   resolve to private forge addresses.
4. **`hosts`** are trusted. A leading dot is a domain suffix: `.opensearch.org` matches
   `opensearch.org` and `docs.opensearch.org`, never `evilopensearch.org`.
5. **Provisional**: any other host, if `provisional.enabled` and the host is not on
   `provisional_exclude`.
6. Every public result, trusted or provisional, must resolve **only** to public addresses
   (DNS-rebind guard), re-checked per redirect hop immediately before the request.

A missing or malformed allowlist denies everything. A missing `provisional` block means
**disabled**, so a v1 file (`hosts` + `forge_endpoints`) behaves exactly as before.

### Trust tiers

The cache is injected into every agent as documentation, and research picks URLs from
web search — untrusted input. So the allowlist's job is **content trust**, not only SSRF
(the resolve-and-recheck guard does not depend on it).

- **Trusted** content (allowlisted hosts) is cached at `~/.claude/memory/docs/<service>/`.
- **Provisional** content (any other public host) is cached at
  `~/.claude/docs-provisional/<service>/` — deliberately outside `~/.claude/memory`, so it
  is never indexed or promoted as agent memory — and its chunks carry `trust: provisional`.
  Ceilings: `ttl_days` (default 30; past it an unpromoted topic is skipped and its chunks
  removed, without failing the run), `max_hosts` (default 25 distinct hosts; adds past it
  are refused), and `max_bytes` per fetch (default 2 MiB; larger bodies are refused, not
  truncated).
- **A redirect never changes a topic's tier.** A provisional URL that redirects to a
  trusted host is still provisional content. A trusted URL that redirects to an unvetted
  host is **refused**, as v1 did: it would otherwise be unvetted content that the TTL,
  `max_hosts` and the promotion event never see, because all three key on the configured
  URL. Register the target URL itself if it should be cached.
- **Promotion is one line** in `hosts`. The `trust:` / `added:` fields written into
  `doc-sync.yml` are informational: trust is recomputed from the allowlist on every add
  and every fetch, and the next sync moves the chunks to the trusted directory.
- Every provisional add emits a `doc-cache.provisional.added` event (host, service, topics)
  to `AUDIT_LOG_DIR` — the promotion queue.
- **The TTL bounds idle content, not intent.** Removing and re-adding a topic, or adding it
  under a new name, starts a fresh TTL. Each re-add emits the event, so the promotion queue
  is the real bound.
- **Demoting** a host (removing it from `hosts`) expires its topics and deletes their
  chunks from both directories on the next sync.

### Allowlist schema (v2)

```yaml
hosts: [docs.docker.com, .opensearch.org]            # leading dot = domain suffix
forge_endpoints: [vikunja.example.internal/api/v1/docs.json]
deny:
  open_edit:                 # added to the built-in floor; cannot remove from it
    docs.example.com: ['^/community/']
  path_shapes:               # the ONLY paths these hosts may be fetched on
    github.com: ['^/[^/]+/[^/]+/(blob|raw|tree|releases)/']
provisional:                 # absent = disabled (v1 behaviour)
  enabled: true
  ttl_days: 30
  max_hosts: 25
  max_bytes: 2097152
provisional_exclude: [pastebin.com, .ngrok-free.app]   # never self-service
```

The allowlist lives at `host-forge-scripts/doc-cache-allowlist.yml` (git-backed) and is
re-read on every call, so edits take effect without a restart.

## Architecture

`doc-cache-mcp` does **not** re-implement chunking or shell out. It imports the shared
[`doc-sync.py`](https://gitea.tadmstr.me) logic (`sync_service()`) as the single source of
truth for fetch/convert/chunk/write, and calls it directly. The CLI (`doc-sync.py --service …`,
the `doc-sync-daily` cron) and the MCP share the same core and the same `flock` on the
state file, so they never race writes.

The server holds no generic file or command primitive. Its only write surface is
`doc-sync.yml` (structural edit + atomic write + single-file git commit) and the two docs
cache directories (via `doc-sync.py`'s `sync_service` / `remove_topics`). It binds
loopback-only.

## Configuration

Environment variables (prefix `DOC_CACHE_MCP_`):

| Var | Default | Meaning |
|-----|---------|---------|
| `DOC_CACHE_MCP_TRANSPORT` | `http` | `http` (streamable-http) or `stdio`. |
| `DOC_CACHE_MCP_HOST` | `127.0.0.1` | Bind host. Loopback only by design. |
| `DOC_CACHE_MCP_PORT` | `8503` | Bind port. |
| `DOC_CACHE_MCP_DOCSYNC_PATH` | `~/scripts/doc-sync.py` | Shared doc-sync logic to import. |
| `DOC_CACHE_MCP_CONFIG_PATH` | `~/docs/doc-sync.yml` | Docs cache config the add-tool edits. |
| `DOC_CACHE_MCP_ALLOWLIST_PATH` | `host-forge-scripts/doc-cache-allowlist.yml` | Source-URL allowlist. |
| `DOC_CACHE_MCP_GIT_COMMIT` | `true` | Commit `doc-sync.yml` after each change. |
| `DOC_CACHE_MCP_MAX_ENTRIES_PER_ADD` | `50` | Ceiling on source entries accepted in one `add_service` call. |
| `DOC_CACHE_MCP_MAX_REMOVE_PER_CALL` | `20` | Ceiling on topics removed in one `remove_topic` call. |
| `DOC_CACHE_MCP_PROVISIONAL_CACHE_DIR` | `~/.claude/docs-provisional` | Where provisional chunks go. `doc-sync.py` reads the same variable with the same default. Keep it outside any directory indexed as agent memory. |
| `DOC_CACHE_MCP_GIT_PUSH` | `false` | Push that commit. **Off by default** — see below. |
| `DOC_CACHE_MCP_DEPLOY_KEY_PATH` | unset | ssh key used for the push. Required when `GIT_PUSH` is on. |
| `DOC_CACHE_MCP_PUSH_REMOTE` / `_PUSH_BRANCH` | `origin` / `main` | Push target. |
| `DOC_CACHE_MCP_COMMIT_IDENTITY_NAME` / `_EMAIL` | `doc-cache-mcp` / `doc-cache-mcp@forge` | Identity for tool commits. |
| `DOC_CACHE_MCP_AUDIT_LOG_DIR` | unset | Append-only JSONL sink (agent-bus shape) for every write decision and every provisional add. Unset = no audit events. |

### Pushing

> **`DOC_CACHE_MCP_GIT_PUSH` is `false` by default. Turning it on gives this MCP
> unattended write access to a shared repo's `main`.** Only enable it if you have read
> the sequence below and provisioned a scoped deploy key.

Without `GIT_PUSH` a commit stays on the local branch (`committed_local`). That is a
problem when the caller is an agent with no git write access of its own: it can never
finish what the tool started. With it, each write call is one sequence, serialised in the
server, and every step is fail-closed — a check that cannot be *evaluated* is a refusal:

1. **Precondition, before writing.** `doc-sync.yml` has no uncommitted changes, the
   checkout is on the push branch, and after a fetch it is exactly level with
   `<remote>/<branch>`. Otherwise `refused`, naming the condition (e.g. "local main is 2
   behind origin/main"). Nothing is written.
2. **Commit** the one file, authored as the configured identity, not the host user.
3. **Classify** the commit as `additive`, `repoint` or `remove`, and roll back if it is not
   the kind the calling tool makes (add only adds, update only repoints, remove only
   removes).
4. **Guards:** every commit in `<remote>/<branch>..HEAD` is the tool's (nobody else's
   unpushed work rides along), and the range touches the config file and nothing else.
5. **Push the SHA the guards inspected**, never the `HEAD` ref, using `DEPLOY_KEY_PATH`
   with `-F /dev/null`, `IdentitiesOnly=yes` and no ssh agent. `IdentitiesOnly` alone still
   offers `IdentityFile`s from `~/.ssh/config`; not reading the config at all is what makes
   the deploy key the only identity, so the push URL must carry the user and port. Use a
   key scoped to the single target repo.
6. **If the push fails, roll back this call's commit** — a compare-and-swap of the branch
   from exactly this commit to exactly its parent, then a restore of the file if nobody
   edited it since. A concurrent commit makes the swap fail and nothing is reset: the
   outcome is `stranded`, and the next call's precondition refuses until a human
   reconciles it. A push that timed out is checked against the remote first.
7. **Audit** every decision to `AUDIT_LOG_DIR`, a trail outside git that survives a
   force-push.

There is no review branch. Up to v0.2 a guard refusal pushed the commit to a review branch
and left it on local `main`, where it put every later call's push range — and its
`reason` — in the same state. Git history, the audit trail and the agent-bus event make
each change attributable and revertible after the fact; repoints and removals come only
from the explicit tools, so they are intended.

The deploy key should be mode `0600` and owned by the user the server runs as.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Deployment (forge)

PM2 process on `127.0.0.1:8503` via `ecosystem.config.js`. See the build plan for the
manifest cutover (removing research's `system-ops` grant) — that is a separate, gated
sysadmin step.

## Observability

Structured logging (structlog, JSON to stdout) is always on. Two optional backends are each
gated on their own env var and disabled — with no import cost — when unset. Install the extras
with `pip install ".[telemetry]"`.

| Var | Enables |
|-----|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP span export (e.g. `http://127.0.0.1:4317`). `service.name` is `doc-cache-mcp`. |
| `INFLUXDB_URL` | Best-effort per-call metric emission to InfluxDB 3. |
| `INFLUXDB_TOKEN` | Auth token for the InfluxDB backend. |
| `INFLUXDB_BUCKET` | Target database/bucket (default `doc-cache-mcp`). |

### Failure behaviour

Metric emission is best-effort and never fails a tool call, but it is **not silent**. An
unset `INFLUXDB_URL` is the intended disabled state and logs nothing. A backend that is
*configured and failing* warns exactly once per process:

| Event | Meaning |
|-------|---------|
| `influx_init_failed` | `INFLUXDB_URL` is set but the client could not be built (usually the `[telemetry]` extra is missing). Writes are disabled for the process and not retried. |
| `influx_write_failed` | The client built but a write failed. This — not `influx_init_failed` — is what a wrong or unreachable `INFLUXDB_URL` looks like, because `InfluxDBClient3` connects lazily and so builds successfully against any host. Writes keep being attempted, since a write failure is often transient. |

Both carry the exception *class* only, never the URL or the token.

The audit sink is held to the same rule. A write to `AUDIT_LOG_DIR` that fails never
blocks a change, but it is logged at error level (`audit_sink_write_failed`), surfaced as
`audit_error` in the tool result, and counted in `doc_cache_audit_sink_errors_total`.

## License

MIT
