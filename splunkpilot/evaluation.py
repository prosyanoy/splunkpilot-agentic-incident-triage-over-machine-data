"""Offline evaluation harness: root-cause accuracy and time-to-diagnosis vs a human baseline."""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import asdict, dataclass
from importlib import resources
from typing import Any, Callable

from .agent import Agent, LLM
from .agent.report import RCAReport
from .backend import LocalBackend
from .generator import GroundTruth, generate, scenario_names

# Score weights: category is what matters most; service/host localise; evidence must be on-topic.
W_CATEGORY, W_SERVICE, W_HOST, W_EVIDENCE = 0.5, 0.2, 0.1, 0.2


def load_baseline() -> dict[str, Any]:
    with resources.files("splunkpilot.data").joinpath("human_baseline.json").open() as f:
        return json.load(f)


@dataclass
class Score:
    category: bool
    service: bool
    host: bool
    evidence_precision: float
    total: float


def score_rca(category: str, service: str, host: str, evidence_messages: list[str], truth: GroundTruth) -> Score:
    """Score an RCA (agent's or human's) against the scenario label."""
    cat_ok = category == truth.category
    svc_ok = service == truth.service
    host_ok = (host == truth.host) if truth.host else True
    pats = [re.compile(p, re.I) for p in truth.evidence_patterns]
    hits = sum(any(p.search(m) for p in pats) for m in evidence_messages)
    prec = hits / len(evidence_messages) if evidence_messages else 0.0
    total = W_CATEGORY * cat_ok + W_SERVICE * svc_ok + W_HOST * host_ok + W_EVIDENCE * prec
    return Score(cat_ok, svc_ok, host_ok, round(prec, 3), round(total, 3))


def score_report(report: RCAReport, truth: GroundTruth) -> Score:
    rc = report.root_cause
    return score_rca(rc.category, rc.service, rc.host, [e.message for e in report.evidence], truth)


@dataclass
class RunResult:
    scenario: str
    seed: int
    predicted: str
    score: Score
    agent_seconds: float
    tool_calls: int
    baseline_minutes: float
    baseline_score: float
    error: str = ""


def run_eval(
    llm_factory: Callable[[], LLM],
    scenarios: list[str] | None = None,
    seeds: int = 3,
) -> list[RunResult]:
    baseline = load_baseline()
    results = []
    for sc in scenarios or scenario_names():
        for seed in range(seeds):
            inc = generate(sc, seed)
            backend = LocalBackend()
            backend.ingest(inc.events)
            b = baseline[sc]
            b_score = score_rca(b["category"], b["service"], b["host"], b["evidence_messages"], inc.truth).total
            try:
                inv = Agent(backend, llm_factory()).investigate(inc.truth.alert)
            except Exception as e:  # a failed run is a scored miss, not a crash
                zero = Score(False, False, False, 0.0, 0.0)
                results.append(RunResult(sc, seed, "error", zero, 0.0, 0, b["minutes"], b_score, f"{type(e).__name__}: {e}"))
                continue
            results.append(RunResult(sc, seed, inv.report.root_cause.category, score_report(inv.report, inc.truth),
                                     inv.seconds, inv.tool_calls, b["minutes"], b_score))
    return results


def summarise(results: list[RunResult]) -> dict[str, Any]:
    def agg(rs: list[RunResult]) -> dict[str, Any]:
        n = len(rs)
        secs = statistics.mean(r.agent_seconds for r in rs)
        base_s = statistics.mean(r.baseline_minutes for r in rs) * 60
        return {
            "runs": n,
            "root_cause_accuracy": round(sum(r.score.category for r in rs) / n, 3),
            "service_accuracy": round(sum(r.score.service for r in rs) / n, 3),
            "evidence_precision": round(statistics.mean(r.score.evidence_precision for r in rs), 3),
            "mean_score": round(statistics.mean(r.score.total for r in rs), 3),
            "baseline_mean_score": round(statistics.mean(r.baseline_score for r in rs), 3),
            "agent_seconds": round(secs, 2),
            "tool_calls": round(statistics.mean(r.tool_calls for r in rs), 1),
            "human_baseline_minutes": round(base_s / 60, 1),
            "speedup_vs_human": round(base_s / secs, 1) if secs > 0 else None,
        }

    per = {sc: agg([r for r in results if r.scenario == sc]) for sc in dict.fromkeys(r.scenario for r in results)}
    return {"overall": agg(results), "per_scenario": per}


def format_table(summary: dict[str, Any], llm_name: str) -> str:
    cols = [("scenario", 13), ("runs", 4), ("rc_acc", 6), ("svc_acc", 7), ("evid_p", 6), ("score", 5),
            ("human_score", 11), ("agent_s", 7), ("tools", 5), ("human_min", 9), ("speedup", 8)]
    head = " ".join(n.ljust(w) for n, w in cols)
    lines = [f"Evaluation ({llm_name})", head, "-" * len(head)]
    rows = {**summary["per_scenario"], "OVERALL": summary["overall"]}
    for name, a in rows.items():
        vals = [name, a["runs"], a["root_cause_accuracy"], a["service_accuracy"], a["evidence_precision"],
                a["mean_score"], a["baseline_mean_score"], a["agent_seconds"], a["tool_calls"],
                a["human_baseline_minutes"], f"{a['speedup_vs_human']}x"]
        lines.append(" ".join(str(v).ljust(w) for v, (_, w) in zip(vals, cols)))
    return "\n".join(lines)


def results_json(results: list[RunResult]) -> list[dict[str, Any]]:
    return [asdict(r) for r in results]
