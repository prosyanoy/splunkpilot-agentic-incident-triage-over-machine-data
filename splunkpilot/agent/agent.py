"""Tool-calling agent loop that turns an alert into a verified RCA report."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from ..backend import Backend
from .llm import LLM
from .report import REPORT_SCHEMA_HINT, RCAReport
from .tools import ToolBox

SYSTEM_PROMPT = f"""You are SplunkPilot, an SRE incident investigator with read-only access to machine data \
(logs, metrics and change events) through tools. You are given an alert; find the root cause.

Method: (1) find which error templates dominate and when they start (top_errors, timechart); \
(2) check whether a deploy/config/cron change preceded the onset (change_correlation); \
(3) look for infrastructure signals in metrics (cpu_pct, mem_pct, disk_pct via sourcetype=metric); \
(4) verify your hypothesis with a targeted search. Distinguish cause from symptoms: downstream services that \
only report timeouts are victims. Only cite event ids that tools returned.

When done, reply with ONLY a JSON object (no tool call) of this shape:
{REPORT_SCHEMA_HINT}"""


@dataclass
class Step:
    tool: str
    arguments: dict[str, Any]
    summary: str


@dataclass
class Investigation:
    report: RCAReport
    steps: list[Step] = field(default_factory=list)
    seconds: float = 0.0
    llm: str = ""

    @property
    def tool_calls(self) -> int:
        return len(self.steps)


class AgentError(RuntimeError):
    pass


def _summarise(result: dict[str, Any]) -> str:
    if "error" in result:
        return f"error: {result['error']}"
    for k in ("events", "rows", "groups", "changes"):
        if k in result:
            return f"{len(result[k])} {k}"
    return "ok"


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


class Agent:
    def __init__(self, backend: Backend, llm: LLM, max_steps: int = 10) -> None:
        self.backend, self.llm, self.max_steps = backend, llm, max_steps

    def investigate(self, alert: dict[str, Any]) -> Investigation:
        t0 = time.perf_counter()
        tools = ToolBox(self.backend, alert.get("index"))
        user = ("Investigate this alert and produce the RCA report.\n\nALERT:\n"
                + json.dumps(alert, indent=2) + "\n\nEND ALERT")
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT},
                                          {"role": "user", "content": user}]
        steps: list[Step] = []
        report: RCAReport | None = None
        repairs = 0
        for _ in range(self.max_steps + 3):
            budget_left = len(steps) < self.max_steps
            reply = self.llm.chat(messages, tools.specs() if budget_left else None)
            calls = reply.get("tool_calls") or []
            if calls and budget_left:
                messages.append(reply)
                for c in calls:
                    fn = c["function"]
                    result = tools.call(fn["name"], fn.get("arguments"))
                    steps.append(Step(fn["name"], fn.get("arguments") or {}, _summarise(result)))
                    messages.append({"role": "tool", "name": fn["name"], "content": json.dumps(result)})
                continue
            if calls:  # tool budget exhausted: demand the answer
                messages.append({"role": "user", "content": "Tool budget exhausted. Reply now with the final JSON report only."})
                continue
            try:
                report = RCAReport.model_validate(_extract_json(reply.get("content", "")))
                break
            except (ValueError, ValidationError) as e:
                if repairs >= 2:
                    raise AgentError(f"model did not return a valid report: {e}") from e
                repairs += 1
                messages.append(reply)
                messages.append({"role": "user", "content": f"Invalid report ({e}). Reply with ONLY the JSON object."})
        if report is None:
            raise AgentError("agent did not produce a report")
        self._verify_citations(report)
        return Investigation(report, steps, time.perf_counter() - t0, getattr(self.llm, "name", "llm"))

    def _verify_citations(self, report: RCAReport) -> None:
        """Replace LLM-supplied citation details with the stored event; drop ids that don't exist."""
        kept, dropped, seen = [], [], set()
        for ev in report.evidence:
            if ev.event_id in seen:
                continue
            seen.add(ev.event_id)
            row = self.backend.get_event(ev.event_id)
            if row is None:
                dropped.append(ev.event_id)
                continue
            ev.time, ev.host, ev.service, ev.message = row["_time"], row["host"], row["service"], row["message"]
            kept.append(ev)
        report.evidence, report.dropped_citations = kept, dropped
        if not kept:
            report.confidence = min(report.confidence, 0.3)
