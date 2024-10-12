"""Tools exposed to the investigating agent. Each returns a compact JSON-serialisable dict."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Callable

from ..backend import Backend
from ..spl import SPLError, parse_time

MAX_ROWS = 60
FETCH_LIMIT = 20_000

_NORM = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{12,}\b"), "<hex>"),
    (re.compile(r"\d+(?:\.\d+)*"), "#"),
]


def normalize(message: str) -> str:
    """Collapse numbers/IPs/ids so repeated log templates group together."""
    for rx, rep in _NORM:
        message = rx.sub(rep, message)
    return message


def _ts(s: str) -> float:
    v = parse_time(s)
    assert v is not None
    return v


class ToolBox:
    def __init__(self, backend: Backend, index: str | None = None) -> None:
        self.backend, self.index = backend, index
        self._tools: dict[str, Callable[..., dict[str, Any]]] = {
            "search": self.search, "aggregate": self.aggregate, "timechart": self.timechart,
            "top_errors": self.top_errors, "change_correlation": self.change_correlation,
        }

    # ------------------------------------------------------------------ dispatch
    def call(self, name: str, arguments: dict[str, Any] | str | None) -> dict[str, Any]:
        fn = self._tools.get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}; available: {', '.join(self._tools)}"}
        if isinstance(arguments, str):
            import json

            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return {"error": "arguments must be a JSON object"}
        try:
            return fn(**(arguments or {}))
        except (SPLError, TypeError, ValueError, KeyError) as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _run(self, query: str, earliest: str, latest: str, limit: int) -> dict[str, Any]:
        return self.backend.search(query, earliest, latest, self.index, limit)

    # --------------------------------------------------------------------- tools
    def search(self, query: str, earliest: str, latest: str, limit: int = 20, order: str = "asc") -> dict[str, Any]:
        """Run an SPL query and return matching events (with ids you can cite)."""
        limit = max(1, min(int(limit), MAX_ROWS))
        q = query
        if order == "asc" and "|" not in query:
            q = f"{query} | sort _time"
        res = self._run(q, earliest, latest, limit)
        rows = res["results"]
        if rows and "_id" in rows[0] and "message" in rows[0]:
            cols = [c for c in res["fields"] if c not in ("index", "source")]
            rows = [{c: r[c] for c in cols if c in r} for r in rows]
        return {"count": res["count"], "truncated": res["truncated"], "events": rows}

    def aggregate(self, query: str, earliest: str, latest: str) -> dict[str, Any]:
        """Run an SPL query ending in stats/top/timechart/etc. and return the table."""
        res = self._run(query, earliest, latest, MAX_ROWS)
        return {"fields": res["fields"], "rows": res["results"], "truncated": res["truncated"]}

    def timechart(self, search: str, earliest: str, latest: str, span: str = "1m",
                  agg: str = "count", by: str | None = None) -> dict[str, Any]:
        """Bucket events over time, e.g. search='level=ERROR', by='service'."""
        q = f"{search} | timechart span={span} {agg}" + (f" by {by}" if by else "")
        res = self._run(q, earliest, latest, 500)
        return {"fields": res["fields"], "rows": res["results"][:MAX_ROWS], "truncated": len(res["results"]) > MAX_ROWS}

    def top_errors(self, earliest: str, latest: str, service: str | None = None, limit: int = 8) -> dict[str, Any]:
        """Most frequent WARN/ERROR message templates in the window, with services/hosts and sample event ids."""
        q = "(level=ERROR OR level=WARN) NOT sourcetype=metric NOT sourcetype=change"
        if service:
            q += f" service={service}"
        res = self._run(q + " | table _id _time host service level message | sort _time", earliest, latest, FETCH_LIMIT)
        groups: dict[str, dict[str, Any]] = {}
        for r in res["results"]:
            g = groups.setdefault(normalize(r["message"]), {
                "pattern": normalize(r["message"]), "count": 0, "levels": Counter(), "services": Counter(),
                "hosts": Counter(), "first_seen": r["_time"], "last_seen": r["_time"], "sample_ids": []})
            g["count"] += 1
            g["levels"][r["level"]] += 1
            g["services"][r["service"]] += 1
            g["hosts"][r["host"]] += 1
            g["last_seen"] = r["_time"]
            if len(g["sample_ids"]) < 3:
                g["sample_ids"].append(r["_id"])
        ranked = sorted(groups.values(), key=lambda g: (-g["count"], g["pattern"]))[: max(1, min(int(limit), 20))]
        out = [{
            "pattern": g["pattern"], "level": g["levels"].most_common(1)[0][0], "count": g["count"],
            "services": dict(g["services"].most_common(3)), "hosts": dict(g["hosts"].most_common(3)),
            "first_seen": g["first_seen"], "last_seen": g["last_seen"], "sample_ids": g["sample_ids"],
        } for g in ranked]
        return {"total_events": res["count"], "distinct_patterns": len(groups), "groups": out}

    def change_correlation(self, earliest: str, latest: str, window_minutes: int = 10) -> dict[str, Any]:
        """List deploys/config/cron changes and compare each target service's ERROR rate before vs after."""
        w = max(1, int(window_minutes)) * 60
        lo, hi = _ts(earliest) - w, _ts(latest)
        changes = self._run("sourcetype=change | table _id _time host service level message change_type", lo, hi, 200)["results"]
        errs = self._run("level=ERROR NOT sourcetype=metric NOT sourcetype=change | table _time service message",
                         lo, hi, FETCH_LIMIT)["results"]
        parsed = [(_ts(e["_time"]), e["service"], normalize(e["message"])) for e in errs]
        out = []
        for c in changes:
            tc = _ts(c["_time"])
            before = [m for t, s, m in parsed if s == c["service"] and tc - w <= t < tc]
            after = [m for t, s, m in parsed if s == c["service"] and tc <= t < tc + w]
            known = set(before)
            new = Counter(m for m in after if m not in known)
            score = 0.0
            if new:
                score = max(0.0, (len(after) - len(before)) / (len(after) + len(before) + 5))
            out.append({
                "id": c["_id"], "time": c["_time"], "service": c["service"], "host": c["host"],
                "type": c.get("change_type", ""), "message": c["message"],
                "errors_before": len(before), "errors_after": len(after), "suspicion": round(score, 2),
                "new_error_patterns": [{"pattern": p, "count": n} for p, n in new.most_common(3)],
            })
        out.sort(key=lambda x: (-x["suspicion"], x["time"]))
        return {"window_minutes": w // 60, "changes": out[:MAX_ROWS]}

    # ------------------------------------------------------------------- schemas
    def specs(self) -> list[dict[str, Any]]:
        def fn(name: str, desc: str, props: dict[str, Any], required: list[str]) -> dict[str, Any]:
            return {"type": "function", "function": {
                "name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required}}}

        s = {"type": "string"}
        t_e = {"type": "string", "description": "ISO-8601 start of window (inclusive)"}
        t_l = {"type": "string", "description": "ISO-8601 end of window (exclusive)"}
        return [
            fn("search", "Run an SPL-like search and return raw events with ids (usable as citations). "
               "Syntax: field=value, wildcards*, \"phrases\", AND/OR/NOT, then '| sort', '| head N', '| table'.",
               {"query": s, "earliest": t_e, "latest": t_l, "limit": {"type": "integer"}}, ["query", "earliest", "latest"]),
            fn("aggregate", "Run an SPL query with stats/top/where/sort, e.g. 'sourcetype=metric metric=mem_pct "
               "| stats max(value) by host'.", {"query": s, "earliest": t_e, "latest": t_l}, ["query", "earliest", "latest"]),
            fn("timechart", "Count or aggregate events over time buckets, optionally split by a field.",
               {"search": s, "earliest": t_e, "latest": t_l, "span": {"type": "string"},
                "agg": {"type": "string", "description": "count, avg(field), max(field)..."}, "by": s},
               ["search", "earliest", "latest"]),
            fn("top_errors", "Group WARN/ERROR logs into templates ranked by frequency, with services, hosts and sample event ids.",
               {"earliest": t_e, "latest": t_l, "service": s, "limit": {"type": "integer"}}, ["earliest", "latest"]),
            fn("change_correlation", "List recent deploys/config/cron changes and show how each target service's errors "
               "changed right after it.", {"earliest": t_e, "latest": t_l, "window_minutes": {"type": "integer"}},
               ["earliest", "latest"]),
        ]
