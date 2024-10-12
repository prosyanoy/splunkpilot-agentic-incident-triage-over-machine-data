"""Core event model shared by the generator, HEC ingest, store and search."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

CORE_FIELDS = ("index", "host", "source", "sourcetype", "service", "level", "message")


@dataclass
class Event:
    ts: float
    message: str
    index: str = "main"
    host: str = "unknown"
    source: str = ""
    sourcetype: str = "app"
    service: str = ""
    level: str = "INFO"
    fields: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = dict(self.fields)
        row.update(
            _id=self.id,
            _time=self.ts,
            index=self.index,
            host=self.host,
            source=self.source,
            sourcetype=self.sourcetype,
            service=self.service,
            level=self.level,
            message=self.message,
        )
        return row


def iso(ts: float) -> str:
    """Format an epoch timestamp as ISO-8601 UTC (ms precision only when needed)."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    if ts == int(ts):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
