"""Source-URL allowlist for the docs cache — the SSRF / cache-poisoning guard.

This is the single implementation of the allowlist policy, enforced at BOTH boundaries so
add-time and fetch-time policy can never drift:

* this server enforces it at **add time** (``doc_cache_add_service``) — a bad URL is refused
  before it is ever written into ``doc-sync.yml``;
* ``doc-sync.py`` enforces it at **fetch time** (every URL, every redirect hop) so the
  ``doc-sync-daily`` cron and ``doc_cache_sync`` are covered.

On forge, ``doc-sync.py`` runs in a separate venv and imports a byte-identical vendored copy
of this module (``host-forge-scripts/scripts/doc_cache_allowlist.py``); a test asserts the
two stay in sync. The module depends only on the stdlib + PyYAML so both venvs can import it.

Policy (default-deny):

* Scheme must be ``https``.
* IP-literal hosts are rejected outright — the allowlist is name-based.
* A forge endpoint (exact host + normalised, boundary-checked path prefix) is trusted and
  may resolve to a private forge address.
* A public host must be on the host allowlist AND every address it currently resolves to
  must be public (DNS-rebind re-check). At fetch time this runs in the same process as the
  request, immediately before it, so the rebind window is closed in practice.
* Anything else is refused. A missing/malformed allowlist denies everything.
"""

from __future__ import annotations

import ipaddress
import posixpath
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yaml

# Resolver signature matches socket.getaddrinfo; injectable so tests need no real DNS.
Resolver = Callable[[str], list]


class AllowlistError(ValueError):
    """A source URL was refused by the docs-cache allowlist."""


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
    """True for any address that must never be reached from a cache-fetch (SSRF guard)."""
    ip = _unwrap(ip)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def load_allowlist(path) -> dict:
    """Load and normalise the allowlist file.

    Returns ``{"hosts": set[str], "forge_endpoints": list[tuple[host, path_prefix]]}``.
    Raises :class:`AllowlistError` if the file is missing or malformed — a missing allowlist
    means *deny everything*, surfaced as an error rather than an open door.
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

    hosts = {str(h).strip().lower().rstrip(".") for h in (data.get("hosts") or [])}

    forge_endpoints: list[tuple[str, str]] = []
    for raw in data.get("forge_endpoints") or []:
        entry = str(raw).strip()
        if not entry:
            continue
        host, _, prefix = entry.partition("/")
        host = host.lower().rstrip(".")
        prefix = "/" + prefix if prefix else "/"
        forge_endpoints.append((host, prefix))

    return {"hosts": hosts, "forge_endpoints": forge_endpoints}


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


def _assert_resolves_public(host: str, resolver: Resolver) -> None:
    """Reject a public-allowlist host that resolves to any non-public address.

    Default-deny: an unresolvable host is refused too — we cannot prove it is safe.
    """
    try:
        infos = resolver(host)
    except OSError as exc:
        raise AllowlistError(
            f"cannot resolve allowlisted host {host!r}: {exc}"
        ) from exc
    if not infos:
        raise AllowlistError(f"host {host!r} did not resolve to any address")
    for info in infos:
        addr = info[4][0].split("%")[0]  # strip any IPv6 zone id
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_private(ip):
            raise AllowlistError(
                f"allowlisted host {host!r} resolves to non-public address {addr} "
                "(SSRF / DNS-rebind guard)"
            )


def validate_url(url: str, allowlist: dict, resolver: Resolver | None = None) -> str:
    """Validate one source URL against the allowlist. Returns the URL if allowed.

    Raises :class:`AllowlistError` with a specific reason otherwise. Callers that fetch
    should call this immediately before the request (and for every redirect hop) so the
    resolve-and-recheck runs in the same process as the fetch.
    """
    if resolver is None:
        resolver = lambda h: socket.getaddrinfo(h, None)  # noqa: E731

    if not isinstance(url, str) or not url.strip():
        raise AllowlistError("source url must be a non-empty string")

    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        raise AllowlistError(
            f"source url must use https (got scheme {parsed.scheme!r}): {url!r}"
        )
    host = parsed.hostname
    if not host:
        raise AllowlistError(f"source url has no host: {url!r}")
    host = host.lower().rstrip(".")

    # Reject IP-literal hosts outright — the allowlist matches by name only, and a literal
    # is exactly how an SSRF payload names an internal target.
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise AllowlistError(
            f"source url host must be a name, not an IP literal: {host!r}"
        )
    except ValueError:
        pass  # not an IP literal — good, it's a hostname

    # Explicit forge endpoint: trusted internal source, name + boundary-checked path match.
    # Allowed to resolve to private forge addresses, so no DNS re-check.
    for eh, eprefix in allowlist.get("forge_endpoints", []):
        if host == eh and _path_matches_prefix(parsed.path, eprefix):
            return url

    # Public host allowlist + resolve-and-recheck.
    if host not in allowlist.get("hosts", set()):
        raise AllowlistError(
            f"host {host!r} is not on the docs-cache allowlist (default-deny). "
            "Add it to doc-cache-allowlist.yml (sysadmin) to cache from it."
        )
    _assert_resolves_public(host, resolver)
    return url
