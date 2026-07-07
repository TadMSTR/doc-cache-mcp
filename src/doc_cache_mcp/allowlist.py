"""Source-URL allowlist for the shared docs cache — SSRF / cache-poisoning guard.

Every URL that could enter the trusted docs cache passes through :func:`validate_url`
*before* it is written into ``doc-sync.yml``. This is the security core of doc-cache-mcp:
the old filter model constrained the *path* of the edit but not the *content*, so any
``url:`` could be injected into the cache every agent trusts. Here the URL itself is the
thing being validated.

Policy (default-deny):

* Scheme **must** be ``https``.
* IP-literal hosts are rejected outright — the allowlist is name-based.
* An explicit **forge endpoint** (name + path prefix, e.g.
  ``vikunja.helmforge.me/api/v1/docs.json``) is trusted and *may* resolve to a private
  forge address — that is the whole point of listing it.
* A **public host** must be on the host allowlist **and** every address it currently
  resolves to must be public — this re-check defeats DNS-rebind-style bypass where an
  allowlisted name is pointed at an internal IP.
* Anything else is refused.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yaml

# Resolver signature matches socket.getaddrinfo; injectable so tests need no real DNS.
Resolver = Callable[[str], list]


class AllowlistError(ValueError):
    """A source URL was refused by the docs-cache allowlist."""


def _ip_is_private(ip: ipaddress._BaseAddress) -> bool:
    """True for any address that must never be reached from a cache-fetch (SSRF guard)."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def load_allowlist(path: Path) -> dict:
    """Load and normalise the allowlist file.

    Returns ``{"hosts": set[str], "forge_endpoints": list[tuple[host, path_prefix]]}``.
    Raises :class:`AllowlistError` if the file is missing or malformed — a missing
    allowlist means *deny everything*, surfaced as an error rather than an open door.
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
        # "host" or "host/path/prefix" — split on the first slash.
        host, _, prefix = entry.partition("/")
        host = host.lower().rstrip(".")
        prefix = "/" + prefix if prefix else "/"
        forge_endpoints.append((host, prefix))

    return {"hosts": hosts, "forge_endpoints": forge_endpoints}


def _assert_resolves_public(host: str, resolver: Resolver) -> None:
    """Reject a public-allowlist host that resolves to any non-public address.

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
            raise AllowlistError(
                f"allowlisted host {host!r} resolves to non-public address {addr} "
                "(SSRF / DNS-rebind guard)"
            )


def validate_url(url: str, allowlist: dict, resolver: Resolver | None = None) -> str:
    """Validate one source URL against the allowlist. Returns the URL if allowed.

    Raises :class:`AllowlistError` with a specific reason otherwise.
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

    # Explicit forge endpoint: trusted internal source, name + path-prefix match. These are
    # allowed to resolve to private forge addresses, so no DNS re-check.
    for eh, eprefix in allowlist.get("forge_endpoints", []):
        if host == eh and parsed.path.startswith(eprefix):
            return url

    # Public host allowlist + resolve-and-recheck.
    if host not in allowlist.get("hosts", set()):
        raise AllowlistError(
            f"host {host!r} is not on the docs-cache allowlist (default-deny). "
            "Add it to doc-cache-allowlist.yml (sysadmin) to cache from it."
        )
    _assert_resolves_public(host, resolver)
    return url
