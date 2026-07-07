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


async def test_sync_ok(fake_ds):
    r = await server.doc_cache_sync("svc")
    assert r["entries_synced"] == 1
    assert r["chunks"] == 3
    assert r["indexed"]["indexed"] is True
    assert "duration_s" in r
    assert fake_ds.calls == [("svc", False, True)]


async def test_sync_dry_run_does_not_index(fake_ds):
    r = await server.doc_cache_sync("svc", dry_run=True)
    assert r["dry_run"] is True
    assert fake_ds.calls == [("svc", True, True)]


async def test_sync_unknown_service(fake_ds):
    r = await server.doc_cache_sync("missing")
    assert "error" in r
    assert "Unknown service" in r["error"]


async def test_sync_rejects_bad_name(fake_ds):
    r = await server.doc_cache_sync("bad name!")
    assert "error" in r
    # A bad name is rejected before the doc-sync layer is ever touched.
    assert fake_ds.calls == []


async def test_sync_without_ctx_does_not_crash(fake_ds):
    # No Context (e.g. a direct call, or a client that never opened a session) — the
    # heartbeat must silently no-op rather than raise.
    r = await server.doc_cache_sync("svc", ctx=None)
    assert r["entries_synced"] == 1


async def test_sync_reports_progress_while_running(fake_ds, monkeypatch):
    # Make the "sync" step slow enough for at least one heartbeat tick to fire, using a
    # near-zero interval so the test stays fast.
    monkeypatch.setattr(server, "_SYNC_HEARTBEAT_INTERVAL_S", 0.01)

    original_sync_service = fake_ds.sync_service

    def slow_sync_service(service, *, force=False, dry_run=False, index=True):
        import time as _time

        _time.sleep(0.05)
        return original_sync_service(service, force=force, dry_run=dry_run, index=index)

    monkeypatch.setattr(fake_ds, "sync_service", slow_sync_service)

    calls = []

    class FakeCtx:
        async def report_progress(self, progress, total, message):
            calls.append((progress, total, message))

    r = await server.doc_cache_sync("svc", ctx=FakeCtx())
    assert r["entries_synced"] == 1
    assert len(calls) >= 1
    assert calls[0][1] is None  # total is unknown (open-ended elapsed time)
    assert "svc" in calls[0][2]


async def test_sync_heartbeat_stops_after_completion(fake_ds, monkeypatch):
    monkeypatch.setattr(server, "_SYNC_HEARTBEAT_INTERVAL_S", 0.01)
    calls = []

    class FakeCtx:
        async def report_progress(self, progress, total, message):
            calls.append(progress)

    await server.doc_cache_sync("svc", ctx=FakeCtx())
    count_at_return = len(calls)
    # Give the event loop a moment — if the heartbeat task weren't cancelled it would
    # keep appending.
    import asyncio

    await asyncio.sleep(0.05)
    assert len(calls) == count_at_return


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
