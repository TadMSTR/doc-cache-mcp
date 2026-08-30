"""
Observability-layer tests — the telemetry code that was failing silently
(vikunja#575 item 3, #580). Ported from the dockhand-mcp reference implementation
(TadMSTR/dockhand-mcp#6, `42f73cf`), minus the parts whose defect is absent here.

What is asserted:

1. A *configured but failing* InfluxDB warns exactly once at init and is then not
   retried, while a *missing* ``INFLUXDB_URL`` stays silent — "never tried" and
   "tried and failed" must not collapse into one state.
2. The **write** path warns too, and this is the half that matters.
   ``InfluxDBClient3`` constructs lazily and never contacts the host, so against
   an unreachable URL ``_get_influx()`` *succeeds* and the init sentinel never
   fires; the failure only ever surfaces on ``write()``. Verified live on
   2026-08-30 against ``http://127.0.0.1:9/nope`` — client built, every write
   raised ``NewConnectionError``, and the old ``except Exception: pass`` logged
   nothing at all. Without this test the InfluxDB fix is a no-op that looks
   correct (#580).
3. The warning carries the exception *class*, never the URL or the token.

What is deliberately **not** here, and why — read before porting more from
dockhand-mcp:

- **No third-party logger demotion.** dockhand-mcp and backrest-mcp route stdlib
  logging through a root handler, so ``httpx``/``mcp``/``nats`` inherited
  ``LOG_LEVEL`` and drowned the app's own lines. This server does not: its
  ``configure_logging()`` is ``structlog.configure()`` with a
  ``PrintLoggerFactory``, no root ``setLevel``, no stdlib handler. Its logs carry
  zero ``"logger":`` keys, confirming nothing routes through stdlib. A demotion
  here would be a change with no defect behind it, so
  ``test_configure_logging_does_not_touch_stdlib_logging`` pins the premise
  instead.
- **No NATS tests.** There is no NATS path in this server.

``influxdb_client_3`` is deliberately faked via ``sys.modules`` rather than
imported: CI installs only ``.[dev]``, so a test that needed the real package
would be skipped exactly where it matters most.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from doc_cache_mcp import observability

# A stand-in for a real credential-bearing config. Every failure-path test asserts
# these strings reach no log payload — an assertion that fails loudly if someone
# later "improves" the warning by adding str(exc) or the config back in.
SECRET_URL = "http://influx.internal:8182/?u=admin&p=sup3rs3cr3t-pw"
SECRET_TOKEN = "influx-token-do-not-log-me"


class _RecordingLogger:
    """Minimal stand-in for the module's structlog logger.

    Captures ``(event, kwargs)`` so a test can assert both *that* a warning fired
    and *what* was in it. Asserting the payload is the point: "no secret in the
    log" passes trivially when nothing is logged at all, so every leak test also
    asserts the event fired and carried an ``error_class``.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict]] = []

    def warning(self, event, **kw):
        self.warnings.append((event, kw))

    def info(self, event, **kw):  # pragma: no cover - not asserted on
        pass

    def payload_text(self) -> str:
        return repr(self.warnings)


@pytest.fixture
def rec_log(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(observability, "log", recorder)
    return recorder


@pytest.fixture(autouse=True)
def reset_observability_globals(monkeypatch):
    """Reset the backend globals around every test.

    They are module-level negative caches by design — without this, the first test
    to trip a sentinel would disable the backend for the whole session and every
    later assertion would pass for the wrong reason.
    """
    for name, value in (
        ("_influx", None),
        ("_influx_failed", False),
        ("_influx_write_failed_logged", False),
        ("_tracer", None),
    ):
        monkeypatch.setattr(observability, name, value)
    yield


def _fake_influx_module(*, raise_on_init=None, client=None):
    """Build a fake ``influxdb_client_3`` module for ``sys.modules``."""
    mod = types.ModuleType("influxdb_client_3")

    def _ctor(**kwargs):
        if raise_on_init is not None:
            raise raise_on_init
        return client

    class _Point:
        def __init__(self, measurement):
            self.measurement = measurement

        def tag(self, k, v):
            return self

        def field(self, k, v):
            return self

    mod.InfluxDBClient3 = _ctor
    mod.Point = _Point
    return mod


# ---------------------------------------------------------------------------
# configure_logging — the premise for *not* porting the demotion
# ---------------------------------------------------------------------------


def test_configure_logging_does_not_touch_stdlib_logging():
    """Pins why this server has no third-party logger demotion.

    dockhand-mcp/backrest-mcp needed one because they set the *root* logger to
    LOG_LEVEL and attach a stdlib handler, so httpx/mcp/nats inherited it. This
    server's ``configure_logging()`` is ``structlog.configure()`` only. If that
    ever changes to stdlib routing, the demotion becomes necessary and this test
    is the thing that says so.
    """
    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    before_third_party = {
        name: logging.getLogger(name).level for name in ("httpx", "httpcore", "mcp")
    }

    observability.configure_logging()

    assert list(root.handlers) == before_handlers
    assert root.level == before_level
    for name, level in before_third_party.items():
        assert logging.getLogger(name).level == level, name


# ---------------------------------------------------------------------------
# InfluxDB init sentinel
# ---------------------------------------------------------------------------


def test_get_influx_unset_env_is_silently_disabled(monkeypatch, rec_log):
    """A *missing* env var is the intended disabled path and must not warn."""
    monkeypatch.delenv("INFLUXDB_URL", raising=False)

    assert observability._get_influx() is None
    assert rec_log.warnings == []
    assert observability._influx_failed is False, "never-tried must stay distinct from failed"


def test_get_influx_failure_warns_once_and_is_not_retried(monkeypatch, rec_log):
    calls = []

    def _ctor(**kwargs):
        calls.append(kwargs)
        raise ConnectionRefusedError("connection refused")

    mod = _fake_influx_module()
    mod.InfluxDBClient3 = _ctor
    monkeypatch.setitem(sys.modules, "influxdb_client_3", mod)
    monkeypatch.setenv("INFLUXDB_URL", "http://127.0.0.1:9/unreachable")
    monkeypatch.setenv("INFLUXDB_TOKEN", SECRET_TOKEN)

    assert observability._get_influx() is None
    assert observability._get_influx() is None
    assert observability._get_influx() is None

    # The negative cache is the fix for the retry storm: one attempt, not three.
    assert len(calls) == 1
    assert observability._influx_failed is True
    assert len(rec_log.warnings) == 1
    event, kw = rec_log.warnings[0]
    assert event == "influx_init_failed"
    assert kw["error_class"] == "ConnectionRefusedError"


def test_get_influx_failure_warning_carries_no_credentials(monkeypatch, rec_log):
    """The one place this build could introduce a leak."""
    mod = _fake_influx_module(
        raise_on_init=ValueError(f"bad host {SECRET_URL} token {SECRET_TOKEN}")
    )
    monkeypatch.setitem(sys.modules, "influxdb_client_3", mod)
    monkeypatch.setenv("INFLUXDB_URL", SECRET_URL)
    monkeypatch.setenv("INFLUXDB_TOKEN", SECRET_TOKEN)

    observability._get_influx()

    # Assert the warning fired *and* is clean — "no secret in the log" would pass
    # trivially against a code path that logs nothing at all.
    assert [e for e, _ in rec_log.warnings] == ["influx_init_failed"]
    assert rec_log.warnings[0][1]["error_class"] == "ValueError"
    text = rec_log.payload_text()
    assert SECRET_URL not in text
    assert SECRET_TOKEN not in text
    assert "sup3rs3cr3t-pw" not in text


def test_get_influx_success_is_cached(monkeypatch, rec_log):
    sentinel = object()
    calls = []

    def _ctor(**kwargs):
        calls.append(kwargs)
        return sentinel

    mod = _fake_influx_module()
    mod.InfluxDBClient3 = _ctor
    monkeypatch.setitem(sys.modules, "influxdb_client_3", mod)
    monkeypatch.setenv("INFLUXDB_URL", "http://127.0.0.1:8182")
    monkeypatch.setenv("INFLUXDB_BUCKET", "forge")

    assert observability._get_influx() is sentinel
    assert observability._get_influx() is sentinel
    assert len(calls) == 1
    assert calls[0]["database"] == "forge"
    assert rec_log.warnings == []


# ---------------------------------------------------------------------------
# The write path — the half that must not be dropped (#580)
# ---------------------------------------------------------------------------


def test_a_lazily_built_client_means_the_init_sentinel_never_fires(monkeypatch, rec_log):
    """The mechanism behind #580, stated as a test.

    A client that builds without contacting the host is indistinguishable at init
    from a healthy one. So an unreachable InfluxDB produces **no** init warning —
    which is precisely why ``emit_metric`` has to carry its own.
    """

    class _LazyClient:
        """Builds fine, like the real InfluxDBClient3; fails only on write."""

        def write(self, record=None):
            raise ConnectionRefusedError("[Errno 111] Connection refused")

    monkeypatch.setitem(sys.modules, "influxdb_client_3", _fake_influx_module(client=_LazyClient()))
    monkeypatch.setenv("INFLUXDB_URL", "http://127.0.0.1:9/nope")

    assert observability._get_influx() is not None
    assert rec_log.warnings == [], "init cannot detect this failure — that is the bug"

    observability.emit_metric("doc_cache_tool", {"tool": "sync"}, {"chunks": 1})

    assert [e for e, _ in rec_log.warnings] == ["influx_write_failed"]


def test_emit_metric_write_failure_warns_once(monkeypatch, rec_log):
    class _Client:
        def __init__(self):
            self.writes = 0

        def write(self, record=None):
            self.writes += 1
            raise RuntimeError(f"write rejected for {SECRET_TOKEN}")

    client = _Client()
    monkeypatch.setitem(sys.modules, "influxdb_client_3", _fake_influx_module(client=client))
    monkeypatch.setattr(observability, "_influx", client)

    for _ in range(3):
        observability.emit_metric("doc_cache_tool", {"tool": "sync"}, {"chunks": 1})

    # Unlike an init failure, a write failure is often transient — keep writing,
    # but log only the first so a broken collector cannot flood the log.
    assert client.writes == 3
    assert [e for e, _ in rec_log.warnings] == ["influx_write_failed"]
    kw = rec_log.warnings[0][1]
    assert kw["error_class"] == "RuntimeError"
    assert kw["measurement"] == "doc_cache_tool"
    assert SECRET_TOKEN not in rec_log.payload_text()


def test_emit_metric_is_a_noop_with_no_backend(monkeypatch, rec_log):
    monkeypatch.delenv("INFLUXDB_URL", raising=False)

    observability.emit_metric("doc_cache_tool", {"tool": "sync"}, {"chunks": 1})

    assert rec_log.warnings == []


def test_emit_metric_writes_a_point(monkeypatch, rec_log):
    written = []

    class _Client:
        def write(self, record=None):
            written.append(record)

    monkeypatch.setitem(sys.modules, "influxdb_client_3", _fake_influx_module())
    monkeypatch.setattr(observability, "_influx", _Client())

    observability.emit_metric("doc_cache_tool", {"tool": "sync"}, {"chunks": 3})

    assert len(written) == 1
    assert written[0].measurement == "doc_cache_tool"
    assert rec_log.warnings == []


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------


def test_init_tracing_without_endpoint_is_a_silent_noop(monkeypatch, rec_log):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    observability.init_tracing()

    assert observability._tracer is None
    assert rec_log.warnings == []


def test_only_the_otel_site_is_exempt_from_the_no_exc_info_rule():
    """Pins the exemption set decided at the dockhand-mcp audit (2026-08-29, LOW-1).

    ``exc_info=True`` renders the exception text into the log, so it is kept only
    where the config behind the failure carries no credential — OTel. A new
    ``exc_info=True`` on the InfluxDB sites, whose errors can echo the host or the
    token, changes this count.
    """
    import pathlib

    src = pathlib.Path(observability.__file__).read_text()
    exempt = [ln.strip() for ln in src.splitlines() if "exc_info=True" in ln]
    assert len(exempt) == 1, exempt
    assert all("otel_" in ln for ln in exempt), exempt
