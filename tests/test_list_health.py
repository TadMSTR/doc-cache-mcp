"""doc_cache_list_services — a compact health summary by default (#647, #653).

The v0.2 listing returned every topic's row: 56.6 KB at forge's 104 services, over the MCP
result limit, so the client spilled it to a file. And because a failed sync never touched
state, it showed 0 failing topics on a night with 15 errors.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import doc_cache_mcp.server as server

TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=10)).isoformat()
ERR = "HTTPError: 404 Client Error: Not Found for url: https://docs.example.com/" + "x" * 150


class ForgeSizedDocSync:
    """104 services, ~350 topics, 15 failing with full-length errors — forge, 2026-10-19."""

    def __init__(self):
        self.config = {"services": {}}
        self.state = {}
        n = 0
        for s in range(104):
            name = f"service-name-{s:03d}"
            topics = [
                {"topic": f"topic-{t}", "url": f"https://docs.example.com/{name}/{t}"}
                for t in range(3 + (s % 2))
            ]
            self.config["services"][name] = topics
            self.state[name] = []
            for t in topics:
                n += 1
                st = {"topic": t["topic"], "url": t["url"], "chunks": 12, "synced": TODAY}
                if n % 23 == 0:
                    st.update({"synced": OLD, "last_error": ERR[:200], "consecutive_failures": 4})
                self.state[name].append(st)

    def load_config(self):
        return self.config

    def load_state(self):
        return self.state


def test_default_listing_stays_under_10kb_at_forge_scale(monkeypatch):
    ds = ForgeSizedDocSync()
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)

    r = server.doc_cache_list_services()

    size = len(json.dumps(r))
    assert size < 10 * 1024, f"summary listing is {size} bytes"
    assert r["count"] == 104
    assert r["health"]["failing"] == 15
    assert len(r["failing"]) == 15
    assert all(len(f["last_error"]) <= 101 for f in r["failing"])


def test_failing_topic_is_visible_and_last_success_is_kept(monkeypatch):
    """The 2026-10-19 morning: 15 errors in the log, 0 failing in the listing."""

    class DS:
        def load_config(self):
            return {"services": {"woodpecker": [{"topic": "api", "url": "https://x/api"}]}}

        def load_state(self):
            return {
                "woodpecker": [
                    {
                        "topic": "api",
                        "url": "https://x/api",
                        "chunks": 7,
                        "synced": "2026-09-01",
                        "last_attempt": TODAY,
                        "last_error": "404 Not Found",
                        "consecutive_failures": 3,
                    }
                ]
            }

    monkeypatch.setattr(server, "load_doc_sync", lambda: DS())

    summary = server.doc_cache_list_services()
    assert summary["health"]["failing"] == 1
    assert summary["services"]["woodpecker"]["status"] == "failing"
    assert summary["failing"] == [
        {"topic": "woodpecker/api", "last_error": "404 Not Found", "consecutive_failures": 3}
    ]

    row = server.doc_cache_list_services(service="woodpecker")["services"][0]["topics"][0]
    assert row["status"] == "failing"
    assert row["last_success"] == "2026-09-01"
    assert row["last_error"] == "404 Not Found"


def test_expired_provisional_and_never_synced_are_their_own_states(monkeypatch):
    class DS:
        def load_config(self):
            return {
                "services": {
                    "v": [
                        {"topic": "old", "url": "https://u.example/a", "trust": "provisional"},
                        {"topic": "new", "url": "https://u.example/b"},
                    ]
                }
            }

        def load_state(self):
            return {"v": [{"topic": "old", "url": "https://u.example/a", "expired": True}]}

    monkeypatch.setattr(server, "load_doc_sync", lambda: DS())
    r = server.doc_cache_list_services()
    assert r["health"]["expired_provisional"] == 1
    assert r["health"]["never_synced"] == 1
    assert r["services"]["v"]["status"] == "expired_provisional"


def test_unknown_service_filter_is_refused(monkeypatch):
    monkeypatch.setattr(server, "load_doc_sync", lambda: ForgeSizedDocSync())
    r = server.doc_cache_list_services(service="nope")
    assert r["outcome"] == "refused"
