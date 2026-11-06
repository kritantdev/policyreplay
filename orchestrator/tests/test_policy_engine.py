import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.llm import LLMError  # noqa: E402
from orchestrator.policy_engine import (MODEL, REPLAYED, analyze, extract_rules,  # noqa: E402
                                        generate_scenarios)
from verifier.schemas import Rule  # noqa: E402

POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()
GOOD_RULES = json.loads((ROOT / "orchestrator/golden/rules.json").read_text())
GOOD_SC = json.loads((ROOT / "orchestrator/golden/scenarios.json").read_text())


def fake(*replies):
    """Transport returning canned replies in order; records how many calls were made."""
    calls = []

    def t(messages, model):
        calls.append(messages)
        return replies[min(len(calls), len(replies)) - 1]
    t.calls = calls
    return t


def J(o):
    return json.dumps(o)


def test_good_model_output_is_accepted():
    t = fake(J(GOOD_RULES))
    r = extract_rules(POLICY, t)
    assert r["source"] == MODEL and len(r["rules"]) == 3 and len(r["unsupported"]) == 2


def test_markdown_fences_are_stripped():
    r = extract_rules(POLICY, fake("```json\n" + J(GOOD_RULES) + "\n```"))
    assert r["source"] == MODEL


def test_invalid_then_valid_retries_once():
    t = fake("not json at all", J(GOOD_RULES))
    assert extract_rules(POLICY, t)["source"] == MODEL and len(t.calls) == 2


def test_invalid_twice_falls_back_to_labeled_replay():
    t = fake("nope", "still nope")
    r = extract_rules(POLICY, t)
    assert r["source"] == REPLAYED and len(t.calls) == 2


def test_hallucinated_threshold_is_rejected():
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][0]["params"]["threshold"] = 50000
    t = fake(J(bad), J(bad))
    assert extract_rules(POLICY, t)["source"] == REPLAYED     # never accepted from the model


def test_invented_clause_is_rejected():
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][0]["source_clause"] = "Refunds above INR 5,000 require director approval."
    assert extract_rules(POLICY, fake(J(bad), J(bad)))["source"] == REPLAYED


def test_unknown_invariant_type_is_rejected():
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][0]["invariant_type"] = "drop_all_tables"
    assert extract_rules(POLICY, fake(J(bad), J(bad)))["source"] == REPLAYED


def test_replay_refuses_policy_it_was_not_recorded_for():
    with pytest.raises(LLMError):
        extract_rules("Refunds above INR 99 require a smile.", fake("x", "y"))


def _rules():
    return [Rule.model_validate(r) for r in GOOD_RULES["rules"]]


def test_good_scenarios_accepted_and_outcome_recomputed():
    s = json.loads(J(GOOD_SC))
    s["scenarios"][0]["expected_outcome"] = "allowed"          # model lies; code recomputes
    out = generate_scenarios(_rules(), fake(J(s)))
    assert out["source"] == MODEL
    assert {x.test_id: x.expected_outcome for x in out["scenarios"]}["P-01-ADV-001"] == "blocked"
    assert {x.test_id: x.expected_outcome for x in out["scenarios"]}["P-01-BND-001"] == "allowed"


@pytest.mark.parametrize("mutate", [
    lambda s: s["steps"].append({"action": "shell", "cmd": "id"}),                    # outside vocabulary
    lambda s: s["steps"].__setitem__(1, {"action": "goto", "path": "http://evil/"}),  # leaves origin
    lambda s: s["steps"].pop(),                                                       # never clicks submit
    lambda s: s["preconditions"].__setitem__("order_id", 4242),                       # unknown order
    lambda s: s["steps"][2].__setitem__("value", "999999"),                           # amount > total
])
def test_bad_p01_scenarios_rejected_then_replayed(mutate):
    s = json.loads(J(GOOD_SC))
    mutate(s["scenarios"][0])
    out = generate_scenarios(_rules(), fake(J(s), J(s)))
    assert out["source"] == REPLAYED


def test_p05_scenario_must_target_someone_elses_order():
    s = json.loads(J(GOOD_SC))
    p05 = s["scenarios"][2]
    p05["preconditions"]["order_id"] = 1001
    p05["steps"][1]["path"] = "/orders/1001"                   # customer's OWN order: not adversarial
    assert generate_scenarios(_rules(), fake(J(s), J(s)))["source"] == REPLAYED


def test_every_rule_needs_a_scenario():
    s = json.loads(J(GOOD_SC))
    s["scenarios"] = s["scenarios"][:2]                        # P-05 missing
    assert generate_scenarios(_rules(), fake(J(s), J(s)))["source"] == REPLAYED


def test_end_to_end_replay_without_any_key():
    out = analyze(POLICY, replay=True)
    assert out["source"] == {"rules": REPLAYED, "scenarios": REPLAYED}
    assert [s.test_id for s in out["scenarios"]] == ["P-01-ADV-001", "P-01-BND-001", "P-05-ADV-001", "P-02-ADV-001", "P-02-CTL-001"]


def _p02(s):
    return s["scenarios"][3]


@pytest.mark.parametrize("mutate", [
    lambda s: s["preconditions"].__setitem__("target_user_id", 99),               # unknown user
    lambda s: s.__setitem__("actor_role", "customer"),                            # cannot open /users
    lambda s: s["steps"][2].__setitem__("value", "superuser"),                    # not a role
    lambda s: s["steps"][2].__setitem__("testid", "role-select-2"),               # wrong target row
    lambda s: s["steps"].pop(),                                                   # never clicks submit
    lambda s: s["preconditions"].__setitem__("order_id", 1001),                   # extra precondition
])
def test_bad_p02_scenarios_rejected_then_replayed(mutate):
    s = json.loads(J(GOOD_SC))
    mutate(_p02(s))
    assert generate_scenarios(_rules(), fake(J(s), J(s)))["source"] == REPLAYED


def test_p02_expected_outcome_is_recomputed():
    s = json.loads(J(GOOD_SC))
    _p02(s)["expected_outcome"] = "allowed"                                       # model lies
    out = generate_scenarios(_rules(), fake(J(s)))
    assert out["source"] == MODEL
    by = {x.test_id: x.expected_outcome for x in out["scenarios"]}
    assert by["P-02-ADV-001"] == "blocked" and by["P-02-CTL-001"] == "allowed"
