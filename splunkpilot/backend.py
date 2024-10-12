"""Backends the agent/CLI use to talk to the event store: in-process or over HTTP."""
from __future__ import annotations

from typing import Any, Iterable, Protocol

import httpx

from .hec import parse_hec_objects
from .spl import SPLError, SearchResult, execute
from .store import EventStore


class Backend(Protocol):
    def search(self, query: str, earliest: Any = None, latest: Any = None,
               index: str | None = None, limit: int = 1000) -> dict[str, Any]: ...
    def get_event(self, event_id: int) -> dict[str, Any] | None: ...
    def ingest(self, events: list[dict[str, Any]]) -> int: ...


class LocalBackend:
    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    def search(self, query, earliest=None, latest=None, index=None, limit=1000):
        return execute(self.store, query, earliest, latest, index, limit).to_json()

    def get_event(self, event_id):
        row = self.store.get(event_id)
        return SearchResult(list(row), [row]).to_json()["results"][0] if row else None

    def ingest(self, events):
        return self.store.insert_many(parse_hec_objects(events))


class HttpBackend:
    """Talks to a running SplunkPilot API (HEC ingest + search)."""

    def __init__(self, base_url: str, hec_token: str, client: httpx.Client | None = None,
                 batch_size: int = 1000) -> None:
        self.client = client or httpx.Client(base_url=base_url, timeout=60)
        self.token, self.batch_size = hec_token, batch_size

    def search(self, query, earliest=None, latest=None, index=None, limit=1000):
        r = self.client.post("/api/search", json={"query": query, "earliest": earliest, "latest": latest,
                                                  "index": index, "limit": limit})
        if r.status_code == 400:
            raise SPLError(r.json().get("error", "bad search"))
        r.raise_for_status()
        return r.json()

    def get_event(self, event_id):
        r = self.client.get(f"/api/events/{event_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def ingest(self, events: Iterable[dict[str, Any]]) -> int:
        import json

        events, n = list(events), 0
        for i in range(0, len(events), self.batch_size):
            batch = events[i:i + self.batch_size]
            r = self.client.post("/services/collector/event",
                                 content="\n".join(json.dumps(e) for e in batch),
                                 headers={"Authorization": f"Splunk {self.token}"})
            r.raise_for_status()
            n += len(batch)
        return n
