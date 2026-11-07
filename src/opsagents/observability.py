"""Structured logs and optional OpenTelemetry spans."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

log = logging.getLogger("opsagents")

try:  # pragma: no cover - exercised only with the otel extra installed
    from opentelemetry import trace as _otel

    _tracer: Any = _otel.get_tracer("opsagents")
except ImportError:  # pragma: no cover
    _tracer = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data.update(getattr(record, "fields", {}))
        return json.dumps(data, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("opsagents")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


def log_event(event: str, **fields: Any) -> None:
    """One line per decision. Ticket text is never logged: it is customer data."""
    log.info(event, extra={"fields": {"event": event, **fields}})


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    started = time.perf_counter()
    if _tracer is None:  # pragma: no cover
        yield
        return
    with _tracer.start_as_current_span(name, attributes=attributes or {}):
        yield
    log.debug("span", extra={"fields": {"span": name, "ms": round((time.perf_counter() - started) * 1000, 2)}})
