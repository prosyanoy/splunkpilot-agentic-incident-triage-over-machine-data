import json

import pytest
from fastapi.testclient import TestClient

from splunkpilot.api import create_app
from splunkpilot.config import Settings
from splunkpilot.store import EventStore

AUTH = {"Authorization": "Splunk test-token"}


@pytest.fixture
def client():
    return TestClient(create_app(EventStore(), Settings(hec_token="test-token")))


def post(client, payload, headers=AUTH, path="/services/collector/event"):
    body = payload if isinstance(payload, (str, bytes)) else "\n".join(json.dumps(p) for p in payload)
    return client.post(path, content=body, headers=headers)


def test_hec_ingest_and_search_roundtrip(client):
    r = post(client, [
        {"time": 1700000000, "host": "h1", "source": "api", "sourcetype": "app", "index": "demo",
         "event": {"level": "error", "message": "boom", "service": "api", "code": 7}},
        {"time": 1700000001.5, "host": "h2", "index": "demo", "event": "plain text event", "fields": {"service": "web"}},
    ])
    assert r.status_code == 200 and r.json() == {"text": "Success", "code": 0}
    res = client.post("/api/search", json={"query": "level=ERROR code=7", "index": "demo"}).json()
    assert res["count"] == 1 and res["results"][0]["message"] == "boom" and res["results"][0]["host"] == "h1"
    assert client.post("/api/search", json={"query": "service=web", "index": "demo"}).json()["count"] == 1
    assert client.post("/api/search", json={"query": "service=web", "index": "other"}).json()["count"] == 0


def test_legacy_collector_path_and_health(client):
    assert post(client, [{"event": "x", "time": 1}], path="/services/collector").status_code == 200
    assert client.get("/services/collector/health").status_code == 200
    assert client.get("/health").json()["events"] == 1


def test_hec_auth(client):
    assert post(client, [{"event": "x"}], headers={}).status_code == 401
    r = post(client, [{"event": "x"}], headers={"Authorization": "Splunk wrong"})
    assert r.status_code == 403 and r.json()["code"] == 4


@pytest.mark.parametrize("body,code", [("", 5), ("{not json", 6), ('{"time": 1}', 12), ('{"event": ""}', 13),
                                       ('{"event": "x", "time": "abc"}', 6), ('{"event": "x", "fields": 5}', 15), ("[1]", 6)])
def test_hec_invalid_payloads(client, body, code):
    r = post(client, body)
    assert r.status_code == 400 and r.json()["code"] == code


def test_search_errors_and_event_lookup(client):
    post(client, [{"event": {"message": "m", "level": "INFO"}, "time": 5}])
    assert client.post("/api/search", json={"query": "| nope"}).status_code == 400
    ev = client.post("/api/search", json={"query": "m"}).json()["results"][0]
    assert client.get(f"/api/events/{ev['_id']}").json()["message"] == "m"
    assert client.get("/api/events/9999").status_code == 404


def test_splunk_style_export(client):
    post(client, [{"event": {"message": "alpha", "level": "ERROR"}, "time": 10}, {"event": "beta", "time": 11}])
    r = client.post("/services/search/jobs/export", content="search=level%3DERROR&output_mode=json")
    lines = [json.loads(x) for x in r.text.splitlines()]
    assert len(lines) == 1 and lines[0]["result"]["message"] == "alpha"
    assert client.post("/services/search/jobs/export", content="output_mode=json").status_code == 400
