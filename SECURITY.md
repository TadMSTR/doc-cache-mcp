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

1. **Source-URL allowlist** (`allowlist.py`) — enforced before any write:
   - `https` scheme only.
   - IP-literal hosts rejected (the allowlist is name-based; a literal is how SSRF names an
     internal target).
   - Public hosts: must be on the allowlist **and** resolve only to public addresses at
     validation time — a **DNS-rebind guard** (`_assert_resolves_public`). An unresolvable
     host is denied (default-deny).
   - Forge endpoints: exact host + path prefix, explicitly trusted, may resolve internal.
   - Missing/malformed allowlist ⇒ deny everything.

2. **No arbitrary execution.** The server exposes three typed verbs. It has no
   `run_command`/`read_file`/`edit_file` surface. The only subprocess calls are:
   - the fixed-argv `git add`/`commit` of the single config file, and
   - the fixed-argv memsearch index (inherited from `doc-sync.py`, unchanged).
   Neither uses a shell; both use list argv.

3. **Typed, validated params.** `service` matches `^[A-Za-z0-9_-]+$`; `topic` matches
   `^[A-Za-z0-9._-]+$` (so it cannot break out of the YAML frontmatter block or the tags
   list it is interpolated into); URL length is capped.

4. **Structural writes only.** `doc_cache_add_service` does `yaml.safe_load` →
   in-memory merge → `yaml.safe_dump` → atomic replace. It never does a text/regex edit of
   the config, so it cannot corrupt unrelated YAML.

5. **Single-file git scope.** The commit stages and commits exactly the resolved
   `doc-sync.yml` path (`git add -- <path>` / `commit -- <path>`); it cannot sweep other
   working-tree changes into the commit. The service name in the message is pre-validated.

6. **Concurrency.** `sync_service` and the `doc-sync-daily` cron share a `flock` on the
   state file, so a sync cannot interleave with the nightly run and lose writes.

7. **Loopback only.** Binds `127.0.0.1`. No off-host exposure, no auth surface added.

## Residual notes / for the audit

- `doc_cache_add_service` uses `yaml.safe_dump`, which **does not preserve comments** in
  `doc-sync.yml`. The first add rewrites the file without its section comments. This is the
  documented plan tradeoff (structural, not text, merge); flagged for review.
- The allowlist re-check resolves DNS at validation time, not at fetch time — a host that
  passes validation and rebinds before the subsequent `doc_cache_sync` fetch is a
  theoretical TOCTOU window. Fetches only run against already-configured, previously
  validated hosts; the fetch path itself does not re-validate (inherited `doc-sync` fetch).
- The server runs as `ted`; OS-level file permissions, not the server, bound its reach
  beyond the configured paths.
