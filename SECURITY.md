# Security model — doc-cache-mcp

## Threat this server closes

Before this server, the research agent could write **any** `url:` into the shared
`doc-sync.yml`, and every agent searches the resulting docs cache as authoritative. A
malicious or mistaken URL could:

- point the cache-fetcher at an **internal** endpoint (SSRF) — e.g. a metadata service or
  an unauthenticated internal admin API, and
- **poison** the trusted cache with attacker-controlled content other agents then act on.

The old model (deny-list argument filters on generic `edit_file`) constrained the *path*
of the edit, not its *content*, so it had nowhere to enforce this.

## Controls

1. **Source-URL policy** (`allowlist.py`, `classify_url`) — enforced before any write and
   before every fetch hop. Deny wins:
   - `https` only; host must be a name (IP literals refused, numeric IPv4 forms such as
     `16843009` / `0x01010101` / `1.1` included); no userinfo; port 443 or none.
   - **Open-edit floor** in code (GitHub/GitLab wikis, `wiki.atom`, `wiki.debian.org`),
     matched on the decoded, normalised path. The allowlist file can add to it, never
     remove from it. **Path shapes** restrict a host to named path patterns.
   - Forge endpoints: exact host + path prefix, explicitly trusted, may resolve internal.
   - Trusted hosts (exact, or leading-dot suffix on a label boundary).
   - **Provisional** (only if the file enables it): any other public host not on
     `provisional_exclude`. See "Provisional tier" below.
   - Every public result, either tier, must resolve **only** to public addresses — the
     **DNS-rebind guard** (`_assert_resolves_public`). An unresolvable host is denied.
   - Missing/malformed allowlist (including a malformed `deny` or `provisional` block) ⇒
     deny everything.

2. **No arbitrary execution.** The server exposes five typed verbs. It has no
   `run_command`/`read_file`/`edit_file` surface. The only subprocess calls are fixed-argv
   `git` (status, fetch, add, commit, push, update-ref, restore — all scoped to the one
   config file or its branch) and the fixed-argv metadata-index prune that `doc-sync.py`
   runs after removing files. None uses a shell.

3. **Typed, validated params.** `service` matches `^[A-Za-z0-9_-]+$`; `topic` matches
   `^[A-Za-z0-9._-]+$` (so it cannot break out of the YAML frontmatter block or the tags
   list it is interpolated into); URL length is capped.

4. **Structural writes only.** Every write tool does `yaml.safe_load` → in-memory edit →
   `yaml.safe_dump` → atomic replace. It never does a text/regex edit of the config.

5. **Single-file git scope and an honest write path** (`push.py`). The commit stages and
   commits exactly the resolved `doc-sync.yml` path; it cannot sweep other working-tree
   changes in. Before writing, the checkout must be clean for that file and level with the
   remote. After committing, the change must be the kind the tool makes (add/repoint/
   remove), every commit in the push range must be the tool's, and the range may touch
   only that file; the push sends the validated SHA. A failed push is rolled back by
   compare-and-swap of the ref (never `git reset`), so a concurrent human commit is never
   discarded. Every call returns one `outcome`.

6. **Concurrency.** Write calls are serialised by a process lock. `sync_service`,
   `remove_topics` and the `doc-sync-daily` cron share a `flock` on the state file.

7. **Loopback only.** Binds `127.0.0.1`. No off-host exposure, no auth surface added.

## Provisional tier (doc-cache-friction-2026-10, D1 = A)

When the allowlist enables it, research can cache docs from a host nobody has approved.
That is **new egress**, and the content is unvetted. Controls:

- **SSRF:** the resolve-and-recheck runs for provisional hosts exactly as for trusted ones,
  per redirect hop, in the fetching process. Redirects are never auto-followed.
- **Size:** a provisional fetch is streamed and refused past `provisional.max_bytes`
  (default 2 MiB), by declared `Content-Length` and by bytes actually read.
- **Blast radius:** `max_hosts` (default 25) caps distinct provisional hosts at add time;
  `ttl_days` (default 30) stops syncing an unpromoted topic and deletes its chunks. Both
  hold because a topic's tier is its configured URL's: a trusted URL that redirects to an
  unvetted host is refused rather than cached as provisional (audit F-02). The TTL bounds
  idle content, not an agent's intent: a re-add starts a fresh TTL, and emits the event
  again, so Ted's review of the promotion queue is the real bound (audit F-09).
- **Demotion:** removing a host from `hosts` expires its topics and deletes their chunks
  from both tiers, including the trusted, qmd-indexed directory (audit F-05).
- **Dependencies:** `urllib3>=2.8.0`. 2.7.0 had a streaming-API infinite loop and an
  unbounded chunk-size buffer that a hostile provisional server could trigger through the
  capped read (audit F-01). The doc-sync venv has no requirements file; its upgrade is a
  deploy step, and the provisional tier must not be enabled before it.
- **Labelling and isolation:** chunks go to `~/.claude/docs-provisional/` (outside
  `~/.claude/memory`, so qmd's agent-memory collection and the memory pipeline never see
  them) with `trust: provisional` frontmatter.
- **No trust escalation:** a redirect from a provisional URL to a trusted host stays
  provisional, and a trusted URL may not redirect to an unvetted host. Trust is recomputed from the allowlist on every add and fetch; the
  `trust:` field in `doc-sync.yml` is informational and grants nothing.
- **Never self-service:** `provisional_exclude` (paste sites, shorteners, user-content and
  free-hosting platforms) is refused; only an explicit `hosts` entry admits such a host.
- **Visibility:** every provisional add emits `doc-cache.provisional.added` to the audit sink.
- **DNS rebind:** the recheck and the connection are two lookups, so for a provisional
  host, whose DNS an attacker may run, the recheck only narrows the window. **TLS hostname
  verification is the control that closes it**: an internal `:443` service would need a
  valid certificate for the attacker's name. Only `https` on 443 is ever fetched, and a
  provisional fetch is refused if the session's verification is disabled (audit F-04).

## Fetch-time enforcement (post-audit, F-01)

The allowlist is a **single shared module** (`doc_cache_allowlist`, next to `doc-sync.py`)
enforced at **both** boundaries:

- **Add time** — `doc_cache_add_service` / `doc_cache_update_topic` classify every URL
  before it is written. The open-edit rules used to run only at fetch time, so add time
  registered wiki URLs the sync then refused (vikunja#1167); they now live in the shared
  module and run at both.
- **Fetch time** — `doc-sync.py` `fetch_classified` re-classifies the URL **and every redirect hop**
  immediately before each request (`allow_redirects=False`, manual per-hop re-validation).
  This runs the DNS resolve-and-recheck in the same process as the fetch, so the add-time /
  fetch-time TOCTOU is narrowed to the resolver/connect gap (TLS verification closes that;
  see above) and an allowlisted host cannot 301/302 the fetcher to an internal address. The `doc-sync-daily` cron shares this path, so it too is covered — and
  now depends on `doc-cache-allowlist.yml` being present.

## Residual notes

- `doc_cache_add_service` uses `yaml.safe_dump`, which **does not preserve comments** in
  `doc-sync.yml` (F-06, accepted). The first add rewrites the file without its section
  comments — the documented plan tradeoff (structural, not text, merge).
- The server runs as `ted`; OS-level file permissions, not the server, bound its reach
  beyond the configured paths.
- `calling_agent` in audit events is `null`: scoped-mcp does not forward the caller's
  identity to backend MCPs, so the server cannot know which agent called it. The scoped-mcp
  audit log is the place that does.
- A rollback can end `stranded` if someone commits to the live checkout between this
  call's commit and its rollback. That is reported, never resolved by discarding work, and
  the next call refuses until a human reconciles the checkout.
