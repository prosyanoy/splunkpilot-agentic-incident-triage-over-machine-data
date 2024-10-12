import pytest

from splunkpilot.backend import LocalBackend
from splunkpilot.models import Event
from splunkpilot.store import EventStore


@pytest.fixture
def store() -> EventStore:
    s = EventStore()
    t = 1_000_000.0
    s.insert_many([
        Event(t + 0, "GET / 200 10ms", host="web-01", service="web", level="INFO", fields={"latency_ms": 10, "status": 200}),
        Event(t + 30, "GET /cart 500 900ms", host="web-01", service="web", level="ERROR", fields={"latency_ms": 900, "status": 500}),
        Event(t + 70, "GET /cart 500 700ms", host="web-02", service="web", level="ERROR", fields={"latency_ms": 700, "status": 500}),
        Event(t + 90, "charge ok", host="pay-01", service="payments", level="INFO", fields={"latency_ms": 50}),
        Event(t + 130, "slow request 400ms", host="pay-01", service="payments", level="WARN", fields={"latency_ms": 400}),
        Event(t + 5, "cpu_pct=40.0", host="web-01", service="web", sourcetype="metric", fields={"metric": "cpu_pct", "value": 40.0}),
    ])
    return s


@pytest.fixture(scope="session")
def incidents():
    from splunkpilot.generator import generate, scenario_names

    return {sc: generate(sc, 0) for sc in scenario_names()}


@pytest.fixture
def loaded(incidents):
    def make(sc: str):
        b = LocalBackend()
        b.ingest(incidents[sc].events)
        return b, incidents[sc]
    return make
