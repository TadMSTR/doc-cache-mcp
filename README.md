# doc-cache-mcp

A capability-scoped [FastMCP](https://github.com/jlowin/fastmcp) server for the forge
**documentation cache**. It exposes exactly three verbs — **list**, **add**, **sync** —
over the shared docs cache, and validates every source URL against an allowlist before it
can enter that cache.

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
| `doc_cache_list_services()` | Read-only. Lists each configured service, its topics/URLs, chunk counts, and last-synced date. |
| `doc_cache_add_service(service, entries)` | Registers a service + `[{topic, url}]`. **Validates every URL against the allowlist**, then does a structural YAML merge (dedup by topic), atomic write, and single-file git commit. Never fetches. |
| `doc_cache_sync(service, dry_run=False)` | Ingests/refreshes a configured service: fetch → convert → chunk → cache → index into memsearch. Service must already exist in config. |

## Source-URL allowlist (the security core)

Every URL passed to `doc_cache_add_service` is checked by
[`allowlist.py`](src/doc_cache_mcp/allowlist.py) before anything is written:

- Scheme **must** be `https`.
- **IP-literal hosts are rejected** — the allowlist is name-based.
- **Public hosts** must be on the host allowlist **and** every address they currently
  resolve to must be public (defeats DNS-rebind bypass).
- **Forge endpoints** (exact host + path prefix, e.g. `vikunja.helmforge.me/api/v1/docs.json`)
  are explicitly trusted and may resolve to private forge addresses — that is why they are
  listed individually.
- Anything else is refused (default-deny). A missing allowlist file denies everything.

The allowlist lives at `host-forge-scripts/doc-cache-allowlist.yml` (git-backed,
sysadmin-editable) and is re-read on every call, so edits take effect without a restart.

## Architecture

`doc-cache-mcp` does **not** re-implement chunking or shell out. It imports the shared
[`doc-sync.py`](https://gitea.tadmstr.me) logic (`sync_service()`) as the single source of
truth for fetch/convert/chunk/write, and calls it directly. The CLI (`doc-sync.py --service …`,
the `doc-sync-daily` cron) and the MCP share the same core and the same `flock` on the
state file, so they never race writes.

The server holds no generic file or command primitive. Its only write surface is
`doc-sync.yml` (structural merge + atomic write + single-file git commit) and the docs
cache directory (via `sync_service`). It binds loopback-only.

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
| `DOC_CACHE_MCP_GIT_COMMIT` | `true` | Commit `doc-sync.yml` after a successful add. |
| `DOC_CACHE_MCP_MAX_ENTRIES_PER_ADD` | `50` | Ceiling on source entries accepted in one `add_service` call. |
| `DOC_CACHE_MCP_GIT_PUSH` | `false` | Push that commit. **Off by default** — see below. |
| `DOC_CACHE_MCP_DEPLOY_KEY_PATH` | unset | ssh key used for the push. Required when `GIT_PUSH` is on. |
| `DOC_CACHE_MCP_PUSH_REMOTE` / `_PUSH_BRANCH` | `origin` / `main` | Push target. |
| `DOC_CACHE_MCP_REVIEW_BRANCH_PREFIX` | `doc-cache-mcp/review` | Where a guarded-off commit lands instead. |
| `DOC_CACHE_MCP_COMMIT_IDENTITY_NAME` / `_EMAIL` | `doc-cache-mcp` / `doc-cache-mcp@forge` | Identity for tool commits. |
| `DOC_CACHE_MCP_AUDIT_LOG_DIR` | unset | Append-only JSONL sink for push decisions. Unset = no audit events. |

### Pushing

`doc_cache_add_service` commits one file. Without `GIT_PUSH` that commit stays on the
local branch — which is a problem when the caller is an agent with no git write access of
its own, because it can never finish what the tool started. The tool reports
`committed: true` for an operation nobody completes.

Enabling the push means this server writes to a shared branch unattended, so it is off by
default and guarded when on. Every guard is fail-closed: one that cannot be *evaluated* is
a refusal, not a pass.

1. Commits are authored as the configured identity, not as the host user.
2. The push uses `DEPLOY_KEY_PATH` with `IdentitiesOnly=yes`, so ssh cannot fall back to
   the host user's ambient key. Use a key scoped to the single target repo.
3. Every commit in `<remote>/<branch>..HEAD` must be authored by that identity — the tool
   will not sweep up anyone else's unpushed work.
4. The push range must touch the config file and nothing else.
5. The config as it will land must be additive against the copy already on the remote: no
   service or topic removed, no existing URL re-pointed.
6. Each decision is appended to `AUDIT_LOG_DIR`, giving a trail outside git that survives
   a force-push.
7. When a guard trips, the commit is pushed to a **review branch** and the URL to open a
   pull request is returned — it degrades to human review, never to a stranded local
   commit or a silent failure.

Note that (5) means re-pointing an existing topic's URL, which `add_service` supports and
which is otherwise idempotent, will not auto-push; it lands on a review branch instead.
That is deliberate — a URL swap on an already-cached topic is what cache poisoning would
look like, and it is worth one human glance.

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

## License

MIT
