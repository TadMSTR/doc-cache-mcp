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

## License

MIT
