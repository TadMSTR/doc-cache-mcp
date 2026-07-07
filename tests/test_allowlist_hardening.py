"""Remediation tests — F-02 (path-prefix boundary + traversal) and F-04 (IPv4-mapped IPv6).

These exercise the shared allowlist module (via the doc_cache_mcp.allowlist shim)."""

from __future__ import annotations

import pytest

from doc_cache_mcp.allowlist import AllowlistError, validate_url

ALLOW = {
    "hosts": {"docs.example.com"},
    "forge_endpoints": [("vikunja.helmforge.me", "/api/v1/docs.json")],
}


def _resolver(ip):
    return lambda host: [(2, 1, 6, "", (ip, 0))]


def _unresolvable(host):
    raise OSError("resolver must not be called for forge endpoints")


# --- F-02: forge_endpoint path-prefix boundary + traversal --------------------


def test_forge_endpoint_exact_path_ok():
    u = "https://vikunja.helmforge.me/api/v1/docs.json"
    assert validate_url(u, ALLOW, _unresolvable) == u


def test_forge_endpoint_sibling_prefix_rejected():
    # docs.json.backup shares the string prefix but not the path boundary.
    with pytest.raises(AllowlistError):
        validate_url(
            "https://vikunja.helmforge.me/api/v1/docs.json.backup",
            ALLOW,
            _resolver("93.184.216.34"),
        )


def test_forge_endpoint_suffix_glued_rejected():
    with pytest.raises(AllowlistError):
        validate_url(
            "https://vikunja.helmforge.me/api/v1/docs.jsonanything",
            ALLOW,
            _resolver("93.184.216.34"),
        )


def test_forge_endpoint_traversal_rejected():
    # /api/v1/docs.json/../tasks normalises to /api/v1/tasks — must not match the prefix.
    with pytest.raises(AllowlistError):
        validate_url(
            "https://vikunja.helmforge.me/api/v1/docs.json/../tasks",
            ALLOW,
            _resolver("93.184.216.34"),
        )


def test_forge_endpoint_boundary_subpath_ok():
    # A true sub-path under the prefix (ends on a / boundary) is allowed and skips DNS.
    u = "https://vikunja.helmforge.me/api/v1/docs.json/section"
    assert validate_url(u, ALLOW, _unresolvable) == u


def test_host_prefix_allows_any_path():
    al = {"hosts": set(), "forge_endpoints": [("intra.helmforge.me", "/")]}
    for path in ("/", "/anything", "/deep/path"):
        assert validate_url(f"https://intra.helmforge.me{path}", al, _unresolvable)


# --- F-04: IPv4-mapped / 6to4 IPv6 must not slip a private address through ----


def test_mapped_ipv6_private_rejected():
    # An allowlisted public host that resolves to ::ffff:192.168.1.12 must be refused.
    with pytest.raises(AllowlistError):
        validate_url(
            "https://docs.example.com/x", ALLOW, _resolver("::ffff:192.168.1.12")
        )


def test_mapped_ipv6_loopback_rejected():
    with pytest.raises(AllowlistError):
        validate_url("https://docs.example.com/x", ALLOW, _resolver("::ffff:127.0.0.1"))


def test_mapped_ipv6_public_ok():
    u = "https://docs.example.com/x"
    assert validate_url(u, ALLOW, _resolver("::ffff:93.184.216.34")) == u
