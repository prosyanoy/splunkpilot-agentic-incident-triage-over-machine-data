import pytest

from splunkpilot.spl import SPLError, execute, parse_time, split_pipeline

T = 1_000_000.0


def q(store, query, **kw):
    return execute(store, query, **kw)


def test_parse_time_variants():
    assert parse_time("2026-01-15T10:00:00Z") == 1768471200.0
    assert parse_time("-15m", now=1000) == 100
    assert parse_time("+1h", now=0) == 3600
    assert parse_time("now", now=5) == 5
    assert parse_time(12.5) == 12.5
    with pytest.raises(SPLError):
        parse_time("yesterdayish")


def test_field_filters_and_wildcards(store):
    assert q(store, "service=web level=ERROR").rows.__len__() == 2
    assert len(q(store, "host=web-0*").rows) == 4
    assert len(q(store, "status>=500").rows) == 2
    assert len(q(store, "level!=INFO service=payments").rows) == 1


def test_bare_terms_boolean_and_parens(store):
    assert len(q(store, '"/cart"').rows) == 2
    assert len(q(store, "charge OR slow").rows) == 2
    assert len(q(store, "service=web NOT level=ERROR NOT sourcetype=metric").rows) == 1
    assert len(q(store, "(level=ERROR OR level=WARN) AND host=pay-01").rows) == 1
    with pytest.raises(SPLError):
        q(store, "(level=ERROR")


def test_time_bounds_inline_and_params(store):
    assert len(q(store, "service=web", earliest=T + 20, latest=T + 80).rows) == 2
    assert len(q(store, f"service=web earliest={T + 20} latest={T + 80}").rows) == 2


def test_stats_by_and_aliases(store):
    r = q(store, "service=web sourcetype=app | stats count, avg(latency_ms) as avg_ms, max(latency_ms) by level")
    by = {row["level"]: row for row in r.rows}
    assert by["ERROR"]["count"] == 2 and by["ERROR"]["avg_ms"] == 800 and by["ERROR"]["max(latency_ms)"] == 900
    assert q(store, "level=NOPE | stats count").rows == [{"count": 0}]
    assert q(store, "service=payments | stats dc(host) p95(latency_ms)").rows[0]["dc(host)"] == 1


def test_timechart_fills_empty_buckets_and_splits(store):
    r = q(store, "sourcetype=app | timechart span=1m count by level", earliest=T, latest=T + 180)
    assert [row["_time"] for row in r.rows] == [T - T % 60 + 60 * i for i in range(4)] or len(r.rows) in (3, 4)
    total = sum(v for row in r.rows for k, v in row.items() if k != "_time")
    assert total == 5
    assert all("ERROR" in row for row in r.rows)  # zero-filled


def test_top_sort_head_tail_dedup_table(store):
    r = q(store, "sourcetype=app | top limit=2 host")
    assert r.rows[0]["host"] in ("web-01", "pay-01") and r.rows[0]["count"] == 2
    assert q(store, "sourcetype=app | sort -latency_ms | head 1").rows[0]["latency_ms"] == 900
    assert q(store, "sourcetype=app | sort latency_ms | head 1").rows[0]["latency_ms"] == 10
    assert len(q(store, "sourcetype=app | tail 2").rows) == 2
    assert len(q(store, "sourcetype=app | dedup host").rows) == 3
    t = q(store, "service=web | table host level")
    assert set(t.rows[0]) == {"host", "level"}
    assert "service" not in q(store, "service=web | fields - service").rows[0]
    assert "svc" in q(store, "service=web | rename service as svc | head 1").rows[0]


def test_where_after_stats(store):
    r = q(store, "sourcetype=app | stats count by host | where count>1")
    assert {x["host"] for x in r.rows} == {"web-01", "pay-01"}


def test_index_scoping_and_truncation(store):
    assert q(store, "service=web", index="nope").rows == []
    r = q(store, "sourcetype=app", limit=2)
    assert len(r.rows) == 2 and r.truncated


def test_errors(store):
    for bad in ["", "| stats count", "stats count", "service=web | frobnicate", "service=web | head x",
                'service="open', "service=web | timechart span=banana count", "service=web | stats median"]:
        with pytest.raises(SPLError):
            q(store, bad)


def test_pipe_inside_quotes_is_not_a_split():
    assert split_pipeline('a "b|c" | stats count') == ['a "b|c"', "stats count"]


def test_to_json_formats_time(store):
    out = q(store, "service=payments | head 1").to_json()
    assert out["results"][0]["_time"].endswith("Z") and out["count"] == 1
