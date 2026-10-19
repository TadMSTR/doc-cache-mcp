"""classify_url — deny wins, suffix hosts, the open-edit floor, path shapes, and the
provisional tier (doc-cache-friction-2026-10 p1, D1 = A; vikunja#1167)."""

from __future__ import annotations

import pytest

from doc_cache_mcp.allowlist import (
    PROVISIONAL,
    TRUSTED,
    AllowlistError,
    classify_url,
    host_matches,
    host_tier,
    load_allowlist,
    lower_trust,
    validate_url,
)


def _resolver(ip):
    return lambda host: [(2, 1, 6, "", (ip, 0))]


PUBLIC = _resolver("93.184.216.34")
PRIVATE = _resolver("10.1.2.3")

V1 = """\
hosts:
  - docs.docker.com
  - github.com
forge_endpoints:
  - vikunja.example.internal/api/v1/docs.json
"""
V2 = """\
hosts: [docs.docker.com, .opensearch.org, github.com]
forge_endpoints: [vikunja.example.internal/api/v1/docs.json]
deny:
  open_edit:
    docs.docker.com: ['^/community/']
  path_shapes:
    github.com: ['^/[^/]+/[^/]+/(blob|raw|tree|releases)/']
provisional:
  enabled: true
  ttl_days: 30
  max_hosts: 25
  max_bytes: 2097152
provisional_exclude: [pastebin.com, .ngrok-free.app]
"""


@pytest.fixture
def v1(tmp_path):
    p = tmp_path / "v1.yml"
    p.write_text(V1)
    return load_allowlist(p)


@pytest.fixture
def v2(tmp_path):
    p = tmp_path / "v2.yml"
    p.write_text(V2)
    return load_allowlist(p)


# --- v1 files: provisional off, behaviour as before ----------------------------------


def test_v1_file_has_provisional_disabled(v1):
    assert v1["provisional"]["enabled"] is False


def test_v1_unlisted_host_is_refused_as_before(v1):
    with pytest.raises(AllowlistError, match="default-deny"):
        classify_url("https://unvetted.example/docs", v1, PUBLIC)


def test_v1_listed_host_is_trusted(v1):
    assert classify_url("https://docs.docker.com/x", v1, PUBLIC).trust == TRUSTED


def test_v1_still_refuses_wikis_at_classify_time(v1):
    """The floor needs no deny block: a v1 file cannot turn wiki refusal off (#1167)."""
    with pytest.raises(AllowlistError, match="open-edit"):
        classify_url("https://github.com/o/r/wiki/Page", v1, PUBLIC)


# --- structural ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.docker.com@evil.example/x",
        "https://user:pw@docs.docker.com/x",
        "https://docs.docker.com:8443/x",
        "https://docs.docker.com:99999/x",
    ],
)
def test_userinfo_and_ports_are_refused(v2, url):
    with pytest.raises(AllowlistError):
        classify_url(url, v2, PUBLIC)


def test_port_443_is_allowed(v2):
    assert classify_url("https://docs.docker.com:443/x", v2, PUBLIC).trust == TRUSTED


# --- suffix hosts --------------------------------------------------------------------


def test_suffix_entry_matches_apex_and_subdomains_on_a_label_boundary():
    assert host_matches("opensearch.org", ".opensearch.org")
    assert host_matches("docs.opensearch.org", ".opensearch.org")
    assert host_matches("a.b.opensearch.org", ".opensearch.org")
    assert not host_matches("evilopensearch.org", ".opensearch.org")
    assert not host_matches("opensearch.org.evil.net", ".opensearch.org")
    assert not host_matches("docs.docker.com.evil.net", "docs.docker.com")


def test_suffix_host_is_trusted(v2):
    assert classify_url("https://docs.opensearch.org/latest/", v2, PUBLIC).trust == TRUSTED


# --- deny wins -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/wiki/Page",
        "https://github.com/o/r/wiki.atom",
        "https://raw.githubusercontent.com/wiki/o/r/P.md",
        "https://gitlab.com/g/p/-/wikis/Home",
        "https://wiki.debian.org/Anything",
        # R2-01 bypass spellings, now refused by the shared code at add time too.
        "https://github.com/o/r/%77iki/Page",
        "https://github.com/o//r/wiki/Page",
        "https://raw.githubusercontent.com/%2577iki/o/r/P.md",
        "https://raw.githubusercontent.com/x/../wiki/o/r/P.md",
        "https://raw.githubusercontent.com/WIKI/o/r/P.md",
    ],
)
def test_open_edit_floor_is_refused_on_every_tier(v2, url):
    with pytest.raises(AllowlistError, match="open-edit"):
        classify_url(url, v2, PUBLIC)


def test_a_deny_block_without_wikis_cannot_remove_the_floor(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text("hosts: [github.com]\ndeny:\n  open_edit: {}\n")
    with pytest.raises(AllowlistError, match="open-edit"):
        classify_url("https://github.com/o/r/wiki", load_allowlist(p), PUBLIC)


def test_open_edit_additions_apply(v2):
    with pytest.raises(AllowlistError, match="open-edit"):
        classify_url("https://docs.docker.com/community/forum", v2, PUBLIC)


def test_path_shapes_limit_a_trusted_host(v2):
    assert classify_url("https://github.com/o/r/blob/main/README.md", v2, PUBLIC).trust == TRUSTED
    for bad in (
        "https://github.com/o/r/issues/1",
        "https://github.com/o/r/pull/2",
        "https://github.com/o",
    ):
        with pytest.raises(AllowlistError, match="not a permitted shape"):
            classify_url(bad, v2, PUBLIC)


def test_a_wiki_is_refused_even_where_hosts_would_trust_it(v2):
    """Deny is evaluated before hosts: being allowlisted does not re-admit a wiki."""
    with pytest.raises(AllowlistError, match="open-edit"):
        classify_url("https://github.com/o/r/wiki/Home", v2, PUBLIC)


# --- provisional ---------------------------------------------------------------------


def test_unlisted_public_host_is_provisional(v2):
    v = classify_url("https://unvetted.example/docs", v2, PUBLIC)
    assert v.trust == PROVISIONAL
    assert v.host == "unvetted.example"


@pytest.mark.parametrize("url", ["https://pastebin.com/raw/x", "https://abc.ngrok-free.app/x"])
def test_excluded_hosts_are_refused(v2, url):
    with pytest.raises(AllowlistError, match="provisional_exclude"):
        classify_url(url, v2, PUBLIC)


def test_provisional_host_resolving_private_is_refused(v2):
    with pytest.raises(AllowlistError, match="non-public"):
        classify_url("https://unvetted.example/docs", v2, PRIVATE)


def test_provisional_host_that_does_not_resolve_is_refused(v2):
    def nx(host):
        raise OSError("NXDOMAIN")

    with pytest.raises(AllowlistError, match="cannot resolve"):
        classify_url("https://unvetted.example/docs", v2, nx)


def test_promotion_is_one_line_in_hosts(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text(V2)
    assert (
        classify_url("https://unvetted.example/d", load_allowlist(p), PUBLIC).trust == PROVISIONAL
    )
    p.write_text(V2.replace("hosts: [", "hosts: [unvetted.example, "))
    assert classify_url("https://unvetted.example/d", load_allowlist(p), PUBLIC).trust == TRUSTED


def test_a_redirect_never_escalates_trust():
    assert lower_trust(PROVISIONAL, TRUSTED) == PROVISIONAL
    assert lower_trust(TRUSTED, PROVISIONAL) == PROVISIONAL
    assert lower_trust(TRUSTED, TRUSTED) == TRUSTED


def test_validate_url_keeps_its_trusted_only_meaning(v2):
    """Older callers asking "may this enter the trusted cache?" still get no for provisional."""
    with pytest.raises(AllowlistError, match="provisional"):
        validate_url("https://unvetted.example/docs", v2, PUBLIC)
    assert validate_url("https://docs.docker.com/x", v2, PUBLIC) == "https://docs.docker.com/x"


def test_host_tier_needs_no_dns(v2, v1):
    assert host_tier("docs.opensearch.org", v2) == TRUSTED
    assert host_tier("vikunja.example.internal", v2) == TRUSTED
    assert host_tier("unvetted.example", v2) == PROVISIONAL
    assert host_tier("pastebin.com", v2) is None
    assert host_tier("unvetted.example", v1) is None


# --- malformed blocks fail closed ----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "hosts: []\ndeny: [x]\n",
        "hosts: []\ndeny:\n  open_edit: [x]\n",
        "hosts: []\ndeny:\n  path_shapes:\n    github.com: ['(unclosed']\n",
        "hosts: []\nprovisional: yes-please\n",
        "hosts: []\nprovisional:\n  enabled: true\n  max_hosts: 0\n",
        "hosts: []\nprovisional:\n  enabled: true\n  ttl_days: thirty\n",
    ],
)
def test_malformed_policy_blocks_are_refused(tmp_path, text):
    p = tmp_path / "bad.yml"
    p.write_text(text)
    with pytest.raises(AllowlistError):
        load_allowlist(p)


def test_provisional_needs_enabled_true_exactly(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text("hosts: []\nprovisional:\n  enabled: 'yes'\n")
    assert load_allowlist(p)["provisional"]["enabled"] is False


# --- pre-audit baseline findings -----------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://vikunja.example.internal/api/v1/docs.json/%2e%2e/tasks/all",
        "https://vikunja.example.internal/api/v1/docs.json/%252e%252e/tasks",
        "https://vikunja.example.internal/api/v1/docs.json/.%2e/tasks",
    ],
)
def test_encoded_dot_segments_cannot_leave_a_forge_endpoint_prefix(v2, url):
    """F-1: requests decodes %2e before sending, so the prefix is matched decoded."""
    with pytest.raises(AllowlistError):
        classify_url(url, v2, PRIVATE)


def test_a_forge_endpoint_still_matches_exactly(v2):
    u = "https://vikunja.example.internal/api/v1/docs.json"
    assert classify_url(u, v2, PRIVATE).trust == TRUSTED


@pytest.mark.parametrize(
    "url",
    [
        "https://docs.docker.com/a\ntrust: trusted\n---\nx",
        "https://docs.docker.com/a\rb",
        "https://docs.docker.com/a\tb",
        "https://docs.docker.com/a b",
        "https://docs.docker.com/a\x00b",
        "https://docs.docker.com/a\x85b",
    ],
)
def test_whitespace_and_control_characters_are_refused(v2, url):
    """F-2: urlparse drops \\t\\r\\n, so these classified clean and reached the frontmatter."""
    with pytest.raises(AllowlistError, match="control characters"):
        classify_url(url, v2, PUBLIC)


def test_overlong_url_is_refused_at_classify_time(v2):
    with pytest.raises(AllowlistError, match="exceeds"):
        classify_url("https://docs.docker.com/" + "a" * 2100, v2, PUBLIC)


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.127.255.254", "192.0.0.8"])
def test_non_global_addresses_are_refused(v2, ip):
    """F-3: CGNAT and other special-purpose ranges are not `is_private`, but not public."""
    with pytest.raises(AllowlistError, match="non-public"):
        classify_url("https://unvetted.example/docs", v2, _resolver(ip))


# --- audit round 1 remediation ---------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "fec0::1",  # F-08: is_global on 3.13, so it needs naming
        "100.64.0.1",
        "192.0.0.8",
        "198.18.0.1",
        "64:ff9b:1::1",
        "::ffff:127.0.0.1",
        "::",
        "0.0.0.0",
        "fe80::1",
        "fc00::1",
    ],
)
def test_every_probed_non_public_range_is_refused(v2, ip):
    with pytest.raises(AllowlistError, match="non-public"):
        classify_url("https://unvetted.example/docs", v2, _resolver(ip))


def test_refusal_does_not_echo_the_resolved_address(v2):
    """F-11: with the provisional tier any name gets here; no internal DNS oracle."""
    with pytest.raises(AllowlistError) as e:
        classify_url("https://unvetted.example/docs", v2, _resolver("192.168.1.12"))
    assert "192.168" not in str(e.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/g/p/-/wikis;x/home",
        "https://raw.githubusercontent.com/wiki;/o/r/Home.md",
        "https://docs.docker.com/a;b/c",
        "https://docs.docker.com/a/b;c",
        # R2-04: the floor rules see the decoded path, so %3B is the same `;` to them.
        "https://gitlab.com/g/p/-/wikis%3Bx/home",
        "https://gitlab.com/g/p/-/wikis%3bx/home",
        "https://raw.githubusercontent.com/wiki%3B/o/r/Home.md",
        "https://docs.docker.com/a%253Bb",
    ],
)
def test_semicolons_in_the_path_are_refused(v2, url):
    """F-12: urlparse only splits ;params off the last segment, so a mid-path ; slipped
    past boundary-anchored floor rules."""
    with pytest.raises(AllowlistError, match="';'"):
        classify_url(url, v2, PUBLIC)


@pytest.mark.parametrize(
    "text",
    [
        "provisional:\n  enabled: true\n",
        "hosts: []\nforge_endpoints: []\nprovisional:\n  enabled: true\n",
    ],
)
def test_enabled_provisional_with_nothing_trusted_is_refused(tmp_path, text):
    """R2-01: it would classify every configured topic provisional and expire them all."""
    p = tmp_path / "a.yml"
    p.write_text(text)
    with pytest.raises(AllowlistError, match="no hosts and no forge_endpoints"):
        load_allowlist(p)


def test_an_empty_hosts_list_is_still_fine_with_provisional_off(tmp_path):
    p = tmp_path / "a.yml"
    p.write_text("hosts: []\n")
    assert load_allowlist(p)["hosts"] == set()
