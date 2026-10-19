"""Source-URL policy for the docs cache — the SSRF, cache-poisoning and content-trust guard.

This is the single implementation of the policy, enforced at BOTH boundaries so add-time
and fetch-time policy can never drift:

* this server enforces it at **add time** (``doc_cache_add_service`` /
  ``doc_cache_update_topic``) — a bad URL is refused before it is ever written into
  ``doc-sync.yml``;
* ``doc-sync.py`` enforces it at **fetch time** (every URL, every redirect hop) so the
  ``doc-sync-daily`` cron and ``doc_cache_sync`` are covered.

That claim used to be only half true: the open-edit (wiki) denylist lived in ``doc-sync.py``
alone, so add time would register a GitHub wiki URL the sync then refused on every run
(vikunja#1167). It lives here now, and both boundaries call :func:`classify_url`.

On forge, ``doc-sync.py`` runs in a separate venv and imports a vendored copy of this module
(``host-forge-scripts/scripts/doc_cache_allowlist.py``). That copy is **generated** from this
file by ``python -m doc_cache_mcp.vendoring --write`` — edit here, never there — and a test
regenerates it to catch drift. The module depends only on the stdlib + PyYAML so both venvs
can import it.

Evaluation order (:func:`classify_url`). **Deny wins**: nothing later can re-admit a URL an
earlier step refused.

1. Structural: ``https`` only, a host name (no IP literal), no userinfo, port 443 or none.
2. ``deny``, absolute, every tier:

   * open-edit paths — public wikis anyone can edit. A built-in floor
     (:data:`OPEN_EDIT_FLOOR`) that the allowlist's ``deny.open_edit`` can add to but never
     remove from, so a v1 file, or a v2 file without the block, cannot turn wiki refusal off;
   * path shapes — ``deny.path_shapes`` maps a host to the *only* path patterns it may be
     fetched on (e.g. ``github.com`` only on ``/<o>/<r>/(blob|raw|tree|releases)/``).

3. ``forge_endpoints`` — exact host + boundary-checked path prefix. Trusted, and may resolve
   to a private forge address, so no DNS re-check.
4. ``hosts`` — trusted. An entry with a leading dot (``.opensearch.org``) matches that domain
   and every subdomain of it; any other entry is exact-match.
5. Provisional — any other host, when ``provisional.enabled`` and the host is not matched by
   ``provisional_exclude``. Labelled unvetted, cached apart from trusted content, expires.
6. Everything public (trusted or provisional) must resolve only to public addresses — the
   DNS-rebind re-check. At fetch time it runs per redirect hop, in the same process as the
   request, immediately before it.

A missing ``provisional`` block means **disabled**, so a v1 allowlist (``hosts`` +
``forge_endpoints`` only) behaves exactly as it always has: default-deny. A missing or
malformed file denies everything.
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlparse

import yaml

# Resolver signature matches socket.getaddrinfo; injectable so tests need no real DNS.
Resolver = Callable[[str], list]

TRUSTED = "trusted"
PROVISIONAL = "provisional"

#: Defaults for the ``provisional`` block's ceilings. The block itself defaults to absent,
#: which means disabled — these apply only once a file turns the tier on.
PROVISIONAL_DEFAULTS = {"ttl_days": 30, "max_hosts": 25, "max_bytes": 2 * 1024 * 1024}

#: Open-edit sources: hosts that serve maintainer-reviewed docs AND pages any account can
#: edit. Text from one would reach every agent's docs cache with no review at all
#: (obs-followups-2026-10 p1 audit F-01). Matched against the normalised path, per redirect
#: hop. This is a floor: ``deny.open_edit`` in the allowlist adds to it, never replaces it.
OPEN_EDIT_FLOOR: tuple[tuple[str, str], ...] = (
    ("raw.githubusercontent.com", r"^/wiki/"),
    # Any third segment starting with "wiki": /wiki, /wiki/<page>, and the /wiki.atom feed,
    # which carries full rendered page bodies (audit R3-01).
    ("github.com", r"^/[^/]+/[^/]+/wiki"),
    ("gitlab.com", r"/-/wikis(/|$)"),
    # Any registered account can edit it (audit R2-02); refused whole so adding the host to
    # the allowlist cannot quietly bring it back.
    ("wiki.debian.org", r"^/"),
)


_FLOOR = tuple((h, re.compile(p)) for h, p in OPEN_EDIT_FLOOR)


class AllowlistError(ValueError):
    """A source URL was refused by the docs-cache allowlist."""


class Verdict(NamedTuple):
    """What :func:`classify_url` decided about an allowed URL.

    A NamedTuple, not a dataclass: dataclasses look their module up in ``sys.modules``,
    and the vendored copy is loaded by path, where it is not necessarily registered.
    """

    trust: str  # TRUSTED or PROVISIONAL
    host: str
    url: str


def _unwrap(ip: ipaddress._BaseAddress) -> ipaddress._BaseAddress:
    """Unwrap an IPv4-mapped/6to4 IPv6 address to its embedded IPv4 (F-04).

    ``IPv6Address.is_private`` does not consult the embedded IPv4's private ranges on all
    Python versions, so ``::ffff:10.0.0.1`` could otherwise slip past the recheck.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        return mapped
    sixtofour = getattr(ip, "sixtofour", None)
    if sixtofour is not None:
        return sixtofour
    return ip


def _ip_is_private(ip: ipaddress._BaseAddress) -> bool:
    """True for any address that must never be reached from a cache-fetch (SSRF guard).

    ``not is_global`` catches the special-purpose ranges the named flags miss, such as
    100.64.0.0/10 (CGNAT). It does NOT catch fec0::/10: deprecated site-local addresses are
    ``is_global`` on Python 3.13, so they are named explicitly (audit F-08).
    """
    ip = _unwrap(ip)
    return (
        not ip.is_global
        or (ip.version == 6 and ip.is_site_local)
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _norm_host(raw) -> str:
    return str(raw).strip().lower().rstrip(".")


def host_matches(host: str, entry: str) -> bool:
    """``entry`` matches ``host`` exactly, or — with a leading dot — as a domain suffix.

    ``.opensearch.org`` matches ``opensearch.org`` and ``docs.opensearch.org``, but not
    ``evilopensearch.org``: the match is on a label boundary, never a bare string suffix.
    """
    if entry.startswith("."):
        apex = entry[1:]
        return bool(apex) and (host == apex or host.endswith(entry))
    return host == entry


def _matches_any(host: str, entries) -> bool:
    return any(host_matches(host, e) for e in entries)


def _compile_host_patterns(raw, field: str) -> list[tuple[str, re.Pattern]]:
    """``{host: [regex, ...]}`` → ``[(host, compiled), ...]``. Malformed → AllowlistError."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise AllowlistError(f"allowlist {field} must be a mapping of host -> [regex, ...]")
    out: list[tuple[str, re.Pattern]] = []
    for host, patterns in raw.items():
        if isinstance(patterns, str):
            patterns = [patterns]
        if not isinstance(patterns, list) or not patterns:
            raise AllowlistError(f"allowlist {field}.{host} must be a non-empty list of regexes")
        for pat in patterns:
            try:
                out.append((_norm_host(host), re.compile(str(pat))))
            except re.error as exc:
                raise AllowlistError(f"allowlist {field}.{host}: bad regex {pat!r}: {exc}") from exc
    return out


def _load_provisional(raw) -> dict:
    """The ``provisional`` block, normalised. Absent → disabled (v1 behaviour)."""
    if raw is None:
        return {"enabled": False, **PROVISIONAL_DEFAULTS}
    if not isinstance(raw, dict):
        raise AllowlistError("allowlist provisional must be a mapping")
    out: dict = {"enabled": raw.get("enabled") is True}
    for key, default in PROVISIONAL_DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AllowlistError(f"allowlist provisional.{key} must be a positive integer")
        out[key] = value
    return out


def load_allowlist(path) -> dict:
    """Load and normalise the allowlist file.

    Returns a dict with:

    * ``hosts`` — ``set[str]``, exact and leading-dot suffix entries together;
    * ``forge_endpoints`` — ``list[tuple[host, path_prefix]]``;
    * ``open_edit`` — ``list[tuple[host, Pattern]]``, the file's ``deny.open_edit``
      additions. :data:`OPEN_EDIT_FLOOR` is not stored here: it is applied unconditionally
      by :func:`refuse_open_edit`, so no dict — loaded or hand-built — can omit it;
    * ``path_shapes`` — ``list[tuple[host, Pattern]]`` from ``deny.path_shapes``;
    * ``provisional`` — ``{enabled, ttl_days, max_hosts, max_bytes}``, disabled when absent;
    * ``provisional_exclude`` — ``set[str]``, same matching rules as ``hosts``;
    * ``source`` and ``loaded_at`` — provenance, not policy: they let a refusal say *which
      snapshot* refused, so "the host is not in the file" and "this process is holding an
      old copy of the file" stop looking identical (vikunja#374).

    Raises :class:`AllowlistError` if the file is missing or malformed — a missing allowlist
    means *deny everything*, surfaced as an error rather than an open door. A malformed
    ``deny`` or ``provisional`` block is a refusal too, never a silently skipped block.
    """
    path = Path(path)
    if not path.exists():
        raise AllowlistError(
            f"allowlist not found at {path}; refusing all source URLs until it exists"
        )
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise AllowlistError(f"allowlist file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AllowlistError("allowlist file must be a YAML mapping")

    hosts = {_norm_host(h) for h in (data.get("hosts") or [])}

    forge_endpoints: list[tuple[str, str]] = []
    for raw in data.get("forge_endpoints") or []:
        entry = str(raw).strip()
        if not entry:
            continue
        host, _, prefix = entry.partition("/")
        host = host.lower().rstrip(".")
        prefix = "/" + prefix if prefix else "/"
        forge_endpoints.append((host, prefix))

    deny = data.get("deny") or {}
    if not isinstance(deny, dict):
        raise AllowlistError("allowlist deny must be a mapping")

    return {
        "hosts": hosts,
        "forge_endpoints": forge_endpoints,
        "open_edit": _compile_host_patterns(deny.get("open_edit"), "deny.open_edit"),
        "path_shapes": _compile_host_patterns(deny.get("path_shapes"), "deny.path_shapes"),
        "provisional": _load_provisional(data.get("provisional")),
        "provisional_exclude": {_norm_host(h) for h in (data.get("provisional_exclude") or [])},
        "source": str(path),
        "loaded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


def _snapshot_hint(allowlist: dict) -> str:
    """Describe the snapshot that refused a host, so a stale one is recognisable.

    The old refusal said only "Add it to doc-cache-allowlist.yml", which sent the operator
    to edit a file that already contained the host: the server had memoised the allowlist
    for the life of the process, so no edit could ever take effect (vikunja#374). Naming
    the file, the load time and the host count makes the two cases tell themselves apart
    without anyone having to guess.

    Degrades gracefully for a hand-built allowlist dict with no provenance (tests, and any
    caller that assembles one directly).
    """
    source = allowlist.get("source")
    if not source:
        return "Add it to doc-cache-allowlist.yml (sysadmin) to cache from it."
    loaded_at = allowlist.get("loaded_at")
    when = f", loaded {loaded_at}" if loaded_at else ""
    count = len(allowlist.get("hosts") or ())
    return (
        f"This used {source}{when} ({count} hosts). If the host is ALREADY in that file, "
        "this process is serving a stale snapshot — report that as a bug rather than "
        "editing the file again. Otherwise add it (sysadmin) to cache from it."
    )


def _path_matches_prefix(url_path: str, prefix: str) -> bool:
    """Boundary-aware, traversal-safe path-prefix match (F-02).

    Normalises the URL path (collapsing ``.``/``..``) and requires either an exact match or
    a match ending on a ``/`` boundary — so ``/a/docs.json`` does NOT match
    ``/a/docs.json.backup`` and ``/a/docs.json/../tasks`` cannot slip through.
    """
    norm = posixpath.normpath(url_path or "/")
    ep = posixpath.normpath(prefix or "/")
    if ep in ("", "/", "."):
        return True  # host-level allow (prefix "/")
    return norm == ep or norm.startswith(ep + "/")


def _decoded_path(path: str) -> str:
    """The path the server will route on: percent-decoded (repeatedly, so %252F and %2F
    both land), repeated slashes collapsed, dot segments resolved. Case preserved."""
    p = path or "/"
    for _ in range(4):
        q = unquote(p)
        if q == p:
            break
        p = q
    return posixpath.normpath("/" + re.sub(r"/+", "/", p).lstrip("/"))


def _normalised_path(path: str) -> str:
    """:func:`_decoded_path`, lowercased — for deny matching, where wider is safer.
    Matching the raw string let %77iki, wiki%2F, /./wiki and // through (audit R2-01)."""
    return _decoded_path(path).lower()


def _assert_resolves_public(host: str, resolver: Resolver) -> None:
    """Reject a public host that resolves to any non-public address.

    Default-deny: an unresolvable host is refused too — we cannot prove it is safe.
    """
    try:
        infos = resolver(host)
    except OSError as exc:
        raise AllowlistError(f"cannot resolve allowlisted host {host!r}: {exc}") from exc
    if not infos:
        raise AllowlistError(f"host {host!r} did not resolve to any address")
    for info in infos:
        addr = info[4][0].split("%")[0]  # strip any IPv6 zone id
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_private(ip):
            # Not the address itself: with the provisional tier any name reaches this check,
            # and echoing what it resolved to would make the tool an internal DNS oracle
            # (audit F-11).
            raise AllowlistError(
                f"host {host!r} resolves to a non-public address (SSRF / DNS-rebind guard)"
            )


#: Longer than any real docs URL. Checked here, not only at add time, because a redirect
#: ``Location`` reaches this function at fetch time and the deny regexes run on it.
MAX_URL_LEN = 2048

# Whitespace and C0/C1 controls. urlparse silently drops \t \r \n, so a URL carrying them
# classified as if they were absent and was then written verbatim into doc-sync.yml and the
# chunk frontmatter, where a newline forges fields (pre-audit baseline, SH-03).
_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f-\x9f]")


def _structural(url: str) -> tuple:
    """Step 1. Returns ``(parsed, host)`` or raises."""
    if not isinstance(url, str) or not url.strip():
        raise AllowlistError("source url must be a non-empty string")
    url = url.strip()
    if len(url) > MAX_URL_LEN:
        raise AllowlistError(f"source url exceeds {MAX_URL_LEN} characters")
    if _UNSAFE_URL_CHARS.search(url):
        raise AllowlistError(f"source url contains whitespace or control characters: {url!r}")

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AllowlistError(f"source url must use https (got scheme {parsed.scheme!r}): {url!r}")
    host = parsed.hostname
    if not host:
        raise AllowlistError(f"source url has no host: {url!r}")
    host = host.lower().rstrip(".")

    # Reject IP-literal hosts outright — the allowlist matches by name only, and a literal
    # is exactly how an SSRF payload names an internal target.
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise AllowlistError(f"source url host must be a name, not an IP literal: {host!r}")
    except ValueError:
        pass  # not an IP literal — good, it's a hostname

    # userinfo is how a URL is made to read as one host while naming another
    # (https://docs.example.com@evil.net/), and a non-default port reaches a different
    # service on an allowed name. Neither has a legitimate use for a docs source.
    if parsed.username is not None or parsed.password is not None:
        raise AllowlistError(f"source url must not carry userinfo: {url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AllowlistError(f"source url has an invalid port: {url!r}") from exc
    if port not in (None, 443):
        raise AllowlistError(f"source url must use port 443 (got {port}): {url!r}")
    # urlparse splits ;params off the LAST segment only, so a mid-path `;` stayed in the
    # path and broke boundary-anchored rules: /-/wikis;x/home and /wiki;/o/r passed the
    # floor (audit F-12). No docs URL needs a matrix parameter.
    if ";" in parsed.path or parsed.params:
        raise AllowlistError(f"source url path must not contain ';': {url!r}")
    return parsed, host


def refuse_open_edit(url: str, allowlist: dict | None = None) -> None:
    """Raise AllowlistError if the URL is an open-edit source (a public wiki).

    The floor always applies; the allowlist's ``deny.open_edit`` only adds to it. With no
    allowlist the floor alone applies, so the refusal never depends on one loading.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = _normalised_path(parsed.path)
    for h, pattern in (*_FLOOR, *((allowlist or {}).get("open_edit") or ())):
        if host_matches(host, h) and pattern.search(path):
            raise AllowlistError(
                f"source url is an open-edit wiki page, not maintainer-reviewed content: {url!r}"
            )


def _deny(url: str, host: str, path: str, allowlist: dict) -> None:
    """Step 2: open-edit floor + additions, then path shapes. Applies to every tier."""
    refuse_open_edit(url, allowlist)
    shapes = [pat for h, pat in allowlist.get("path_shapes") or () if host_matches(host, h)]
    if shapes and not any(pat.search(path) for pat in shapes):
        raise AllowlistError(
            f"path {path!r} is not a permitted shape for host {host!r} "
            "(deny.path_shapes in the allowlist names the only paths it may be fetched on)"
        )


def host_tier(host: str, allowlist: dict) -> str | None:
    """The tier a host *would* get, without DNS and without the path rules.

    For counting and labelling (``max_hosts``, listings) where resolving every configured
    host would be slow and is not the question. Never a substitute for :func:`classify_url`
    before a fetch or a write.
    """
    host = _norm_host(host)
    if any(host == eh for eh, _ in allowlist.get("forge_endpoints", [])):
        return TRUSTED
    if _matches_any(host, allowlist.get("hosts", set())):
        return TRUSTED
    prov = allowlist.get("provisional") or {}
    if prov.get("enabled") and not _matches_any(host, allowlist.get("provisional_exclude", set())):
        return PROVISIONAL
    return None


def classify_url(url: str, allowlist: dict, resolver: Resolver | None = None) -> Verdict:
    """Classify one source URL. Returns a :class:`Verdict` or raises :class:`AllowlistError`.

    See the module docstring for the evaluation order; deny wins. Callers that fetch should
    call this immediately before the request, and for every redirect hop, so the
    resolve-and-recheck runs in the same process as the fetch.
    """
    if resolver is None:
        resolver = lambda h: socket.getaddrinfo(h, None)  # noqa: E731

    parsed, host = _structural(url)
    path = _normalised_path(parsed.path)
    _deny(url, host, path, allowlist)

    # Explicit forge endpoint: trusted internal source, name + boundary-checked path match.
    # Allowed to resolve to private forge addresses, so no DNS re-check. Matched on the
    # DECODED path: requests turns %2e back into "." before sending, so a raw-path match let
    # /docs.json/%2e%2e/tasks through as inside the prefix (pre-audit baseline F-1).
    decoded = _decoded_path(parsed.path)
    for eh, eprefix in allowlist.get("forge_endpoints", []):
        if host == eh and _path_matches_prefix(decoded, eprefix):
            return Verdict(TRUSTED, host, url)

    if _matches_any(host, allowlist.get("hosts", set())):
        _assert_resolves_public(host, resolver)
        return Verdict(TRUSTED, host, url)

    prov = allowlist.get("provisional") or {}
    if prov.get("enabled"):
        if _matches_any(host, allowlist.get("provisional_exclude", set())):
            raise AllowlistError(
                f"host {host!r} is on provisional_exclude — it can never be cached "
                "self-service. Only an explicit entry in the allowlist's hosts (Ted) admits it."
            )
        _assert_resolves_public(host, resolver)
        return Verdict(PROVISIONAL, host, url)

    raise AllowlistError(
        f"host {host!r} is not on the docs-cache allowlist (default-deny). "
        + _snapshot_hint(allowlist)
    )


def lower_trust(a: str, b: str) -> str:
    """The trust of a fetch that passed through both tiers: a redirect never escalates."""
    return PROVISIONAL if PROVISIONAL in (a, b) else TRUSTED


def validate_url(url: str, allowlist: dict, resolver: Resolver | None = None) -> str:
    """Validate one source URL for the **trusted** tier. Returns the URL if allowed.

    Kept for callers that predate the provisional tier: it accepts exactly what
    :func:`classify_url` classifies as trusted, and refuses a provisional URL as
    not-allowlisted, so its meaning — "this may enter the trusted cache" — is unchanged.
    """
    verdict = classify_url(url, allowlist, resolver)
    if verdict.trust != TRUSTED:
        raise AllowlistError(
            f"host {verdict.host!r} is not on the docs-cache allowlist (default-deny); it is "
            "eligible only for the provisional tier. " + _snapshot_hint(allowlist)
        )
    return url
