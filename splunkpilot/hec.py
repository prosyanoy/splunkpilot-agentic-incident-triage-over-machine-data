"""Parsing of Splunk HTTP Event Collector (HEC) payloads."""
from __future__ import annotations

import json
import time
from typing import Any

from .models import Event


class HECError(Exception):
    """Carries the HEC response code/text and the HTTP status to return."""

    def __init__(self, status: int, code: int, text: str) -> None:
        super().__init__(text)
        self.status, self.code, self.text = status, code, text


def event_from_hec(obj: Any, now: float | None = None) -> Event:
    if not isinstance(obj, dict):
        raise HECError(400, 6, "Invalid data format")
    if "event" not in obj:
        raise HECError(400, 12, "Event field is required")
    raw = obj["event"]
    if raw in ("", None, {}):
        raise HECError(400, 13, "Event field cannot be blank")
    try:
        ts = float(obj["time"]) if obj.get("time") is not None else (now or time.time())
    except (TypeError, ValueError):
        raise HECError(400, 6, "Invalid data format") from None
    indexed = obj.get("fields") or {}
    if not isinstance(indexed, dict):
        raise HECError(400, 15, "Fields must be a JSON object")
    fields: dict[str, Any] = dict(indexed)
    source = str(obj.get("source", ""))
    if isinstance(raw, dict):
        body = dict(raw)
        message = body.pop("message", None) or body.pop("msg", None)
        level = body.pop("level", None) or "INFO"
        service = body.pop("service", None) or fields.pop("service", None) or source
        if message is None:
            message = json.dumps(raw, sort_keys=True, default=str)
            body = {}
        fields.update(body)
    else:
        message, level = str(raw), "INFO"
        service = fields.pop("service", None) or source
    return Event(
        ts=ts, message=str(message), index=str(obj.get("index", "main")),
        host=str(obj.get("host", "unknown")), source=source,
        sourcetype=str(obj.get("sourcetype", "app")), service=str(service),
        level=str(level).upper(), fields=fields,
    )


def parse_hec_objects(objs: list[Any]) -> list[Event]:
    now = time.time()
    return [event_from_hec(o, now) for o in objs]


def parse_hec_body(body: bytes) -> list[Event]:
    """Parse concatenated JSON objects (the HEC wire format)."""
    text = body.decode("utf-8", errors="replace")
    dec = json.JSONDecoder()
    pos, objs = 0, []
    while True:
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, pos = dec.raw_decode(text, pos)
        except json.JSONDecodeError:
            raise HECError(400, 6, "Invalid data format") from None
        objs.append(obj)
    if not objs:
        raise HECError(400, 5, "No data")
    return parse_hec_objects(objs)
