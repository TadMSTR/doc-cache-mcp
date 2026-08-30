"""Observability — structlog (always on, JSON) + optional OTEL/InfluxDB/NATS.

Each optional backend is gated on its env var; a missing var disables that backend with no
import error. Mirrors the forge MCP convention (see dockhand-mcp / vikunja-mcp).
"""

from __future__ import annotations

import os
from typing import Any

import structlog

log = structlog.get_logger(__name__)


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


_tracer = None


def init_tracing() -> None:
    """Enable OTLP tracing if OTEL_EXPORTER_OTLP_ENDPOINT is set. No-op otherwise."""
    global _tracer
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "doc-cache-mcp"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("doc-cache-mcp")
        log.info("otel_enabled", endpoint=endpoint)
    except Exception:
        # EXEMPT from the no-exc_info rule the credential-bearing backends follow
        # (dockhand-mcp audit 2026-08-29, LOW-1). OTEL_EXPORTER_OTLP_ENDPOINT is a
        # bare URL with no credential in it, and the try block above spans five
        # separate imports plus a gRPC exporter build — `error_class` alone would
        # not say *which* failed, which is the whole diagnostic question when the
        # [telemetry] extra is missing.
        log.warning("otel_init_failed", exc_info=True)


# A *configured but failing* backend must be visible and must not be retried on
# every tool call. `except Exception: pass` left the client global at None, which
# meant (a) 35 days of logs with zero lines mentioning influx, and (b) every
# emit_metric() re-entering the connect path (vikunja#575 item 3, #580).
#
# The sentinel is a distinct flag rather than an overloaded None so "never tried"
# and "tried and failed" stay distinguishable: a missing env var is the intended
# disabled path and must stay silent, a failed init must warn exactly once.
#
# SECURITY: the warning carries the exception *class*, never str(exc). An InfluxDB
# error can echo the host or the token, and both would land verbatim in a log.
_influx = None
_influx_failed = False
_influx_write_failed_logged = False


def _get_influx():
    global _influx, _influx_failed
    if _influx is not None:
        return _influx
    if _influx_failed:
        return None
    url = os.environ.get("INFLUXDB_URL", "")
    if not url:
        return None  # backend not configured — intended disabled path, stay silent
    try:
        from influxdb_client_3 import InfluxDBClient3

        _influx = InfluxDBClient3(
            host=url,
            token=os.environ.get("INFLUXDB_TOKEN", ""),
            database=os.environ.get("INFLUXDB_BUCKET", "doc-cache-mcp"),
        )
    except Exception as exc:
        _influx_failed = True
        log.warning(
            "influx_init_failed",
            error_class=type(exc).__name__,
            detail=(
                "INFLUXDB_URL is set but the client could not be built; metric "
                "writes are disabled for the lifetime of this process. Check the "
                "URL is reachable and the influxdb3-python extra is installed."
            ),
        )
    return _influx


def emit_metric(measurement: str, tags: dict[str, str], fields: dict[str, Any]) -> None:
    """Best-effort metric emission to InfluxDB. Silent no-op when unconfigured."""
    global _influx_write_failed_logged

    influx = _get_influx()
    if not influx:
        return
    try:
        from influxdb_client_3 import Point

        p = Point(measurement)
        for k, v in tags.items():
            p = p.tag(k, v)
        for k, v in fields.items():
            p = p.field(k, v)
        influx.write(record=p)
    except Exception as exc:
        # THE half that matters. `InfluxDBClient3` constructs lazily and never
        # contacts the host, so `_get_influx()` *succeeds* against an unreachable
        # URL and the init sentinel above never fires — verified live 2026-08-30
        # against http://127.0.0.1:9/nope, where the client built fine and every
        # write() raised NewConnectionError into this `except`. Without this
        # warning the sentinel is dead code on the only path that runs, and a
        # misconfigured InfluxDB stays exactly as silent as it was for 35 days
        # (vikunja#580).
        #
        # Warn once per process, then stay quiet. Unlike an init failure a write
        # failure is often transient (collector restart), so it does not disable
        # the backend — but it must not be silent either.
        if not _influx_write_failed_logged:
            _influx_write_failed_logged = True
            log.warning(
                "influx_write_failed",
                measurement=measurement,
                error_class=type(exc).__name__,
                detail="first metric write failure; further ones are not logged",
            )
