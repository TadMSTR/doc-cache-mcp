"""doc_cache_sync + doc_cache_list_services — delegation to the shared doc-sync logic,
with that logic faked so the tests need no network or real cache."""

from __future__ import annotations

import pytest

import doc_cache_mcp.server as server


class FakeDocSync:
    def __init__(self):
        self.calls = []

    def load_config(self):
        return {
            "services": {"svc": [{"topic": "overview", "url": "https://x/README.md"}]}
        }

    def load_state(self):
        return {
            "svc": [
                {
                    "topic": "overview",
                    "url": "https://x/README.md",
                    "chunks": 3,
                    "synced": "2026-07-07",
                }
            ]
        }

    def sync_service(self, service, *, force=False, dry_run=False, index=True):
        self.calls.append((service, dry_run, index))
        if service not in self.load_config()["services"]:
            raise ValueError(f"Unknown service: {service}")
        if dry_run:
            return {
                "service": service,
                "entries_synced": 0,
                "chunks": 0,
                "errors": 0,
                "indexed": None,
                "dry_run": True,
                "results": [],
            }
        return {
            "service": service,
            "entries_synced": 1,
            "chunks": 3,
            "errors": 0,
            "indexed": {"indexed": True, "returncode": 0, "timed_out": False},
            "dry_run": False,
            "results": [
                {"topic": "overview", "url": "https://x/README.md", "chunks": 3}
            ],
        }


@pytest.fixture
def fake_ds(monkeypatch):
    ds = FakeDocSync()
    monkeypatch.setattr(server, "load_doc_sync", lambda: ds)
    return ds


def test_sync_ok(fake_ds):
    r = server.doc_cache_sync("svc")
    assert r["entries_synced"] == 1
    assert r["chunks"] == 3
    assert r["indexed"]["indexed"] is True
    assert "duration_s" in r
    assert fake_ds.calls == [("svc", False, True)]


def test_sync_dry_run_does_not_index(fake_ds):
    r = server.doc_cache_sync("svc", dry_run=True)
    assert r["dry_run"] is True
    assert fake_ds.calls == [("svc", True, True)]


def test_sync_unknown_service(fake_ds):
    r = server.doc_cache_sync("missing")
    assert "error" in r
    assert "Unknown service" in r["error"]


def test_sync_rejects_bad_name(fake_ds):
    r = server.doc_cache_sync("bad name!")
    assert "error" in r
    # A bad name is rejected before the doc-sync layer is ever touched.
    assert fake_ds.calls == []


def test_list_services(fake_ds):
    r = server.doc_cache_list_services()
    assert r["count"] == 1
    svc = r["services"][0]
    assert svc["service"] == "svc"
    assert svc["topic_count"] == 1
    assert svc["total_chunks"] == 3
    assert svc["topics"][0]["last_synced"] == "2026-07-07"


def test_list_services_unsynced_topic_shows_zero(monkeypatch):
    class DS(FakeDocSync):
        def load_state(self):
            return {}  # nothing synced yet

    monkeypatch.setattr(server, "load_doc_sync", lambda: DS())
    r = server.doc_cache_list_services()
    svc = r["services"][0]
    assert svc["total_chunks"] == 0
    assert svc["topics"][0]["last_synced"] is None
