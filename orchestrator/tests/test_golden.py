"""The cached (golden) model output must agree with policies/sample_policy.txt, word for word, and the
five-policy system must have exactly the shape the plan promises."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.policy_engine import _norm, analyze  # noqa: E402

POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()
RULES = json.loads((ROOT / "orchestrator/golden/rules.json").read_text())
SCENARIOS = json.loads((ROOT / "orchestrator/golden/scenarios.json").read_text())["scenarios"]

# Section 1 of the execution plan: scenario -> (expected outcome, invariant)
EXPECTED = {
    "P-01-ADV-001": "blocked", "P-01-BND-001": "allowed", "P-05-ADV-001": "blocked",
    "P-02-ADV-001": "blocked", "P-02-CTL-001": "allowed",
    "P-03-ADV-001": "blocked", "P-03-CTL-001": "allowed",
    "P-04-ADV-001": "blocked", "P-04-BND-001": "allowed",
}


def test_every_cached_rule_quotes_the_policy_verbatim():
    for r in RULES["rules"]:
        assert _norm(r["source_clause"]) in _norm(POLICY), r["rule_id"]


def test_every_policy_clause_has_exactly_one_rule_and_nothing_is_unsupported():
    ids_in_policy = re.findall(r"\((P-\d+)\)", POLICY)
    assert sorted(ids_in_policy) == ["P-01", "P-02", "P-03", "P-04", "P-05"]
    assert sorted(r["rule_id"] for r in RULES["rules"]) == sorted(ids_in_policy)
    assert RULES["unsupported_clauses"] == []


def test_each_rule_clause_sits_on_its_own_policy_line():
    lines = {re.search(r"\((P-\d+)\)", l).group(1): l for l in POLICY.splitlines() if re.search(r"\(P-\d+\)", l)}
    for r in RULES["rules"]:
        assert _norm(r["source_clause"]) in _norm(lines[r["rule_id"]]), r["rule_id"]


def test_scenario_set_matches_the_plan_matrix():
    assert {s["test_id"]: s["expected_outcome"] for s in SCENARIOS} == EXPECTED
    assert len(SCENARIOS) == 9 and sum(v == "blocked" for v in EXPECTED.values()) == 5


def test_five_of_five_policies_have_an_adversarial_scenario_and_pass_validation():
    out = analyze(POLICY, replay=True)
    assert len(out["rules"]) == 5 and out["unsupported"] == [] and len(out["scenarios"]) == 9
    adv = {s.rule_id for s in out["scenarios"] if s.expected_outcome == "blocked"}
    assert adv == {"P-01", "P-02", "P-03", "P-04", "P-05"}
    assert 2 * len(out["scenarios"]) == 18                                  # buggy + correct build per scenario


def test_policy_text_was_not_edited():
    assert "Shipped orders cannot be cancelled." in POLICY and "Discounts cannot exceed 30%." in POLICY
