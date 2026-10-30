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

| Tool | Description | Key parameters | Returns |
|------|-------------|----------------|---------|
| `doc_cache_list_services` | Read-only. By default a compact **health summary** of the cache. Call it before add/update/remove/sync. | `service` (optional; implies per-topic detail), `detail` (default `false`) | Summary: `services` (per service its topic count, chunk total and worst status), `health` (topic counts by status), `failing` (each failing topic with its last error). Detail: per-topic rows with `topic`, `url`, `status`, `chunks`, `last_success`, `last_error`, `consecutive_failures`, `trust`. |
| `doc_cache_add_service` | Registers **new** topics, creating the service if needed. Strictly additive: an existing topic with the same URL is `unchanged`; one with a different URL is refused with a pointer to `doc_cache_update_topic`. All-or-nothing: one bad entry refuses the call. Never fetches. | `service` (`^[A-Za-z0-9_-]+$`), `entries` (list of `{topic, url}`, at most `MAX_ENTRIES_PER_ADD`; `topic` is `^[A-Za-z0-9._-]+$`) | `outcome`, `sha`, `change`, `reason`, plus `added`, `provisional` and any `warnings` (e.g. the URL is already registered under another topic). |
| `doc_cache_update_topic` | Repoints one existing topic. The new URL is classified exactly as add classifies one. The topic keeps its name and position; a provisional topic keeps its original `added` date, so a repoint does not restart its TTL. | `service`, `topic`, `url` | `outcome`, `sha`, `change`, `reason`, `previous_url`. |
| `doc_cache_remove_topic` | Removes topics, then (only if the config change lands) their cached chunks in both tiers, their sync state and their metadata-index rows. All-or-nothing. Removing a service's last topic needs `remove_service=true`. If `cache.ok` is false, call again with the same topics to retry the cleanup. | `service`, `topics` (at most `MAX_REMOVE_PER_CALL`; empty only with `remove_service=true`), `remove_service` (default `false`) | `outcome`, `sha`, `change`, `reason`, plus `cache` (the cleanup half) and `orphans` on a retry. |
| `doc_cache_sync` | Ingests/refreshes a configured service: fetch, convert, chunk, write to the cache. Sends a progress notification every 15 s while it runs. Indexes nothing: the docs become searchable in qmd after the next hourly `qmd-refresh` (at :00). | `service` (must already exist in `doc-sync.yml`), `dry_run` (default `false`) | `ok` (false if any source failed), `errors`, `results` (each with `status` `ok` / `failing` / `expired_provisional`, `trust`, and on failure the real `error`). A call that cannot run returns `outcome: refused` with a `reason`. |

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

1. **Structural.** `https` only; a host *name* (IP literals refused, including the numeric
   IPv4 forms `inet_aton` accepts: `16843009`, `0x01010101`, `1.1`); no userinfo; port 443
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

The allowlist is a git-backed YAML file (`DOC_CACHE_MCP_ALLOWLIST_PATH`), re-read on every
call, so edits take effect without a restart. `doc-sync.py` enforces the same policy at fetch
time through `doc_cache_allowlist.py`, a copy of `allowlist.py` generated next to it with
`python -m doc_cache_mcp.vendoring --write`. Regenerate it after any change to
`allowlist.py`, and commit it wherever `doc-sync.py` lives. Upgrading to 0.3.1 without
regenerating it leaves fetch time admitting IP-literal hosts.

## Architecture

`doc-cache-mcp` does **not** re-implement chunking or shell out. It imports the shared
`doc-sync.py` logic (`sync_service()`, `remove_topics()`) by path as the single source of
truth for fetch/convert/chunk/write, and calls it directly. The CLI (`doc-sync.py --service …`,
the `doc-sync-daily` cron) and the MCP share the same core and the same `flock` on the
state file, so they never race writes.

The server holds no generic file or command primitive. Its only write surface is
`doc-sync.yml` (structural edit + atomic write + single-file git commit) and the two docs
cache directories (via `doc-sync.py`'s `sync_service` / `remove_topics`). It binds
loopback-only.

## Configuration

Environment variables (prefix `DOC_CACHE_MCP_`). A `.env` file in the working directory is
also read. None is strictly required, but the path defaults assume the layout below; set
them to match yours.

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `DOC_CACHE_MCP_TRANSPORT` | no | `http` | `http` (streamable-http, stateless) or `stdio`. |
| `DOC_CACHE_MCP_HOST` | no | `127.0.0.1` | Bind host. Loopback only by design. |
| `DOC_CACHE_MCP_PORT` | no | `8503` | Bind port. |
| `DOC_CACHE_MCP_DOCSYNC_PATH` | no | `~/scripts/doc-sync.py` | Shared doc-sync logic to import. |
| `DOC_CACHE_MCP_CONFIG_PATH` | no | `~/docs/doc-sync.yml` | Docs cache config the write tools edit (symlink resolved before writing). |
| `DOC_CACHE_MCP_ALLOWLIST_PATH` | no | `~/repos/gitea/host-forge-scripts/doc-cache-allowlist.yml` | Source-URL allowlist. Missing or malformed denies everything. |
| `DOC_CACHE_MCP_PROVISIONAL_CACHE_DIR` | no | `~/.claude/docs-provisional` | Where provisional chunks go. `doc-sync.py` reads the same variable with the same default. Keep it outside any directory indexed as agent memory. |
| `DOC_CACHE_MCP_GIT_COMMIT` | no | `true` | Commit `doc-sync.yml` after each change. `false` writes the file only (`outcome: written`). |
| `DOC_CACHE_MCP_GIT_PUSH` | no | `false` | Push that commit. **Off by default**; see [Pushing](#pushing). |
| `DOC_CACHE_MCP_DEPLOY_KEY_PATH` | when `GIT_PUSH` is on | unset | ssh key used for the push. With `GIT_PUSH` on and no key at this path, every write is refused. |
| `DOC_CACHE_MCP_PUSH_REMOTE` / `_PUSH_BRANCH` | no | `origin` / `main` | Push target. |
| `DOC_CACHE_MCP_COMMIT_IDENTITY_NAME` / `_EMAIL` | no | `doc-cache-mcp` / `doc-cache-mcp@forge` | Identity for tool commits. |
| `DOC_CACHE_MCP_AUDIT_LOG_DIR` | no | unset | Append-only JSONL sink (agent-bus shape) for every write decision and every provisional add. Unset = no audit events. |
| `DOC_CACHE_MCP_MAX_ENTRIES_PER_ADD` | no | `50` | Ceiling on source entries accepted in one `add_service` call. |
| `DOC_CACHE_MCP_MAX_REMOVE_PER_CALL` | no | `20` | Ceiling on topics removed in one `remove_topic` call. |

The provisional tier's ceilings (`ttl_days`, `max_hosts`, `max_bytes`) are set in the
allowlist file, not in the environment.

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

A call either pushes its own commit or rolls it back; it never leaves a commit on the
local branch without saying so in `outcome`. Git history, the audit trail and the
agent-bus event make each change attributable and revertible after the fact; repoints and
removals come only from the explicit tools, so they are intended.

The deploy key should be mode `0600` and owned by the user the server runs as.

## Installation

Requirements:

- Python 3.11+
- A `doc-sync.py` that matches this release (failure state, `remove_topics`, provisional
  tier), with the generated `doc_cache_allowlist.py` next to it (see
  [Allowlist schema](#allowlist-schema-v2)).
- `doc-sync.yml` inside a git checkout, if `GIT_COMMIT` is on (the default).
- The allowlist file.

doc-cache-mcp is not published to PyPI. Install it from a checkout of a release tag, from
the committed `uv.lock`, so the dependency set is the one CI tested and audited. Use
[uv](https://docs.astral.sh/uv/).

Save this as a script and run it with `bash`. Don't paste it into a shell: every check ends
the run on failure, and that only works under `set -e` in a script.

```bash
#!/usr/bin/env bash
set -euo pipefail
VERSION=0.4.0
REPO=~/repos/doc-cache-mcp
VENV=/opt/venvs/doc-cache-mcp
PY=/usr/bin/python3.13
EXTRAS=(--extra telemetry)   # drop it if no telemetry backend will ever be enabled
die() { echo "install: $*" >&2; exit 1; }

[ -d "$REPO/.git" ] || git clone https://github.com/TadMSTR/doc-cache-mcp "$REPO"
cd "$REPO" || die "cannot enter $REPO"
git fetch --tags origin
git checkout --quiet "v$VERSION"

# 1. The checkout is exactly the release tag on the remote, and nothing else: the package
#    is built from this tree. `git status` does not show ignored files, so remove bytecode
#    and tool caches, then refuse anything left except a development venv and .env files.
find . \( -path ./.venv -o -path ./venv \) -prune -o \( -name __pycache__ -o -name '*.py[co]' \) -print0 \
  | xargs -0 -r rm -rf --
rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov build dist
status=$(git status --porcelain --ignored)
extra=$(printf '%s\n' "$status" | grep -vxE '(!! (\.?venv/|\.env|\.env\.[^/]+))?' || true)
[ -z "$extra" ] || die "checkout holds more than v$VERSION; remove these first:
$extra"
# The peeled line (^{}) is listed last for an annotated tag; a lightweight tag has one line.
tag=$(git ls-remote origin "refs/tags/v$VERSION" "refs/tags/v$VERSION^{}" | tail -n1 | cut -f1)
[ -n "$tag" ] || die "tag v$VERSION is not on origin"
[ "$(git rev-parse HEAD)" = "$tag" ] || die "HEAD is not origin's v$VERSION ($tag)"

# 2. Build a fresh venv at the path the service runs from. Stop the service and move the
#    old venv aside first; this refuses to build over one. Pin the interpreter: uv
#    otherwise prefers a Python it manages itself, which `uv python` can later remove or
#    replace under a running service. --no-editable installs a copy of the package, so the
#    service does not follow later changes to the checkout.
[ ! -e "$VENV" ] || die "$VENV exists: stop the service and move it aside first"
UV_PROJECT_ENVIRONMENT="$VENV" \
  uv sync --frozen --no-editable "${EXTRAS[@]}" --python "$PY"

# 3. The venv is exactly the lock (no missing, extra or different package), and the
#    installed package imports and reports the release version.
UV_PROJECT_ENVIRONMENT="$VENV" \
  uv sync --frozen --no-editable "${EXTRAS[@]}" --python "$PY" --check \
  || die "venv does not match uv.lock"
# -I: import only what is installed in the venv, never from this checkout.
"$VENV/bin/python" -I - "$VERSION" <<'EOF' || die "installed package check failed"
import importlib.metadata as m, sys, doc_cache_mcp.server
assert m.version("doc-cache-mcp") == sys.argv[1], m.version("doc-cache-mcp")
print("doc-cache-mcp", sys.argv[1])
EOF
[ -x "$VENV/bin/doc-cache-mcp" ] || die "entry point missing"
echo "install: doc-cache-mcp v$VERSION ready in $VENV; restart the service"
```

Build a fresh venv rather than installing into an existing one: `pip install .` into an
existing venv keeps whatever older dependency versions it finds there, if they still
satisfy the declared ranges. Build at the final path rather than building elsewhere and
moving it: console-script shebangs embed the venv path. A uv-built venv has no `pip`.

CI audits the locked set on every push and weekly, but nothing reads a scheduled run's
result unless you watch it, and GitHub disables a public repository's scheduled workflows
after 60 days without activity. Audit the deployed venv where it runs as well (for example
`pip-audit --path /opt/venvs/doc-cache-mcp/lib/python3.13/site-packages`).

The provisional tier fetches from unvetted hosts through requests and urllib3. The venv
that runs `doc-sync.py` on its own (outside this server) needs the same floors.

## Deployment

**PM2.** `ecosystem.config.js` runs the installed `doc-cache-mcp` entry point over
streamable-http on `127.0.0.1:8503`, with the paths, `GIT_PUSH`, the deploy key path and
`AUDIT_LOG_DIR` set in its `env` block. Edit the paths for your host, then:

```bash
pm2 start ecosystem.config.js
pm2 logs doc-cache-mcp
```

The HTTP transport is stateless (`stateless_http=True`): no tool keeps session state, and
`doc_cache_sync`'s progress notifications still arrive on the request's own stream.

**scoped-mcp.** Wire it in as an `mcp_proxy` module in the calling agent's manifest and
name the tools it may use:

```yaml
modules:
  doc-cache-mcp:
    type: mcp_proxy
    config:
      url: http://localhost:8503/mcp
      tool_allowlist:
        - doc_cache_list_services
        - doc_cache_add_service
        - doc_cache_update_topic
        - doc_cache_remove_topic
        - doc_cache_sync
rate_limits:
  "doc-cache-mcp_doc_cache_sync": "10/minute"
  "doc-cache-mcp_doc_cache_remove_topic": "5/minute"   # destructive
```

The tools then appear to the agent as `mcp__scoped-mcp__doc-cache-mcp_<tool>`.
`tool_allowlist` restricts that agent only. The port itself has no authentication, so
anything on the host that can reach loopback can call every tool, including
`doc_cache_update_topic` and `doc_cache_remove_topic`. With `GIT_COMMIT` on, every config
change is a git commit and can be reverted.

## Development

```bash
uv sync --frozen --extra dev --extra telemetry
uv run --frozen --no-sync pytest
```

## Observability

Structured logging (structlog, JSON to stdout) is always on. Two optional backends are each
gated on their own env var and disabled — with no import cost — when unset. Install the extras
with `pip install ".[telemetry]"`.

| Var | Required | Default | Purpose |
|-----|----------|---------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | no | unset (tracing off) | OTLP gRPC span export (e.g. `http://127.0.0.1:4317`). `service.name` is `doc-cache-mcp`. |
| `INFLUXDB_URL` | no | unset (metrics off) | Best-effort per-call metric emission to InfluxDB 3. |
| `INFLUXDB_TOKEN` | no | empty | Auth token for the InfluxDB backend, if it requires one. |
| `INFLUXDB_DATABASE` | no | `INFLUXDB_BUCKET`, then `doc-cache-mcp` | Target database. Set it to the database your dashboards read: before 0.3.1 only `INFLUXDB_BUCKET` was read, so a deployment that set `INFLUXDB_DATABASE` wrote every point to an unread `doc-cache-mcp` database. |

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
