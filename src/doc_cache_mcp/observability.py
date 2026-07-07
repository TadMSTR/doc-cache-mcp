"""Observability — structlog (always on, JSON) + optional OTEL/InfluxDB/NATS.

Each optional backend is gated on its env var; a missing var disables that backend with no
import error. Mirrors the forge MCP convention (see dockhand-mcp / vikunja-mcp).
"""

from __future__ import annotations

import os
from typing import Any

import structlog


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
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "doc-cache-mcp"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("doc-cache-mcp")
        structlog.get_logger().info("otel_enabled", endpoint=endpoint)
    except Exception:
        structlog.get_logger().warning("otel_init_failed", exc_info=True)


_influx = None


def _get_influx():
    global _influx
    if _influx is not None:
        return _influx
    url = os.environ.get("INFLUXDB_URL", "")
    if not url:
        return None
    try:
        from influxdb_client_3 import InfluxDBClient3

        _influx = InfluxDBClient3(
            host=url,
            token=os.environ.get("INFLUXDB_TOKEN", ""),
            database=os.environ.get("INFLUXDB_BUCKET", "doc-cache-mcp"),
        )
    except Exception:
        pass
    return _influx


def emit_metric(measurement: str, tags: dict[str, str], fields: dict[str, Any]) -> None:
    """Best-effort metric emission to InfluxDB. Silent no-op when unconfigured."""
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
    except Exception:
        pass
