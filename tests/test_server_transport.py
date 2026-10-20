"""main() runs the HTTP transport stateless (p2 audit F-06, CVE-2026-59951)."""

from __future__ import annotations

from doc_cache_mcp import config, server


def _run_main(monkeypatch, transport):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(config, "_settings", config.Settings(transport=transport))
    server.main()
    return calls


def test_http_transport_is_stateless(monkeypatch):
    calls = _run_main(monkeypatch, "http")
    assert calls == [
        {"transport": "http", "host": "127.0.0.1", "port": 8503, "stateless_http": True}
    ]


def test_stdio_takes_no_http_options(monkeypatch):
    assert _run_main(monkeypatch, "stdio") == [{"transport": "stdio"}]
