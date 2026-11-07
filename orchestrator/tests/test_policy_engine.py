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
    assert r["source"] == MODEL and len(r["rules"]) == 5 and r["unsupported"] == []


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
    assert [s.test_id for s in out["scenarios"]] == ["P-01-ADV-001", "P-01-BND-001", "P-05-ADV-001", "P-02-ADV-001", "P-02-CTL-001",
                                                       "P-03-ADV-001", "P-03-CTL-001",
                                                       "P-04-ADV-001", "P-04-BND-001"]


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


def _p03(s, i=5):
    return s["scenarios"][i]            # 5 = ADV (order 1003, shipped), 6 = CTL (order 1004, processing)


@pytest.mark.parametrize("mutate", [
    lambda s: s["preconditions"].__setitem__("order_id", 1001),                  # not a cancellation test order
    lambda s: s["preconditions"].__setitem__("order_status", "processing"),     # contradicts the seed (1003 is shipped)
    lambda s: s["preconditions"].pop("order_status"),                           # preconditions incomplete
    lambda s: s["preconditions"].__setitem__("extra", 1),                       # extra precondition
    lambda s: s.__setitem__("actor_role", "support"),                           # customer scenario only
    lambda s: s["steps"].pop(),                                                 # never clicks cancel-submit
    lambda s: s["steps"].__setitem__(2, {"action": "click", "testid": "refund-submit"}),   # wrong control
    lambda s: s["steps"].__setitem__(1, {"action": "goto", "path": "/orders/1004"}),       # opens a different order
    lambda s: s["steps"].insert(1, {"action": "click", "testid": "cancel-submit"}),        # cancels before opening the order
])
def test_bad_p03_scenarios_rejected_then_replayed(mutate):
    s = json.loads(J(GOOD_SC))
    mutate(_p03(s))
    assert generate_scenarios(_rules(), fake(J(s), J(s)))["source"] == REPLAYED


def test_p03_expected_outcome_is_recomputed():
    s = json.loads(J(GOOD_SC))
    _p03(s)["expected_outcome"] = "allowed"                                      # model lies about the shipped order
    _p03(s, 6)["expected_outcome"] = "blocked"                                    # and about the control
    out = generate_scenarios(_rules(), fake(J(s)))
    assert out["source"] == MODEL
    by = {x.test_id: x.expected_outcome for x in out["scenarios"]}
    assert by["P-03-ADV-001"] == "blocked" and by["P-03-CTL-001"] == "allowed"


def test_p03_rule_must_take_no_params_and_quote_the_policy():
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][3]["params"] = {"days": 3}
    assert extract_rules(POLICY, fake(J(bad), J(bad)))["source"] == REPLAYED
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][3]["source_clause"] = "Delivered orders cannot be cancelled."
    assert extract_rules(POLICY, fake(J(bad), J(bad)))["source"] == REPLAYED


def _p04(s, i=7):
    return s["scenarios"][i]            # 7 = ADV (support applies 40), 8 = BND (exactly 30)


@pytest.mark.parametrize("mutate", [
    lambda s: s["preconditions"].__setitem__("order_id", 4242),                  # unknown order
    lambda s: s["preconditions"].__setitem__("percent", 40),                     # extra precondition
    lambda s: s.__setitem__("actor_role", "customer"),                           # customers cannot discount
    lambda s: s["steps"].pop(),                                                  # never clicks submit
    lambda s: s["steps"].__setitem__(2, {"action": "fill", "testid": "discount-percent", "value": "forty"}),
    lambda s: s["steps"].__setitem__(2, {"action": "fill", "testid": "discount-percent", "value": "140"}),
    lambda s: s["steps"].__setitem__(2, {"action": "fill", "testid": "refund-amount", "value": "40"}),
    lambda s: s["steps"].__setitem__(3, {"action": "click", "testid": "cancel-submit"}),
    lambda s: s["steps"].__setitem__(1, {"action": "goto", "path": "/orders/1003"}),         # opens another order
    lambda s: s["steps"].insert(1, {"action": "click", "testid": "discount-submit"}),        # submits before opening
])
def test_bad_p04_scenarios_rejected_then_replayed(mutate):
    s = json.loads(J(GOOD_SC))
    mutate(_p04(s))
    assert generate_scenarios(_rules(), fake(J(s), J(s)))["source"] == REPLAYED


def test_p04_expected_outcome_is_recomputed_from_the_rule_threshold():
    s = json.loads(J(GOOD_SC))
    _p04(s)["expected_outcome"] = "allowed"                                       # model lies about 40
    _p04(s, 8)["expected_outcome"] = "blocked"                                     # and about the boundary
    out = generate_scenarios(_rules(), fake(J(s)))
    assert out["source"] == MODEL
    by = {x.test_id: x.expected_outcome for x in out["scenarios"]}
    assert by["P-04-ADV-001"] == "blocked" and by["P-04-BND-001"] == "allowed"


def test_p04_boundary_31_is_blocked_and_30_is_allowed():
    s = json.loads(J(GOOD_SC))
    _p04(s, 8)["steps"][2]["value"] = "31"
    out = generate_scenarios(_rules(), fake(J(s)))
    assert {x.test_id: x.expected_outcome for x in out["scenarios"]}["P-04-BND-001"] == "blocked"


@pytest.mark.parametrize("params", [{}, {"max_percent": 50}, {"max_percent": 0}, {"max_percent": 130},
                                    {"max_percent": "30"}, {"max_percent": 30, "x": 1}, {"threshold": 30}])
def test_p04_rule_params_must_be_exactly_the_number_in_the_clause(params):
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][4]["params"] = params
    assert extract_rules(POLICY, fake(J(bad), J(bad)))["source"] == REPLAYED


def test_p04_rule_must_quote_the_policy():
    bad = json.loads(J(GOOD_RULES))
    bad["rules"][4]["source_clause"] = "Discounts cannot exceed 50%."
    assert extract_rules(POLICY, fake(J(bad), J(bad)))["source"] == REPLAYED


def _without_p04():
    rules = json.loads(J(GOOD_RULES))
    rules["rules"] = [r for r in rules["rules"] if r["rule_id"] != "P-04"]
    rules["unsupported_clauses"] = ["Discounts cannot exceed 30%."]
    sc = json.loads(J(GOOD_SC))
    sc["scenarios"] = [x for x in sc["scenarios"] if x["rule_id"] != "P-04"]
    return rules, sc


def test_unsupported_clause_from_a_model_reply_is_still_reported():
    """The sample policy has no unsupported clause any more, so cover that path with a stubbed reply."""
    rules, sc = _without_p04()
    out = analyze(POLICY, transport=fake(J(rules), J(sc)))
    assert out["source"] == {"rules": MODEL, "scenarios": MODEL}
    assert out["unsupported"] == ["Discounts cannot exceed 30%."]
    assert [r.rule_id for r in out["rules"]] == ["P-01", "P-05", "P-02", "P-03"]
    assert not any(x.rule_id == "P-04" for x in out["scenarios"])


def test_sample_policy_has_no_unsupported_clause_in_replay():
    out = analyze(POLICY, replay=True)
    assert out["unsupported"] == [] and len(out["rules"]) == 5 and len(out["scenarios"]) == 9
    assert sum(x.expected_outcome == "blocked" for x in out["scenarios"]) == 5
