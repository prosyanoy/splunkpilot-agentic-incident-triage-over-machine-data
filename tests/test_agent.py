import json

import httpx
import pytest

from splunkpilot.agent import Agent, MockLLM, OllamaLLM
from splunkpilot.agent.agent import AgentError
from splunkpilot.agent.llm import LLMError, make_llm
from splunkpilot.agent.tools import ToolBox, normalize
from splunkpilot.generator import scenario_names


def test_normalize_collapses_variables():
    assert normalize("login failed user=user_81 ip=1.2.3.4") == "login failed user=user_# ip=<ip>"
    assert normalize("slow request 400ms") == normalize("slow request 950ms")


def test_top_errors_groups_templates(loaded):
    b, inc = loaded("disk_full")
    a = inc.truth.alert
    res = ToolBox(b, a["index"]).top_errors(a["earliest"], a["latest"])
    patterns = [g["pattern"] for g in res["groups"]]
    assert any("No space left" in p for p in patterns)
    g = next(g for g in res["groups"] if "No space left" in g["pattern"])
    assert g["services"] == {"db": g["count"]} and g["sample_ids"]


def test_change_correlation_ranks_bad_deploy_above_decoy(loaded):
    b, inc = loaded("bad_deploy")
    a = inc.truth.alert
    changes = ToolBox(b, a["index"]).change_correlation(a["earliest"], a["latest"])["changes"]
    assert len(changes) >= 3
    assert changes[0]["service"] == "payments" and changes[0]["suspicion"] > 0.5
    assert changes[0]["new_error_patterns"][0]["pattern"].startswith("java.lang.NullPointerException")
    assert all(c["suspicion"] == 0 for c in changes[1:])


def test_change_correlation_does_not_blame_decoys(loaded):
    b, inc = loaded("dns_failure")
    a = inc.truth.alert
    assert all(c["suspicion"] < 0.4 for c in ToolBox(b, a["index"]).change_correlation(a["earliest"], a["latest"])["changes"])


def test_toolbox_errors_are_returned_not_raised(loaded):
    b, inc = loaded("disk_full")
    t = ToolBox(b, inc.truth.alert["index"])
    assert "unknown tool" in t.call("nope", {})["error"]
    assert "error" in t.call("search", {"query": "| bad", "earliest": "-1h", "latest": "now"})
    assert "error" in t.call("search", {"bogus": 1})
    assert "error" in t.call("search", "not json")
    a = inc.truth.alert
    r = t.call("timechart", {"search": "level=ERROR", "earliest": a["earliest"], "latest": a["latest"], "by": "service"})
    assert r["rows"] and "db" in r["fields"]


def test_tool_scope_is_limited_to_alert_index(loaded, incidents):
    b, inc = loaded("disk_full")
    b.ingest(incidents["dns_failure"].events)
    a = inc.truth.alert
    t = ToolBox(b, a["index"])
    r = t.call("aggregate", {"query": "SERVFAIL | stats count", "earliest": a["earliest"], "latest": a["latest"]})
    assert r["rows"] == [{"count": 0}]  # present in the dns_failure index, invisible from this one


@pytest.mark.parametrize("scenario", scenario_names())
def test_mock_agent_diagnoses_every_scenario(loaded, scenario):
    b, inc = loaded(scenario)
    result = Agent(b, MockLLM()).investigate(inc.truth.alert)
    rc = result.report.root_cause
    assert (rc.category, rc.service) == (inc.truth.category, inc.truth.service)
    assert result.tool_calls >= 3 and result.report.evidence and not result.report.dropped_citations
    assert all(e.message and e.time for e in result.report.evidence)


def test_hallucinated_citations_are_dropped_and_details_overwritten(loaded):
    b, inc = loaded("disk_full")

    class Liar(MockLLM):
        def chat(self, messages, tools):
            m = super().chat(messages, tools)
            if "tool_calls" in m:
                return m
            rep = json.loads(m["content"])
            rep["evidence"] = [{"event_id": 10**9, "why": "invented", "message": "fake"}, rep["evidence"][0]]
            return {"role": "assistant", "content": "```json\n" + json.dumps(rep) + "\n```"}

    rep = Agent(b, Liar()).investigate(inc.truth.alert).report
    assert rep.dropped_citations == [10**9] and len(rep.evidence) >= 1
    assert all(e.message != "fake" for e in rep.evidence)


def test_no_valid_citations_caps_confidence(loaded):
    b, inc = loaded("disk_full")

    class NoEvidence(MockLLM):
        def chat(self, messages, tools):
            m = super().chat(messages, tools)
            if "tool_calls" in m:
                return m
            rep = json.loads(m["content"])
            rep["evidence"] = [{"event_id": -5}]
            return {"role": "assistant", "content": json.dumps(rep)}

    assert Agent(b, NoEvidence()).investigate(inc.truth.alert).report.confidence <= 0.3


def test_invalid_json_is_repaired_once_then_fails(loaded):
    b, inc = loaded("disk_full")

    class Flaky:
        name = "flaky"
        calls = 0

        def chat(self, messages, tools):
            Flaky.calls += 1
            return {"role": "assistant", "content": "I think it's the disk." if Flaky.calls == 1 else
                    json.dumps({"root_cause": {"category": "Disk Full", "service": "db"}, "confidence": 0.5})}

    inv = Agent(b, Flaky()).investigate(inc.truth.alert)
    assert inv.report.root_cause.category == "disk_full"

    class Hopeless:
        name = "h"

        def chat(self, messages, tools):
            return {"role": "assistant", "content": "nope"}

    with pytest.raises(AgentError):
        Agent(b, Hopeless()).investigate(inc.truth.alert)


def test_tool_budget_is_enforced(loaded):
    b, inc = loaded("disk_full")

    class Loop:
        name = "loop"

        def chat(self, messages, tools):
            if tools is None:
                return {"role": "assistant", "content": json.dumps({"summary": "gave up"})}
            return {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "top_errors", "arguments": {"earliest": "-1h", "latest": "now"}}}]}

    inv = Agent(b, Loop(), max_steps=3).investigate(inc.truth.alert)
    assert inv.tool_calls == 3 and inv.report.summary == "gave up"


def test_ollama_client_tool_loop_with_mock_transport(loaded):
    b, inc = loaded("disk_full")
    inner = MockLLM()
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert body["model"] == "test-model" and body["stream"] is False
        reply = inner.chat(body["messages"], body.get("tools"))
        msg = dict(reply)
        for c in msg.get("tool_calls", []):  # Ollama may return arguments as a JSON string
            c["function"]["arguments"] = json.dumps(c["function"]["arguments"])
        return httpx.Response(200, json={"message": msg})

    llm = OllamaLLM("http://ollama", "test-model", client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://ollama"))
    result = Agent(b, llm).investigate(inc.truth.alert)
    assert result.report.root_cause.category == "disk_full"
    assert seen[0]["tools"] and len(seen) >= 4


def test_ollama_unreachable_raises_llm_error():
    def boom(request):
        raise httpx.ConnectError("refused")

    llm = OllamaLLM("http://x", "m", client=httpx.Client(transport=httpx.MockTransport(boom), base_url="http://x"))
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "hi"}], None)


def test_make_llm():
    assert make_llm("mock").name == "mock"
    assert make_llm("ollama").name.startswith("ollama:")
    with pytest.raises(ValueError):
        make_llm("gpt")
