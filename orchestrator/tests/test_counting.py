"""Several scenarios for one policy must not inflate the 'planted violations' count (seen live: a 6th row)."""
import copy
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient  # noqa: E402
from orchestrator.main import create_api  # noqa: E402
from orchestrator.policy_engine import MODEL, analyze  # noqa: E402

POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()
GOLD_R = (ROOT / "orchestrator/golden/rules.json").read_text()
GOLD_S = json.loads((ROOT / "orchestrator/golden/scenarios.json").read_text())


def model_with(extra):
    sc = copy.deepcopy(GOLD_S)
    sc["scenarios"].extend(extra)
    return lambda m, name: GOLD_R if m[0]["content"].startswith("You extract") else json.dumps(sc)


def p05(test_id, steps=None):
    s = copy.deepcopy(next(x for x in GOLD_S["scenarios"] if x["test_id"] == "P-05-ADV-001"))
    s["test_id"] = test_id
    if steps:
        s["steps"] = steps
    return s


def fake_runner(rule, sc, profile, out, on_event):
    bad = profile == "buggy" and sc["expected_outcome"] == "blocked"
    return {"verdict": "VIOLATION" if bad else "PASS", "reasons": ["fake"], "checks": {}, "observed": {},
            "artifact_sha256": {}}


def run_all(tmp_path, transport):
    c = TestClient(create_api(runs_dir=tmp_path, runner=fake_runner, transport=transport, daily_budget=50))
    a = c.post("/api/analyze", json={}).json()
    c.post(f"/api/analyses/{a['analysis_id']}/approve", json={})
    rid = c.post("/api/runs", json={"analysis_id": a["analysis_id"]}).json()["run_id"]
    for _ in range(200):
        s = c.get(f"/api/runs/{rid}").json()
        if s["status"] in ("finished", "failed"):
            return a, s["summary"]
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_exact_duplicate_scenario_is_dropped_not_rejected():
    out = analyze(POLICY, transport=model_with([p05("P-05-CTL-001")]))
    assert out["source"]["scenarios"] == MODEL                       # still accepted as live model output
    assert len(out["scenarios"]) == 9 and "P-05-CTL-001" not in [s.test_id for s in out["scenarios"]]


def test_a_genuinely_different_second_scenario_is_kept():
    steps = [{"action": "login", "role": "customer"}, {"action": "goto", "path": "/orders"},
             {"action": "goto", "path": "/orders/1002"}]
    out = analyze(POLICY, transport=model_with([p05("P-05-CTL-001", steps)]))
    assert len(out["scenarios"]) == 10


def test_two_violating_scenarios_for_one_policy_count_as_one_planted_bug(tmp_path):
    steps = [{"action": "login", "role": "customer"}, {"action": "goto", "path": "/orders"},
             {"action": "goto", "path": "/orders/1002"}]
    a, s = run_all(tmp_path, model_with([p05("P-05-CTL-001", steps)]))
    assert len(a["scenarios"]) == 10 and s["cases"] == 20
    assert s["planted_violations_detected"] == 5                    # five policies, five planted bugs
    assert s["violating_scenarios"] == 6                            # the raw count stays visible
    assert s["policies_tested"] == 5
    assert s["false_positives_on_correct_build"] == 0


def test_normal_run_numbers_are_unchanged(tmp_path):
    a, s = run_all(tmp_path, None)
    assert s["cases"] == 18 and s["planted_violations_detected"] == 5 and s["violating_scenarios"] == 5


def test_gate_report_counts_policies_not_scenarios(tmp_path):
    run = tmp_path / "run1"
    for test_id, rule, profile, verdict in [("P-05-ADV-001", "P-05", "buggy", "VIOLATION"),
                                            ("P-05-CTL-001", "P-05", "buggy", "VIOLATION"),
                                            ("P-01-ADV-001", "P-01", "buggy", "VIOLATION"),
                                            ("P-05-ADV-001", "P-05", "correct", "PASS")]:
        d = run / f"{test_id}__{profile}"
        d.mkdir(parents=True)
        (d / "verdict.json").write_text(json.dumps({
            "verdict": verdict, "rule_id": rule,
            "sandbox": {"cleanup_confirmed": True, "containment": {}, "leftovers": []}}))
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "gate_report.py"), str(tmp_path)],
                         capture_output=True, text=True).stdout
    row = next(line for line in out.splitlines() if "run1" in line).split()
    assert row[3] == "4"                                            # cases
    assert row[4] == "2"                                            # planted: 2 policies, not 3 scenarios
