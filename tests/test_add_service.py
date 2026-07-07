"""doc_cache_add_service — structural YAML merge, dedup idempotence, validation gating,
and single-file git commit."""

from __future__ import annotations

import subprocess

import pytest
import yaml

import doc_cache_mcp.config as config
import doc_cache_mcp.server as server


def _entry(topic, url):
    return server.DocEntry(topic=topic, url=url)


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A workspace with a doc-sync.yml (with a comment + one existing service) and an
    allowlist. Git commit disabled; DNS forced to a public address."""
    cfg = tmp_path / "doc-sync.yml"
    cfg.write_text(
        "# doc-sync configuration — keep me\n"
        "services:\n"
        "  existing:\n"
        "    - topic: overview\n"
        "      url: https://raw.githubusercontent.com/o/r/main/README.md\n"
    )
    allow = tmp_path / "allow.yml"
    allow.write_text(
        "hosts:\n  - raw.githubusercontent.com\n  - docs.example.com\n"
        "forge_endpoints:\n  - vikunja.helmforge.me/api/v1/docs.json\n"
    )
    monkeypatch.setenv("DOC_CACHE_MCP_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("DOC_CACHE_MCP_ALLOWLIST_PATH", str(allow))
    monkeypatch.setenv("DOC_CACHE_MCP_GIT_COMMIT", "false")
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    config.reset_settings()
    return cfg


def test_add_new_service(ws):
    r = server.doc_cache_add_service(
        "newsvc", [_entry("overview", "https://docs.example.com/a")]
    )
    assert "error" not in r, r
    data = yaml.safe_load(ws.read_text())
    assert data["services"]["newsvc"] == [
        {"topic": "overview", "url": "https://docs.example.com/a"}
    ]
    # Existing service is preserved by the structural merge.
    assert "existing" in data["services"]


def test_add_is_idempotent(ws):
    e = [_entry("overview", "https://docs.example.com/a")]
    server.doc_cache_add_service("newsvc", e)
    server.doc_cache_add_service("newsvc", e)
    data = yaml.safe_load(ws.read_text())
    assert len(data["services"]["newsvc"]) == 1  # dedup by topic, no growth


def test_add_replaces_topic_url(ws):
    server.doc_cache_add_service("newsvc", [_entry("t", "https://docs.example.com/a")])
    server.doc_cache_add_service("newsvc", [_entry("t", "https://docs.example.com/b")])
    data = yaml.safe_load(ws.read_text())
    assert [e["url"] for e in data["services"]["newsvc"]] == [
        "https://docs.example.com/b"
    ]


def test_add_extends_existing_service(ws):
    r = server.doc_cache_add_service(
        "existing", [_entry("install", "https://docs.example.com/i")]
    )
    assert "error" not in r, r
    data = yaml.safe_load(ws.read_text())
    topics = [e["topic"] for e in data["services"]["existing"]]
    assert topics == ["overview", "install"]  # original kept, new appended


def test_add_forge_endpoint_ok(ws):
    r = server.doc_cache_add_service(
        "vikunja", [_entry("api", "https://vikunja.helmforge.me/api/v1/docs.json")]
    )
    assert "error" not in r, r


def test_reject_insecure_url_writes_nothing(ws):
    before = ws.read_text()
    r = server.doc_cache_add_service(
        "newsvc", [_entry("t", "http://docs.example.com/a")]
    )
    assert "error" in r
    assert ws.read_text() == before


def test_reject_unlisted_host_writes_nothing(ws):
    before = ws.read_text()
    r = server.doc_cache_add_service(
        "newsvc", [_entry("t", "https://evil.example.net/a")]
    )
    assert "error" in r
    assert ws.read_text() == before


def test_all_or_nothing_when_one_url_bad(ws):
    before = ws.read_text()
    r = server.doc_cache_add_service(
        "newsvc",
        [
            _entry("good", "https://docs.example.com/a"),
            _entry("bad", "https://evil.example.net/a"),
        ],
    )
    assert "error" in r
    assert ws.read_text() == before  # the good entry must not have been written either


def test_reject_bad_service_name(ws):
    r = server.doc_cache_add_service(
        "bad name!", [_entry("t", "https://docs.example.com/a")]
    )
    assert "error" in r


def test_reject_bad_topic(ws):
    r = server.doc_cache_add_service(
        "svc", [_entry("bad topic", "https://docs.example.com/a")]
    )
    assert "error" in r


def test_reject_topic_with_newline_injection(ws):
    before = ws.read_text()
    r = server.doc_cache_add_service(
        "svc", [_entry("evil\ninjected: true", "https://docs.example.com/a")]
    )
    assert "error" in r
    assert ws.read_text() == before


def test_reject_empty_entries(ws):
    r = server.doc_cache_add_service("svc", [])
    assert "error" in r


def test_reject_duplicate_topic_in_request(ws):
    r = server.doc_cache_add_service(
        "svc",
        [
            _entry("t", "https://docs.example.com/a"),
            _entry("t", "https://docs.example.com/b"),
        ],
    )
    assert "error" in r


def test_git_commit_scopes_to_single_file(tmp_path, monkeypatch):
    """With git_commit enabled, the add commits only doc-sync.yml — not an unrelated dirty
    file in the same repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    cfg = repo / "doc-sync.yml"
    cfg.write_text(
        "services:\n  existing:\n    - topic: overview\n      url: https://docs.example.com/x\n"
    )
    unrelated = repo / "other.txt"
    unrelated.write_text("dirty\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
    # Now dirty the unrelated file — it must NOT be swept into the doc-cache commit.
    unrelated.write_text("dirty changed\n")

    allow = tmp_path / "allow.yml"
    allow.write_text("hosts:\n  - docs.example.com\nforge_endpoints: []\n")
    monkeypatch.setenv("DOC_CACHE_MCP_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("DOC_CACHE_MCP_ALLOWLIST_PATH", str(allow))
    monkeypatch.setenv("DOC_CACHE_MCP_GIT_COMMIT", "true")
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
    )
    config.reset_settings()

    r = server.doc_cache_add_service(
        "newsvc", [_entry("t", "https://docs.example.com/a")]
    )
    assert "error" not in r, r
    assert r["commit"]["committed"] is True, r["commit"]

    # The last commit changed exactly doc-sync.yml.
    files = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert files == ["doc-sync.yml"], files
    # The unrelated file is still dirty (uncommitted).
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--short", "other.txt"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "other.txt" in status
