import re

from splunkpilot.generator import CATEGORIES, SCENARIOS, generate
from splunkpilot.hec import parse_hec_objects


def test_five_labeled_scenarios():
    assert set(SCENARIOS) == set(CATEGORIES) and len(SCENARIOS) == 5


def test_deterministic_and_seed_sensitive():
    a, b, c = generate("bad_deploy", 3), generate("bad_deploy", 3), generate("bad_deploy", 4)
    assert a.events == b.events and a.truth.onset != c.truth.onset


def test_events_are_valid_hec_and_ordered(incidents):
    for inc in incidents.values():
        ts = [e["time"] for e in inc.events]
        assert ts == sorted(ts) and len(ts) > 5000
        parse_hec_objects(inc.events)  # raises on malformed events


def test_label_is_not_leaked_into_events(incidents):
    for sc, inc in incidents.items():
        blob = " ".join(str(e) for e in inc.events).lower()
        assert "ground" not in blob and f"scenario={sc}" not in blob
        assert inc.truth.alert["index"] in {e["index"] for e in inc.events}


def test_incident_signature_present_after_onset(incidents):
    for sc, inc in incidents.items():
        pats = [re.compile(p, re.I) for p in inc.truth.evidence_patterns]
        hits = [e for e in inc.events if e["time"] >= inc.truth.onset - 3600
                and any(p.search(e["event"]["message"]) for p in pats)]
        assert hits, sc
        assert any(e["time"] >= inc.truth.onset for e in hits), sc


def test_alert_window_contains_onset(incidents):
    from splunkpilot.spl import parse_time

    for inc in incidents.values():
        a = inc.truth.alert
        assert parse_time(a["earliest"]) < inc.truth.onset < parse_time(a["fired_at"]) < parse_time(a["latest"])


def test_decoy_change_exists_in_non_deploy_scenarios(incidents):
    for sc, inc in incidents.items():
        deploys = [e for e in inc.events if e["sourcetype"] == "change" and "deploy" in e["event"]["message"]]
        assert len(deploys) >= 2, sc
