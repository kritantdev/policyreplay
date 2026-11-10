import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient  # noqa: E402
from orchestrator.main import create_api  # noqa: E402

GOLD_RULES = (ROOT / "orchestrator/golden/rules.json").read_text()
GOLD_SC = (ROOT / "orchestrator/golden/scenarios.json").read_text()
N_RULES = len(json.loads(GOLD_RULES)["rules"])
N_UNSUPPORTED = len(json.loads(GOLD_RULES)["unsupported_clauses"])
SCENARIOS = json.loads(GOLD_SC)["scenarios"]
N_SCENARIOS = len(SCENARIOS)
N_BLOCKED = sum(x["expected_outcome"] == "blocked" for x in SCENARIOS)     # planted violations expected
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


def fake_runner(rule, sc, profile, out, on_event):
    """Pretends to be the pipeline: writes the same artifact layout, buggy build -> VIOLATION."""
    out = Path(out)
    (out / "evidence" / "screenshots").mkdir(parents=True, exist_ok=True)
    (out / "evidence" / "screenshots" / "01_login.png").write_bytes(PNG)
    (out / "evidence" / "trace.json").write_text(json.dumps([
        {"step": 1, "action": "login", "role": "support", "ok": True, "error": None,
         "screenshot": "screenshots/01_login.png", "ts": 1}]))
    (out / "evidence" / "manifest.json").write_text(json.dumps({"screenshots/01_login.png": "ab" * 32}))
    (out / "verdict.json").write_text("{}")
    (out / "pre.db").write_bytes(b"secret-db")
    on_event({"type": "stage", "text": f"fake run on {profile}"})
    bad = profile == "buggy" and sc["expected_outcome"] == "blocked"
    return {"verdict": "VIOLATION" if bad else "PASS", "reasons": ["fake"], "checks": {}, "observed": {},
            "artifact_sha256": {"pre.db": "cd" * 32}}


@pytest.fixture
def api(tmp_path):
    return TestClient(create_api(runs_dir=tmp_path, runner=fake_runner, mode_label="test mode"))


def analyze(c, **kw):
    r = c.post("/api/analyze", json={"replay": True, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def wait_done(c, run_id, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = c.get(f"/api/runs/{run_id}").json()
        if s["status"] in ("finished", "failed"):
            return s
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def approved_run(c):
    a = analyze(c)
    assert c.post(f"/api/analyses/{a['analysis_id']}/approve", json={}).status_code == 200
    r = c.post("/api/runs", json={"analysis_id": a["analysis_id"]})
    assert r.status_code == 202, r.text
    return a, r.json()["run_id"]


def test_analyze_returns_labeled_unapproved_scenarios(api):
    a = analyze(api)
    assert a["source"] == {"rules": "replayed model output", "scenarios": "replayed model output"}
    assert (len(a["rules"]), len(a["scenarios"]), len(a["unsupported"])) == (N_RULES, N_SCENARIOS, N_UNSUPPORTED)
    assert not any(s["approved"] for s in a["scenarios"])


def test_cannot_run_without_approval(api):
    a = analyze(api)
    r = api.post("/api/runs", json={"analysis_id": a["analysis_id"]})
    assert r.status_code == 400 and "approve" in r.json()["detail"]


def test_approve_rejects_unknown_ids(api):
    a = analyze(api)
    assert api.post(f"/api/analyses/{a['analysis_id']}/approve", json={"test_ids": ["nope"]}).status_code == 400
    assert api.post("/api/analyses/doesnotexist/approve", json={}).status_code == 404


def test_full_run_summary_and_zero_false_positives(api):
    a, run_id = approved_run(api)
    s = wait_done(api, run_id)
    assert s["status"] == "finished" and s["progress"] == {"done": 2 * N_SCENARIOS, "total": 2 * N_SCENARIOS}
    assert s["summary"]["planted_violations_detected"] == N_BLOCKED   # one per blocked-outcome scenario
    assert s["summary"]["false_positives_on_correct_build"] == 0
    assert s["summary"]["inconclusive"] == 0


def test_partial_approval_runs_only_approved(api):
    a = analyze(api)
    api.post(f"/api/analyses/{a['analysis_id']}/approve", json={"test_ids": ["P-05-ADV-001"]})
    run_id = api.post("/api/runs", json={"analysis_id": a["analysis_id"]}).json()["run_id"]
    assert wait_done(api, run_id)["progress"]["total"] == 2          # buggy + correct twin


def test_only_one_concurrent_run(tmp_path):
    gate = threading.Event()

    def slow(rule, sc, profile, out, on_event):
        gate.wait(5)
        return fake_runner(rule, sc, profile, out, on_event)
    c = TestClient(create_api(runs_dir=tmp_path, runner=slow))
    a = analyze(c)
    c.post(f"/api/analyses/{a['analysis_id']}/approve", json={})
    first = c.post("/api/runs", json={"analysis_id": a["analysis_id"]})
    second = c.post("/api/runs", json={"analysis_id": a["analysis_id"]})
    assert first.status_code == 202 and second.status_code == 409
    gate.set()
    wait_done(c, first.json()["run_id"])
    assert c.post("/api/runs", json={"analysis_id": a["analysis_id"]}).status_code == 202   # slot freed


def test_sse_stream_ends_with_run_finished(api):
    _, run_id = approved_run(api)
    wait_done(api, run_id)
    with api.stream("GET", f"/api/runs/{run_id}/events") as r:
        text = "".join(r.iter_text())
    assert "event: run_started" in text and "event: step" in text and "event: verdict" in text
    assert text.rstrip().splitlines()[-1].startswith("data:") and "event: run_finished" in text
    with api.stream("GET", f"/api/runs/{run_id}/events?after=9999") as r:        # resume past the end
        assert "".join(r.iter_text()) == ""


def test_report_has_clause_trace_hashes_limits_and_labels(api):
    _, run_id = approved_run(api)
    wait_done(api, run_id)
    rep = api.get(f"/api/runs/{run_id}/report").json()
    assert rep["execution_mode"] == "test mode" and rep["human_approved_at"]
    assert rep["model_output"]["rules"] == "replayed model output"
    assert len(rep["limitations"]) >= 3
    c0 = rep["cases"][0]
    assert c0["policy_clause"] and c0["trace"][0]["screenshot"].startswith(f"/api/runs/{run_id}/cases/")
    assert c0["manifest_sha256"] and c0["artifact_sha256"]


def test_artifact_endpoint_serves_screenshots_only_from_whitelist(api):
    _, run_id = approved_run(api)
    wait_done(api, run_id)
    case = api.get(f"/api/runs/{run_id}/report").json()["cases"][0]
    cid = case["case_id"]
    base = f"/api/runs/{run_id}/cases/{cid}/file"
    assert api.get(f"{base}/evidence/screenshots/01_login.png").content == PNG
    assert api.get(f"{base}/verdict.json").status_code == 200
    assert api.get(f"{base}/pre.db").status_code == 404                           # DB snapshots not served
    assert api.get(f"{base}/../../../etc/passwd").status_code == 404
    assert api.get(f"{base}/evidence/screenshots/..%2F..%2Fpre.db").status_code == 404
    assert api.get(f"/api/runs/{run_id}/cases/not-a-case/file/verdict.json").status_code == 404


def test_rate_limit_on_analyze(tmp_path):
    c = TestClient(create_api(runs_dir=tmp_path, runner=fake_runner, limits={"analyze": (2, 60)}))
    assert [c.post("/api/analyze", json={"replay": True}).status_code for _ in range(3)] == [200, 200, 429]


def test_policy_length_cap_and_unknown_policy(api):
    assert api.post("/api/analyze", json={"policy_text": "x" * 4001}).status_code == 422
    r = api.post("/api/analyze", json={"policy_text": "Refunds above INR 99 require a smile."})
    assert r.status_code == 422 and "no cached output" in r.json()["detail"]      # no model, no fake answer


def test_daily_budget_falls_back_to_labeled_replay(tmp_path):
    calls = []

    def model(messages, name):
        calls.append(1)
        return GOLD_RULES if messages[0]["content"].startswith("You extract") else GOLD_SC
    c = TestClient(create_api(runs_dir=tmp_path, runner=fake_runner, transport=model, daily_budget=1))
    a = c.post("/api/analyze", json={}).json()
    assert a["source"] == {"rules": "model", "scenarios": "replayed model output"}   # budget hit on call 2
    assert len(calls) == 1
    b = c.post("/api/analyze", json={}).json()                                       # budget gone for the day
    assert b["source"] == {"rules": "replayed model output", "scenarios": "replayed model output"}
    assert len(calls) == 1


def test_no_dispatch_route_and_no_target_url_parameter(api):
    paths = api.get("/openapi.json").json()["paths"]
    assert not any("dispatch" in p for p in paths)
    run_schema = json.dumps(api.get("/openapi.json").json()["components"]["schemas"]["RunBody"])
    assert "url" not in run_schema.lower() and "target" not in run_schema.lower()


def test_runner_crash_becomes_inconclusive_not_a_pass(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("secret path /etc/shadow")
    c = TestClient(create_api(runs_dir=tmp_path, runner=boom))
    a = analyze(c)
    c.post(f"/api/analyses/{a['analysis_id']}/approve", json={"test_ids": ["P-05-ADV-001"]})
    run_id = c.post("/api/runs", json={"analysis_id": a["analysis_id"]}).json()["run_id"]
    s = wait_done(c, run_id)
    assert set(s["verdicts"].values()) == {"INCONCLUSIVE"}
    assert "shadow" not in json.dumps(c.get(f"/api/runs/{run_id}/report").json())   # no internals leaked


def test_unsupported_clause_is_returned_to_the_ui_from_a_stubbed_model(tmp_path):
    """The sample policy no longer has an unsupported clause; keep that path covered with a stub."""
    rules = json.loads(GOLD_RULES)
    rules["rules"] = [r for r in rules["rules"] if r["rule_id"] != "P-04"]
    rules["unsupported_clauses"] = ["Discounts cannot exceed 30%."]
    sc = json.loads(GOLD_SC)
    sc["scenarios"] = [x for x in sc["scenarios"] if x["rule_id"] != "P-04"]

    def model(messages, name):
        return json.dumps(rules if messages[0]["content"].startswith("You extract") else sc)
    c = TestClient(create_api(runs_dir=tmp_path, runner=fake_runner, transport=model))
    a = c.post("/api/analyze", json={}).json()
    assert a["source"] == {"rules": "model", "scenarios": "model"}
    assert a["unsupported"] == ["Discounts cannot exceed 30%."]
    assert len(a["rules"]) == N_RULES - 1 and len(a["scenarios"]) == N_SCENARIOS - 2


def test_five_policy_headline_numbers(api):
    """Explicit numbers from the plan (the other tests compute them from the data)."""
    a, run_id = approved_run(api)
    assert (len(a["rules"]), len(a["scenarios"]), len(a["unsupported"])) == (5, 9, 0)
    s = wait_done(api, run_id)
    assert s["progress"] == {"done": 18, "total": 18}
    assert s["summary"] == {"cases": 18, "planted_violations_detected": 5,
                            "false_positives_on_correct_build": 0, "inconclusive": 0,
                            "cleanup_confirmed": None}
