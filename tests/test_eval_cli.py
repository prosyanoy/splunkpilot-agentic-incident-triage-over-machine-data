import json

import pytest

from splunkpilot.agent import MockLLM
from splunkpilot.agent.report import RCAReport
from splunkpilot.cli import main
from splunkpilot.evaluation import W_CATEGORY, format_table, load_baseline, run_eval, score_rca, score_report, summarise
from splunkpilot.generator import scenario_names


def test_score_rca_weights(incidents):
    truth = incidents["disk_full"].truth
    perfect = score_rca("disk_full", "db", "db-01", ["No space left on device"], truth)
    assert perfect.total == 1.0
    wrong_cat = score_rca("dns_failure", "db", "db-01", ["No space left"], truth)
    assert wrong_cat.total == pytest.approx(1.0 - W_CATEGORY)
    assert score_rca("disk_full", "db", "db-01", [], truth).evidence_precision == 0.0
    assert score_rca("disk_full", "db", "db-01", ["No space left", "unrelated"], truth).evidence_precision == 0.5


def test_baseline_covers_all_scenarios_and_scores_perfectly(incidents):
    base = load_baseline()
    for sc in scenario_names():
        b = base[sc]
        assert b["minutes"] > 0
        assert score_rca(b["category"], b["service"], b["host"], b["evidence_messages"], incidents[sc].truth).total == 1.0


def test_run_eval_offline_mock_is_accurate():
    results = run_eval(MockLLM, seeds=2)
    s = summarise(results)
    assert len(results) == 10 and not any(r.error for r in results)
    assert s["overall"]["root_cause_accuracy"] == 1.0
    assert s["overall"]["evidence_precision"] >= 0.9
    assert s["overall"]["agent_seconds"] < s["overall"]["human_baseline_minutes"] * 60
    assert "OVERALL" in format_table(s, "mock")


def test_run_eval_scores_failures_as_misses():
    class Broken:
        name = "broken"

        def chat(self, messages, tools):
            return {"role": "assistant", "content": "garbage"}

    results = run_eval(Broken, ["disk_full"], seeds=1)
    assert results[0].error and results[0].score.total == 0
    assert summarise(results)["overall"]["root_cause_accuracy"] == 0.0


def test_unknown_report_scores_zero_category(incidents):
    assert score_report(RCAReport(), incidents["disk_full"].truth).category is False


def test_cli_replay_prints_rca(capsys):
    assert main(["replay", "bad_deploy"]) == 0
    out = capsys.readouterr().out
    assert "RCA:" in out and "bad_deploy" in out and "CORRECT" in out and "Evidence" in out


def test_cli_eval_writes_json(tmp_path, capsys):
    path = tmp_path / "r.json"
    assert main(["eval", "--seeds", "1", "--scenario", "dns_failure", "--json", str(path)]) == 0
    data = json.loads(path.read_text())
    assert data["summary"]["overall"]["runs"] == 1 and data["runs"][0]["scenario"] == "dns_failure"


def test_cli_generate_and_scenarios(tmp_path, capsys):
    out = tmp_path / "e.jsonl"
    assert main(["generate", "auth_storm", "--out", str(out)]) == 0
    assert json.loads(out.read_text().splitlines()[0])["index"] == "auth_storm_0"
    main(["scenarios"])
    assert "disk_full" in capsys.readouterr().out


def test_replay_over_http_api(monkeypatch, capsys):
    """End to end: HEC ingest -> search API -> agent, through the FastAPI app."""
    from fastapi.testclient import TestClient

    import splunkpilot.cli as cli
    from splunkpilot.api import create_app
    from splunkpilot.backend import HttpBackend
    from splunkpilot.config import Settings
    from splunkpilot.store import EventStore

    client = TestClient(create_app(EventStore(), Settings(hec_token="t")))
    monkeypatch.setattr(cli, "_backend", lambda args, settings: HttpBackend("", "t", client=client))
    assert main(["replay", "memory_leak", "--url", "http://testserver"]) == 0
    assert "memory_leak" in capsys.readouterr().out
    assert client.get("/health").json()["events"] > 5000
