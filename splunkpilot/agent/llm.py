"""LLM backends: a real Ollama client and a deterministic rule-based mock.

Both implement ``chat(messages, tools) -> assistant message`` using the Ollama/OpenAI message
shape: assistant messages may carry ``tool_calls`` and tool results come back as ``role: tool``.
"""
from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx

from ..config import Settings


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]: ...


# ---------------------------------------------------------------------- Ollama

class OllamaLLM:
    def __init__(self, base_url: str, model: str, client: httpx.Client | None = None, timeout: float = 300) -> None:
        self.name = f"ollama:{model}"
        self.model = model
        self.client = client or httpx.Client(base_url=base_url, timeout=timeout)

    def chat(self, messages, tools):
        body: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                "options": {"temperature": 0}}
        if tools:
            body["tools"] = tools
        else:
            body["format"] = "json"
        try:
            r = self.client.post("/api/chat", json=body)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama request failed ({e}); is `ollama serve` running and the model pulled?") from e
        msg = r.json().get("message") or {}
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})
        out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if calls:
            out["tool_calls"] = calls
        return out


# ------------------------------------------------------------------------ Mock

# (category, regex over normalised log templates). The mock is a rule-based "analyst": it reads
# tool output only, never the generator's ground truth.
SIGNATURES: dict[str, re.Pattern[str]] = {
    "disk_full": re.compile(r"no space left|disk usage|disk full|could not write", re.I),
    "memory_leak": re.compile(r"outofmemory|oomkilled|gc pause|heap", re.I),
    "dns_failure": re.compile(r"unknownhost|name resolution|servfail|eai_again|enotfound|resolver", re.I),
    "auth_storm": re.compile(r"login failed|invalid_credentials|rate limit", re.I),
}

# Background noise produces a handful of matching lines; a real signature needs more than that.
MIN_SIGNATURE_EVENTS = 10

REMEDIATION = {
    "bad_deploy": ["Roll back {service} to the previous version", "Add a canary stage/error-rate gate to the deploy pipeline",
                   "Fix the failing code path and add a regression test"],
    "memory_leak": ["Restart/roll {service} on {host} to restore service", "Capture a heap dump and find the leaking allocation",
                    "Add a memory alert and container memory limit with graceful restarts"],
    "dns_failure": ["Fail over to the secondary resolver or restore {service}", "Check the upstream resolver health and forwarders",
                    "Add local DNS caching and resolver SLO alerts"],
    "auth_storm": ["Block or throttle the attacking source IPs at the edge", "Enable per-IP rate limits and CAPTCHA/step-up auth",
                   "Scale {service} and alert on login-failure ratio"],
    "disk_full": ["Free space on {host} (purge old logs/WAL archives) and restore writes", "Fix the failed log-rotation job",
                  "Add disk-usage alerts at 80/90% and automatic retention"],
    "unknown": ["Collect more telemetry and escalate to the owning team"],
}


def _tool_results(messages: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for m in messages:
        if m.get("role") == "tool":
            try:
                out.append((m.get("name", ""), json.loads(m["content"])))
            except (json.JSONDecodeError, KeyError):
                out.append((m.get("name", ""), {"error": "unparseable"}))
    return out


def _call(name: str, **arguments: Any) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


class MockLLM:
    """Deterministic scripted investigator.

    Plan: top_errors -> timechart by service -> change_correlation -> 1-2 verification queries
    chosen from the error signatures it saw -> final JSON report.
    """

    name = "mock"

    def chat(self, messages, tools):
        user = next(m["content"] for m in messages if m["role"] == "user")
        alert = json.loads(user.split("ALERT:\n", 1)[1].split("\n\nEND ALERT", 1)[0])
        lo, hi = alert["earliest"], alert["latest"]
        results = _tool_results(messages)
        n = len(results)
        if tools is not None:
            if n == 0:
                return _call("top_errors", earliest=lo, latest=hi, limit=12)
            if n == 1:
                return _call("timechart", search="level=ERROR", earliest=lo, latest=hi, span="2m", by="service")
            if n == 2:
                return _call("change_correlation", earliest=lo, latest=hi)
            plan = self._verification_plan(results, lo, hi)
            if n - 3 < len(plan):
                name, args = plan[n - 3]
                return _call(name, **args)
        return {"role": "assistant", "content": json.dumps(self._report(alert, results))}

    # -- analysis ---------------------------------------------------------------
    @staticmethod
    def _by_name(results, name):
        return [r for n, r in results if n == name]

    def _diagnose(self, results) -> dict[str, Any]:
        tops = self._by_name(results, "top_errors")
        changes = self._by_name(results, "change_correlation")
        groups = tops[0].get("groups", []) if tops and "groups" in tops[0] else []
        scores: dict[str, int] = {c: 0 for c in SIGNATURES}
        matched: dict[str, list[dict]] = {c: [] for c in SIGNATURES}
        for g in groups:
            for cat, rx in SIGNATURES.items():
                if rx.search(g["pattern"]):
                    scores[cat] += g["count"]
                    matched[cat].append(g)
        cat = max(scores, key=lambda c: (scores[c], c))
        best_change = None
        if changes and changes[0].get("changes"):
            c0 = changes[0]["changes"][0]
            if c0["suspicion"] >= 0.4:
                best_change = c0
        if scores[cat] < MIN_SIGNATURE_EVENTS:
            cat = "bad_deploy" if best_change else "unknown"
        diag: dict[str, Any] = {"category": cat, "change": best_change, "service": "", "host": "", "groups": []}
        if cat == "bad_deploy":
            diag["service"], diag["host"] = best_change["service"], ""
            new = {p["pattern"] for p in best_change["new_error_patterns"]}
            diag["groups"] = [g for g in groups if g["pattern"] in new][:3]
            diag["pivot"] = diag["groups"][0] if diag["groups"] else None
        elif cat != "unknown":
            gs = sorted(matched[cat], key=lambda g: -g["count"])
            key = [g for g in gs if re.search(r"servfail|resolver", g["pattern"], re.I)] if cat == "dns_failure" else []
            pivot = (key or gs)[0]
            diag["service"] = next(iter(pivot["services"]))
            diag["host"] = next(iter(pivot["hosts"]))
            diag["groups"] = gs[:3]
            diag["pivot"] = pivot
        diag["confidence"] = 0.9 if cat != "unknown" else 0.2
        return diag

    def _verification_plan(self, results, lo, hi):
        d = self._diagnose(results)
        svc, host, cat = d["service"], d["host"], d["category"]
        metric = {"disk_full": "disk_pct", "memory_leak": "mem_pct", "auth_storm": "cpu_pct"}.get(cat)
        plan: list[tuple[str, dict]] = []
        if metric:
            plan.append(("search", dict(query=f"sourcetype=metric metric={metric} host={host} | sort -value | head 1",
                                        earliest=lo, latest=hi, limit=1, order="desc")))
        if cat == "auth_storm":
            plan.append(("aggregate", dict(query='service=auth-service "login failed" | top limit=3 ip', earliest=lo, latest=hi)))
        elif cat in ("dns_failure", "bad_deploy") and d.get("pivot"):
            phrase = max(re.split(r"[#<>]", d["pivot"]["pattern"]), key=len).strip()
            plan.append(("search", dict(query=f'service={svc} level=ERROR "{phrase}" | sort _time | head 3',
                                        earliest=lo, latest=hi, limit=3, order="desc")))
        elif cat == "disk_full":
            plan.append(("search", dict(query=f"host={host} sourcetype=change", earliest=lo, latest=hi, limit=3)))
        return plan

    def _report(self, alert, results) -> dict[str, Any]:
        d = self._diagnose(results)
        cat, svc, host = d["category"], d["service"], d["host"]
        ids: list[int] = []
        for g in d["groups"]:
            ids.extend(g["sample_ids"][:2])
        extra: dict[int, str] = {}
        for name, res in results[3:]:
            if name == "search":
                for ev in res.get("events", [])[:3]:
                    extra[ev["_id"]] = f"verification: {ev['message']}"
        if d["change"]:
            ids.append(d["change"]["id"])
        ids = list(dict.fromkeys([*extra, *ids]))[:8]
        why = {i: extra.get(i, "sample of the dominant error template for the suspected root cause") for i in ids}
        if d["change"]:
            why[d["change"]["id"]] = "change that preceded the error wave on the same service"

        top = d["groups"][0] if d["groups"] else None
        n_err = sum(g["count"] for g in d["groups"])
        timeline = [{"time": alert["fired_at"], "event": f"Alert fired: {alert['title']}"}]
        if d["change"] and cat == "bad_deploy":
            timeline.append({"time": d["change"]["time"], "event": d["change"]["message"]})
        if top:
            timeline.append({"time": top["first_seen"], "event": f"First '{top['pattern']}' on {svc}"})
        timeline.sort(key=lambda t: t["time"])

        desc = {
            "bad_deploy": lambda: f"{d['change']['message'] if d['change'] else 'A deploy'} was followed by "
                                  f"{d['change']['errors_after'] if d['change'] else '?'} new errors on {svc} "
                                  f"({d['change']['new_error_patterns'][0]['pattern'] if d['change'] and d['change']['new_error_patterns'] else 'new exception'}) "
                                  f"versus {d['change']['errors_before'] if d['change'] else '?'} before.",
            "memory_leak": lambda: f"{svc} on {host} is exhausting heap: {top['pattern']} ({top['count']}x) with rising memory use and container restarts.",
            "dns_failure": lambda: f"{svc} on {host} is failing upstream lookups ({top['pattern']}), and dependent services cannot resolve internal hostnames.",
            "auth_storm": lambda: f"{svc} is flooded with failed logins ({top['pattern']}, {top['count']}x) from few source IPs, saturating CPU and delaying token validation.",
            "disk_full": lambda: f"{svc} on {host} ran out of disk space ({top['pattern']}), so writes fail and dependent services error out.",
            "unknown": lambda: "No dominant signature or suspicious change was found in the window.",
        }[cat]()
        return {
            "title": f"{cat.replace('_', ' ').title()} on {svc or 'unknown service'}",
            "summary": f"Alert '{alert['title']}' traced to {cat.replace('_', ' ')} in {svc or 'an unknown component'}. {desc}",
            "root_cause": {"category": cat, "service": svc, "host": host, "description": desc},
            "confidence": d["confidence"],
            "impact": f"{n_err} related WARN/ERROR events in the window; alerting service: {alert.get('service', 'n/a')}.",
            "timeline": timeline,
            "evidence": [{"event_id": i, "why": why[i]} for i in ids],
            "remediation": [r.format(service=svc, host=host or svc) for r in REMEDIATION[cat]],
        }


def make_llm(provider: str | None = None, settings: Settings | None = None) -> LLM:
    settings = settings or Settings.from_env()
    provider = (provider or settings.llm).lower()
    if provider == "mock":
        return MockLLM()
    if provider == "ollama":
        return OllamaLLM(settings.ollama_url, settings.ollama_model)
    raise ValueError(f"unknown LLM provider {provider!r} (use 'mock' or 'ollama')")
