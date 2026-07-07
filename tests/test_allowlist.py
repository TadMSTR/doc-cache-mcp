"""Allowlist / SSRF guard — the security core. Bypass cases must all fail closed."""

from __future__ import annotations

import pytest

from doc_cache_mcp.allowlist import AllowlistError, load_allowlist, validate_url

ALLOW = {
    "hosts": {"raw.githubusercontent.com", "docs.example.com"},
    "forge_endpoints": [("vikunja.helmforge.me", "/api/v1/docs.json")],
}


def _resolver(ip):
    return lambda host: [(2, 1, 6, "", (ip, 0))]


PUBLIC = _resolver("93.184.216.34")
PRIVATE = _resolver("192.168.1.12")


def _unresolvable(host):
    raise OSError("Name or service not known")


# --- scheme / host shape --------------------------------------------------


def test_http_scheme_rejected():
    with pytest.raises(AllowlistError):
        validate_url("http://raw.githubusercontent.com/a/README.md", ALLOW, PUBLIC)


def test_ftp_scheme_rejected():
    with pytest.raises(AllowlistError):
        validate_url("ftp://raw.githubusercontent.com/a", ALLOW, PUBLIC)


def test_empty_url_rejected():
    with pytest.raises(AllowlistError):
        validate_url("   ", ALLOW, PUBLIC)


def test_no_host_rejected():
    with pytest.raises(AllowlistError):
        validate_url("https:///path-only", ALLOW, PUBLIC)


# --- IP literals (classic SSRF payload shapes) ----------------------------


def test_link_local_metadata_ip_rejected():
    # 169.254.169.254 — the cloud metadata service, the canonical SSRF target.
    with pytest.raises(AllowlistError):
        validate_url("https://169.254.169.254/latest/meta-data/", ALLOW, PUBLIC)


def test_private_ip_literal_rejected():
    with pytest.raises(AllowlistError):
        validate_url("https://192.168.1.12/x", ALLOW, PUBLIC)


def test_loopback_ip_literal_rejected():
    with pytest.raises(AllowlistError):
        validate_url("https://127.0.0.1/x", ALLOW, PUBLIC)


def test_public_ip_literal_rejected():
    # Even a public IP literal is refused — the allowlist is name-based.
    with pytest.raises(AllowlistError):
        validate_url("https://93.184.216.34/x", ALLOW, PUBLIC)


# --- host allowlist -------------------------------------------------------


def test_public_allowlisted_host_ok():
    u = "https://raw.githubusercontent.com/o/r/main/README.md"
    assert validate_url(u, ALLOW, PUBLIC) == u


def test_host_not_on_allowlist_rejected():
    with pytest.raises(AllowlistError):
        validate_url("https://evil.example.net/x", ALLOW, PUBLIC)


def test_forge_host_not_explicitly_listed_rejected():
    # A forge host that is NOT in forge_endpoints and NOT in hosts must be refused.
    with pytest.raises(AllowlistError):
        validate_url("https://grafana.helmforge.me/x", ALLOW, PUBLIC)


# --- DNS-rebind guard -----------------------------------------------------


def test_allowlisted_host_resolving_private_rejected():
    # raw.githubusercontent.com is allowlisted, but if it resolves to a private IP the
    # rebind guard must reject the fetch.
    with pytest.raises(AllowlistError):
        validate_url("https://raw.githubusercontent.com/x", ALLOW, PRIVATE)


def test_unresolvable_allowlisted_host_denied():
    # Default-deny: cannot prove it is safe, so refuse.
    with pytest.raises(AllowlistError):
        validate_url("https://docs.example.com/x", ALLOW, _unresolvable)


# --- forge endpoints ------------------------------------------------------


def test_forge_endpoint_allowed_without_dns_recheck():
    # Forge endpoints are explicitly trusted and may resolve internal — the resolver here
    # would raise, proving it is never consulted for a forge endpoint.
    u = "https://vikunja.helmforge.me/api/v1/docs.json"
    assert validate_url(u, ALLOW, _unresolvable) == u


def test_forge_endpoint_path_prefix_enforced():
    with pytest.raises(AllowlistError):
        validate_url("https://vikunja.helmforge.me/api/v1/other", ALLOW, _unresolvable)


def test_forge_endpoint_wrong_host_rejected():
    with pytest.raises(AllowlistError):
        validate_url("https://evil.helmforge.me/api/v1/docs.json", ALLOW, PUBLIC)


# --- allowlist file loading ----------------------------------------------


def test_load_missing_allowlist_fails_closed(tmp_path):
    with pytest.raises(AllowlistError):
        load_allowlist(tmp_path / "nope.yml")


def test_load_non_mapping_rejected(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(AllowlistError):
        load_allowlist(p)


def test_load_normalises_hosts_and_endpoints(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text(
        "hosts:\n  - Raw.GitHubUserContent.com\n"
        "forge_endpoints:\n  - Vikunja.Helmforge.me/api/v1/docs.json\n"
    )
    al = load_allowlist(p)
    assert "raw.githubusercontent.com" in al["hosts"]
    assert al["forge_endpoints"] == [("vikunja.helmforge.me", "/api/v1/docs.json")]


def test_seeded_allowlist_loads_and_enforces(tmp_path):
    # The real seeded allowlist should load and allow a known host, deny an unknown one.
    from pathlib import Path

    real = (
        Path.home()
        / "repos"
        / "gitea"
        / "host-forge-scripts"
        / "doc-cache-allowlist.yml"
    )
    if not real.exists():
        pytest.skip("seeded allowlist not present")
    al = load_allowlist(real)
    assert "raw.githubusercontent.com" in al["hosts"]
    assert ("vikunja.helmforge.me", "/api/v1/docs.json") in al["forge_endpoints"]
    with pytest.raises(AllowlistError):
        validate_url("https://evil.example.net/x", al, PUBLIC)
