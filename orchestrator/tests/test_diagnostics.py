import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.policy_engine import analyze  # noqa: E402

POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()


def test_fallback_reason_names_the_validation_problem():
    out = analyze(POLICY, transport=lambda m, model: "this is not json")
    assert out["source"]["rules"] == "replayed model output"
    assert "rejected twice" in out["fallback_reason"]["rules"]


def test_no_reason_when_model_output_is_accepted():
    import json
    gold_r = (ROOT / "orchestrator/golden/rules.json").read_text()
    gold_s = (ROOT / "orchestrator/golden/scenarios.json").read_text()
    out = analyze(POLICY, transport=lambda m, model: gold_r if m[0]["content"].startswith("You extract") else gold_s)
    assert out["source"] == {"rules": "model", "scenarios": "model"}
    assert out["fallback_reason"] == {"rules": None, "scenarios": None}
    json.dumps(out["fallback_reason"])


def test_replay_and_unconfigured_have_plain_reasons():
    assert analyze(POLICY, replay=True)["fallback_reason"]["rules"] == "replay requested"
