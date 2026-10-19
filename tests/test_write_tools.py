"""add / update / remove through the server, end to end (doc-cache-friction-2026-10 p1).

Two workspaces: ``ws`` with git disabled, for the tools' own rules; ``gitws`` with a real
repo, a local bare remote and push enabled, for the finish-or-fail behaviour that only
shows up across calls.
"""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path

import pytest
import yaml

import doc_cache_mcp.config as config
import doc_cache_mcp.server as server

ALLOW_V2 = """\
hosts: [docs.example.com, raw.githubusercontent.com, github.com]
forge_endpoints: [vikunja.example.internal/api/v1/docs.json]
deny:
  path_shapes:
    github.com: ['^/[^/]+/[^/]+/(blob|raw|tree|releases)/']
provisional:
  enabled: true
  max_hosts: 2
provisional_exclude: [pastebin.com]
"""
CONFIG = """\
services:
  existing:
  - topic: overview
    url: https://docs.example.com/overview
  - topic: install
    url: https://docs.example.com/install
"""


def _e(topic, url):
    return server.DocEntry(topic=topic, url=url)


def _public(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])


class FakeDocSync:
    def __init__(self):
        self.removed = []

    def remove_topics(self, service, topics):
        self.removed.append((service, list(topics)))
        return {"service": service, "files_removed": 3, "state_removed": topics, "pruned": True}


@pytest.fixture
def fake_ds(monkeypatch):
    ds = FakeDocSync()
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)
    return ds


@pytest.fixture
def ws(tmp_path, monkeypatch, fake_ds):
    cfg = tmp_path / "doc-sync.yml"
    cfg.write_text(CONFIG)
    allow = tmp_path / "allow.yml"
    allow.write_text(ALLOW_V2)
    monkeypatch.setenv("DOC_CACHE_MCP_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("DOC_CACHE_MCP_ALLOWLIST_PATH", str(allow))
    monkeypatch.setenv("DOC_CACHE_MCP_GIT_COMMIT", "false")
    monkeypatch.setenv("DOC_CACHE_MCP_AUDIT_LOG_DIR", str(tmp_path / "audit"))
    _public(monkeypatch)
    config.reset_settings()
    return cfg


def _cfg(path: Path) -> dict:
    return yaml.safe_load(path.read_text())["services"]


# --- add: strictly additive ----------------------------------------------------------


def test_add_same_topic_same_url_is_unchanged(ws):
    before = ws.read_text()
    r = server.doc_cache_add_service(
        "existing", [_e("overview", "https://docs.example.com/overview")]
    )
    assert r["outcome"] == "unchanged", r
    assert r["unchanged"] == ["overview"]
    assert ws.read_text() == before


def test_add_existing_topic_with_new_url_is_an_error_naming_update(ws):
    before = ws.read_text()
    r = server.doc_cache_add_service("existing", [_e("overview", "https://docs.example.com/v2")])
    assert r["outcome"] == "refused"
    assert "doc_cache_update_topic" in r["reason"]
    assert ws.read_text() == before


def test_add_warns_on_a_url_already_under_another_topic(ws):
    """The woodpecker duplicate case: two topic names, one URL, double-indexed chunks."""
    r = server.doc_cache_add_service(
        "existing", [_e("install-again", "https://docs.example.com/install")]
    )
    assert r["outcome"] == "written", r
    assert any("'install'" in w for w in r["warnings"])


def test_add_written_outcome_when_git_is_off(ws):
    r = server.doc_cache_add_service("new", [_e("a", "https://docs.example.com/a")])
    assert r["outcome"] == "written"
    assert r["change"] == "additive"
    assert _cfg(ws)["new"] == [{"topic": "a", "url": "https://docs.example.com/a"}]


# --- add: refusals at ADD time (#1167) -----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r/wiki/Page",
        "https://github.com/o/r/%77iki/Page",
        "https://github.com/o//r/wiki/Page",
        "https://raw.githubusercontent.com/%2577iki/o/r/P.md",
        "https://raw.githubusercontent.com/x/../wiki/o/r/P.md",
        "https://github.com/o/r/issues/12",
        "https://pastebin.com/raw/abc",
        "https://user@docs.example.com/x",
        "https://docs.example.com:8443/x",
    ],
)
def test_refused_at_add_time_and_nothing_written(ws, url):
    before = ws.read_text()
    r = server.doc_cache_add_service("new", [_e("t", url)])
    assert r["outcome"] == "refused", r
    assert ws.read_text() == before


# --- add: provisional ----------------------------------------------------------------


def test_provisional_add_is_labelled_and_announced(ws, tmp_path):
    r = server.doc_cache_add_service("vendor", [_e("guide", "https://unvetted.example/guide")])
    assert r["outcome"] == "written", r
    assert r["provisional"] == ["guide"]
    entry = _cfg(ws)["vendor"][0]
    assert entry["trust"] == "provisional"
    assert entry["added"] == date.today().isoformat()

    events = [
        json.loads(x) for x in next((tmp_path / "audit").glob("*.jsonl")).read_text().splitlines()
    ]
    prov = [e for e in events if e["event"] == "doc-cache.provisional.added"]
    assert len(prov) == 1
    assert prov[0]["metadata"]["hosts"] == ["unvetted.example"]
    assert prov[0]["metadata"]["topics"] == ["guide"]


def test_trusted_entries_keep_the_two_key_shape(ws):
    server.doc_cache_add_service("new", [_e("a", "https://docs.example.com/a")])
    assert set(_cfg(ws)["new"][0]) == {"topic", "url"}


def test_max_hosts_is_enforced_and_named(ws):
    for i in (1, 2):
        r = server.doc_cache_add_service(f"v{i}", [_e("a", f"https://h{i}.example/a")])
        assert r["outcome"] == "written", r
    # A second topic on an already-counted host is fine.
    r = server.doc_cache_add_service("v1", [_e("b", "https://h1.example/b")])
    assert r["outcome"] == "written", r
    before = ws.read_text()
    r = server.doc_cache_add_service("v3", [_e("a", "https://h3.example/a")])
    assert r["outcome"] == "refused"
    assert "max_hosts is 2" in r["reason"]
    assert ws.read_text() == before


def test_v1_allowlist_refuses_unlisted_hosts_as_before(ws, tmp_path, monkeypatch):
    allow = tmp_path / "v1.yml"
    allow.write_text("hosts: [docs.example.com]\nforge_endpoints: []\n")
    monkeypatch.setenv("DOC_CACHE_MCP_ALLOWLIST_PATH", str(allow))
    config.reset_settings()
    r = server.doc_cache_add_service("vendor", [_e("guide", "https://unvetted.example/guide")])
    assert r["outcome"] == "refused"
    assert "default-deny" in r["reason"]


# --- update_topic --------------------------------------------------------------------


def test_update_repoints_in_place(ws):
    r = server.doc_cache_update_topic("existing", "overview", "https://docs.example.com/v2")
    assert r["outcome"] == "written", r
    assert r["previous_url"] == "https://docs.example.com/overview"
    assert r["change"] == "repoint"
    assert [e["topic"] for e in _cfg(ws)["existing"]] == ["overview", "install"]
    assert _cfg(ws)["existing"][0]["url"] == "https://docs.example.com/v2"


def test_update_of_a_missing_topic_names_add(ws):
    r = server.doc_cache_update_topic("existing", "nope", "https://docs.example.com/v2")
    assert r["outcome"] == "refused"
    assert "doc_cache_add_service" in r["reason"]


def test_update_to_the_same_url_is_unchanged(ws):
    r = server.doc_cache_update_topic("existing", "overview", "https://docs.example.com/overview")
    assert r["outcome"] == "unchanged"


def test_update_is_classified_like_add(ws):
    r = server.doc_cache_update_topic("existing", "overview", "https://github.com/o/r/wiki")
    assert r["outcome"] == "refused"


def test_repointing_a_provisional_topic_keeps_its_added_date(ws):
    server.doc_cache_add_service("vendor", [_e("g", "https://unvetted.example/g")])
    data = yaml.safe_load(ws.read_text())
    data["services"]["vendor"][0]["added"] = "2026-01-01"
    ws.write_text(yaml.safe_dump(data, sort_keys=False))
    server.doc_cache_update_topic("vendor", "g", "https://unvetted.example/g2")
    assert _cfg(ws)["vendor"][0]["added"] == "2026-01-01"


def test_repointing_onto_a_trusted_host_drops_the_provisional_fields(ws):
    server.doc_cache_add_service("vendor", [_e("g", "https://unvetted.example/g")])
    server.doc_cache_update_topic("vendor", "g", "https://docs.example.com/g")
    assert _cfg(ws)["vendor"][0] == {"topic": "g", "url": "https://docs.example.com/g"}


# --- remove_topic --------------------------------------------------------------------


def test_remove_topics_and_their_cache(ws, fake_ds):
    r = server.doc_cache_remove_topic("existing", ["install"])
    assert r["outcome"] == "written", r
    assert r["change"] == "remove"
    assert [e["topic"] for e in _cfg(ws)["existing"]] == ["overview"]
    assert fake_ds.removed == [("existing", ["install"])]
    assert r["cache"]["ok"] is True


def test_remove_every_topic_needs_remove_service(ws, fake_ds):
    before = ws.read_text()
    r = server.doc_cache_remove_topic("existing", ["install", "overview"])
    assert r["outcome"] == "refused"
    assert "remove_service=true" in r["reason"]
    assert ws.read_text() == before
    assert fake_ds.removed == []


def test_remove_service_drops_the_key(ws, fake_ds):
    r = server.doc_cache_remove_topic("existing", [], remove_service=True)
    assert r["outcome"] == "written", r
    assert "existing" not in _cfg(ws)
    assert r["service_removed"] is True
    assert fake_ds.removed == [("existing", ["overview", "install"])]


def test_remove_service_with_a_partial_list_is_refused(ws):
    r = server.doc_cache_remove_topic("existing", ["install"], remove_service=True)
    assert r["outcome"] == "refused"


def test_remove_is_all_or_nothing(ws, fake_ds):
    before = ws.read_text()
    r = server.doc_cache_remove_topic("existing", ["install", "ghost"])
    assert r["outcome"] == "refused"
    assert "ghost" in r["reason"]
    assert ws.read_text() == before
    assert fake_ds.removed == []


def test_remove_cap(ws, monkeypatch):
    monkeypatch.setenv("DOC_CACHE_MCP_MAX_REMOVE_PER_CALL", "1")
    config.reset_settings()
    r = server.doc_cache_remove_topic("existing", ["install", "overview"], remove_service=True)
    assert r["outcome"] == "refused"
    assert "max_remove_per_call is 1" in r["reason"]


def test_remove_against_an_old_doc_sync_says_the_cache_was_left(ws, monkeypatch):
    monkeypatch.setattr(server, "load_doc_sync", lambda: object())
    r = server.doc_cache_remove_topic("existing", ["install"])
    assert r["outcome"] == "written"
    assert r["cache"]["ok"] is False
    assert "no remove_topics" in r["cache"]["error"]


# --- across calls, with git and push -------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def gitws(tmp_path, monkeypatch, fake_ds):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    repo = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _git(repo, "remote", "add", "origin", str(bare))
    cfg = repo / "scripts" / "doc-sync.yml"
    cfg.parent.mkdir()
    cfg.write_text(CONFIG)
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Ted", "-c", "user.email=t@x", "commit", "-qm", "init")
    _git(repo, "push", "-q", "-u", "origin", "main")
    key = tmp_path / "key"
    key.write_text("not-a-key")
    allow = tmp_path / "allow.yml"
    allow.write_text(ALLOW_V2)
    for k, v in {
        "CONFIG_PATH": str(cfg),
        "ALLOWLIST_PATH": str(allow),
        "GIT_COMMIT": "true",
        "GIT_PUSH": "true",
        "DEPLOY_KEY_PATH": str(key),
    }.items():
        monkeypatch.setenv(f"DOC_CACHE_MCP_{k}", v)
    _public(monkeypatch)
    config.reset_settings()
    return {"repo": repo, "cfg": cfg, "bare": bare}


def test_repoint_then_add_both_push(gitws):
    """vikunja#653: the call sequence that poisoned every later push in v0.2.2."""
    first = server.doc_cache_update_topic("existing", "overview", "https://docs.example.com/v2")
    second = server.doc_cache_add_service("new", [_e("a", "https://docs.example.com/a")])

    assert first["outcome"] == "pushed", first
    assert second["outcome"] == "pushed", second
    assert second["reason"] is None
    assert "overview" not in json.dumps(second)
    repo = gitws["repo"]
    assert _git(repo, "rev-parse", "HEAD") == _git(repo, "rev-parse", "origin/main")
    assert _git(repo, "log", "--format=%ae", "-2").split() == ["doc-cache-mcp@forge"] * 2


def test_rejected_push_rolls_back_and_the_next_call_is_clean(gitws):
    hook = gitws["bare"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = gitws["cfg"].read_text()

    r = server.doc_cache_add_service("new", [_e("a", "https://docs.example.com/a")])
    assert r["outcome"] == "rolled_back", r
    assert gitws["cfg"].read_text() == before

    hook.unlink()
    r = server.doc_cache_add_service("new2", [_e("b", "https://docs.example.com/b")])
    assert r["outcome"] == "pushed", r
    assert r["reason"] is None


def test_behind_origin_is_refused_before_writing(gitws, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(gitws["bare"]), str(other)], check=True)
    (other / "README").write_text("x")
    _git(other, "add", "README")
    _git(other, "-c", "user.name=Ted", "-c", "user.email=t@x", "commit", "-qm", "ahead")
    _git(other, "push", "-q", "origin", "main")
    before = gitws["cfg"].read_text()

    r = server.doc_cache_add_service("new", [_e("a", "https://docs.example.com/a")])

    assert r["outcome"] == "refused"
    assert "1 behind" in r["reason"]
    assert gitws["cfg"].read_text() == before


def test_remove_cache_cleanup_waits_for_the_config_change(gitws, fake_ds):
    hook = gitws["bare"] / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    r = server.doc_cache_remove_topic("existing", ["install"])
    assert r["outcome"] == "rolled_back"
    assert "cache" not in r
    assert fake_ds.removed == []


def test_push_without_a_deploy_key_is_refused_before_writing(gitws, monkeypatch):
    monkeypatch.delenv("DOC_CACHE_MCP_DEPLOY_KEY_PATH")
    config.reset_settings()
    before = gitws["cfg"].read_text()
    r = server.doc_cache_add_service("new", [_e("a", "https://docs.example.com/a")])
    assert r["outcome"] == "refused"
    assert gitws["cfg"].read_text() == before


@pytest.mark.parametrize("bad", ["svc\n", "svc\r\n"])
def test_trailing_newline_in_a_service_name_is_refused(ws, bad):
    """IV-01: `$` matches before a trailing newline, so `.match` accepted these."""
    r = server.doc_cache_add_service(bad, [_e("a", "https://docs.example.com/a")])
    assert r["outcome"] == "refused"


def test_trailing_newline_in_a_topic_is_refused(ws):
    """update_topic takes the topic verbatim (add strips it), so it is the reachable path."""
    r = server.doc_cache_update_topic("existing", "overview\n", "https://docs.example.com/x")
    assert r["outcome"] == "refused"
    assert "invalid topic" in r["reason"]


class StatefulFakeDocSync(FakeDocSync):
    def __init__(self, state):
        super().__init__()
        self.state = state

    def load_state(self):
        return self.state


def test_a_failed_cache_cleanup_can_be_retried(ws, monkeypatch):
    """F-06: the config entry is gone, the chunks are not; naming it again cleans up."""
    ds = StatefulFakeDocSync({"existing": [{"topic": "gone", "url": "u"}]})
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)
    before = ws.read_text()
    r = server.doc_cache_remove_topic("existing", ["gone"])
    assert r["outcome"] == "unchanged", r
    assert r["orphans"] == ["gone"]
    assert ds.removed == [("existing", ["gone"])]
    assert ws.read_text() == before


def test_an_unknown_topic_with_no_state_is_still_refused(ws, monkeypatch):
    ds = StatefulFakeDocSync({})
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)
    r = server.doc_cache_remove_topic("existing", ["ghost"])
    assert r["outcome"] == "refused"
    assert ds.removed == []


def test_cleanup_of_an_orphan_alongside_a_real_removal(ws, monkeypatch):
    ds = StatefulFakeDocSync({"existing": [{"topic": "gone", "url": "u"}]})
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)
    r = server.doc_cache_remove_topic("existing", ["install", "gone"])
    assert r["outcome"] == "written", r
    assert ds.removed == [("existing", ["install", "gone"])]


def test_listing_shows_the_tier_the_chunks_were_written_under(ws, monkeypatch):
    """F-02 follow-up: the label used to come from the configured host alone."""

    class DS:
        def load_config(self):
            return {"services": {"s": [{"topic": "t", "url": "https://docs.example.com/x"}]}}

        def load_state(self):
            return {
                "s": [
                    {"topic": "t", "url": "u", "chunks": 1, "synced": "x", "trust": "provisional"}
                ]
            }

    monkeypatch.setattr(server, "load_doc_sync", lambda: DS())
    row = server.doc_cache_list_services(service="s")["services"][0]["topics"][0]
    assert row["trust"] == "provisional"


def test_a_failed_whole_service_cleanup_can_be_retried_with_an_empty_list(ws, monkeypatch):
    """R2-03: the docstring says retry with the same arguments; for remove_service=true
    with no topics that used to be refused as an unknown service."""
    ds = StatefulFakeDocSync({"goneservice": [{"topic": "a"}, {"topic": "b"}]})
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)
    r = server.doc_cache_remove_topic("goneservice", [], remove_service=True)
    assert r["outcome"] == "unchanged", r
    assert r["orphans"] == ["a", "b"]
    assert ds.removed == [("goneservice", ["a", "b"])]


def test_an_unknown_service_with_no_state_is_still_refused(ws, monkeypatch):
    monkeypatch.setattr(server, "load_doc_sync", lambda: StatefulFakeDocSync({}))
    r = server.doc_cache_remove_topic("nothere", [], remove_service=True)
    assert r["outcome"] == "refused"
