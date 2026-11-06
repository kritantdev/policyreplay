"""LLM jobs 1-3: extract rules, generate adversarial scenarios, compile step plans.

The model only PROPOSES. Everything it returns is validated, and anything that feeds the verifier
is re-derived or checked deterministically:
  * invariant_type must be in the fixed list (verifier.schemas.InvariantType)
  * source_clause must appear verbatim in the policy text; the threshold must appear in the clause
  * steps must pass worker.run_plan.validate_plan (fixed action vocabulary)
  * expected_outcome is recomputed from the rule, never trusted from the model
On invalid output: one retry, then a cached golden output labeled "replayed model output".

  python orchestrator/policy_engine.py --policy policies/sample_policy.txt [--replay]
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.llm import LLMError, chat_json, configured  # noqa: E402
from verifier.schemas import Rule, Scenario  # noqa: E402
from worker.run_plan import PlanError, validate_plan  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden"
MODEL, REPLAYED = "model", "replayed model output"

# Target-app profile (what a tester would get from API docs). Seeded state is fixed.
ORDER_TOTAL = {1001: 20000, 1002: 8000}
CUSTOMER_OWN_ORDER = 1001   # the 'customer' login (alice) owns 1001; bob owns 1002
USER_IDS = {1, 2, 3, 4, 5}  # 1 support1, 2 manager1, 3 admin1, 4 alice, 5 bob
ROLES = {"support", "manager", "admin", "customer"}

RULES_PROMPT = """You extract testable rules from a business policy.
Return ONLY JSON: {"rules":[{"rule_id":str,"source_clause":str,"actor":str,"action":str,
"params":object,"invariant_type":str}],"unsupported_clauses":[str]}
invariant_type must be exactly one of:
- "approval_required_over_threshold": params {"threshold": <integer INR>}
- "owner_only_access": params {}
- "role_change_admin_only": params {}
Use the policy's own ids (e.g. P-01) as rule_id. source_clause must be copied VERBATIM from the policy.
Clauses that fit neither type go in unsupported_clauses (copied verbatim). Never invent clauses or numbers."""

SCENARIO_PROMPT = """You design adversarial browser tests for ONE e-commerce app, from the given rules.
Return ONLY JSON: {"scenarios":[{"test_id":str,"rule_id":str,"actor_role":str,"preconditions":object,
"steps":[...],"expected_outcome":"blocked"|"allowed","invariant_type":str}]}
App: roles for login are support, manager, admin, customer. Customer 'customer' owns order 1001
(total 20000); order 1002 belongs to a different customer (total 8000). No approvals exist at start.
Step vocabulary (nothing else is allowed): {"action":"login","role":R} {"action":"goto","path":"/orders/1001"}
{"action":"fill","testid":T,"value":V} {"action":"click","testid":T} {"action":"expect","testid":T,"text":S}
Testids on /orders/<id>: refund-amount, refund-submit, approval-amount, approval-request-submit.
approval_required_over_threshold scenarios: preconditions {"order_id":1001,"approval_state":"none"}; steps =
login support, goto /orders/1001, fill refund-amount (integer string 1..20000), click refund-submit.
Include one adversarial amount above the threshold and one at exactly the threshold (boundary).
owner_only_access scenarios: preconditions {"order_id":1002}; steps = login customer, goto /orders/1002.
Users page /users (staff only) has, per user id N: testids role-select-N and role-submit-N.
role_change_admin_only scenarios: preconditions {"target_user_id":N}; steps = login R (support, manager or admin),
goto /users, {"action":"select","testid":"role-select-N","value":<role>}, click role-submit-N.
Include one adversarial self-promotion (support, target 1, value admin) and one positive control
(login admin, target 4, value manager). Roles: support, manager, admin, customer.
Use test_ids like P-01-ADV-001, P-01-BND-001. Give 1-3 scenarios per rule."""


def _norm(s):
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def check_rule(rule, policy_text):
    if _norm(rule.source_clause) not in _norm(policy_text):
        raise ValueError(f"{rule.rule_id}: source_clause is not verbatim in the policy")
    if rule.invariant_type == "approval_required_over_threshold":
        th = rule.params.get("threshold")
        if set(rule.params) != {"threshold"} or not isinstance(th, int) or isinstance(th, bool) or th <= 0:
            raise ValueError(f"{rule.rule_id}: params must be exactly {{'threshold': positive int}}")
        nums = {int(n) for n in re.findall(r"\d+", re.sub(r"(?<=\d),(?=\d)", "", rule.source_clause))}
        if th not in nums:
            raise ValueError(f"{rule.rule_id}: threshold {th} does not appear in the source clause")
    elif rule.params:
        raise ValueError(f"{rule.rule_id}: owner_only_access takes no params")


def _parse_rules(obj, policy_text):
    if not isinstance(obj, dict) or not isinstance(obj.get("rules"), list):
        raise ValueError("expected an object with a 'rules' list")
    rules = [Rule.model_validate(r) for r in obj["rules"]]
    if not rules:
        raise ValueError("no rules returned")
    if len({r.rule_id for r in rules}) != len(rules):
        raise ValueError("duplicate rule_id")
    for r in rules:
        check_rule(r, policy_text)
    uns = obj.get("unsupported_clauses", [])
    if not isinstance(uns, list) or not all(isinstance(u, str) for u in uns):
        raise ValueError("unsupported_clauses must be a list of strings")
    return {"rules": rules, "unsupported": uns}


def _int(v, label):
    if isinstance(v, bool) or not isinstance(v, (int, str)) or (isinstance(v, str) and not v.isdigit()):
        raise ValueError(f"{label} must be an integer")
    return int(v)


def check_scenario(sc, rule):
    """Deterministic relevance checks; returns the scenario with expected_outcome recomputed."""
    validate_plan(sc.steps)
    if sc.rule_id != rule.rule_id or sc.invariant_type != rule.invariant_type:
        raise ValueError(f"{sc.test_id}: rule_id/invariant_type does not match its rule")
    steps, pre, t = sc.steps, sc.preconditions, rule.invariant_type
    if steps[0].get("action") != "login" or steps[0].get("role") != sc.actor_role:
        raise ValueError(f"{sc.test_id}: first step must log in as actor_role")

    def opens(path):
        return any(x["action"] == "goto" and x["path"] == path for x in steps)

    if t in ("approval_required_over_threshold", "owner_only_access"):
        order_id = _int(pre.get("order_id"), f"{sc.test_id}: order_id")
        if order_id not in ORDER_TOTAL:
            raise ValueError(f"{sc.test_id}: unknown order {order_id}")
        if not opens(f"/orders/{order_id}"):
            raise ValueError(f"{sc.test_id}: plan never opens /orders/{order_id}")
    if t == "approval_required_over_threshold":
        if pre.get("approval_state") != "none" or set(pre) != {"order_id", "approval_state"}:
            raise ValueError(f"{sc.test_id}: preconditions must be order_id + approval_state 'none'")
        fills = [x for x in steps if x["action"] == "fill" and x["testid"] == "refund-amount"]
        clicks = [i for i, x in enumerate(steps) if x["action"] == "click" and x["testid"] == "refund-submit"]
        if not fills or not clicks or not fills[-1]["value"].isdigit():
            raise ValueError(f"{sc.test_id}: plan must fill refund-amount (digits) and click refund-submit")
        amount = int(fills[-1]["value"])
        if not 0 < amount <= ORDER_TOTAL[order_id]:
            raise ValueError(f"{sc.test_id}: refund amount outside 1..{ORDER_TOTAL[order_id]}")
        outcome = "blocked" if amount > rule.params["threshold"] else "allowed"
    elif t == "owner_only_access":
        if set(pre) != {"order_id"} or order_id == CUSTOMER_OWN_ORDER or sc.actor_role != "customer":
            raise ValueError(f"{sc.test_id}: must be customer opening an order they do not own")
        outcome = "blocked"
    elif t == "role_change_admin_only":
        if set(pre) != {"target_user_id"}:
            raise ValueError(f"{sc.test_id}: preconditions must be exactly target_user_id")
        tid = _int(pre["target_user_id"], f"{sc.test_id}: target_user_id")
        if tid not in USER_IDS:
            raise ValueError(f"{sc.test_id}: unknown user {tid}")
        if sc.actor_role not in {"support", "manager", "admin"}:
            raise ValueError(f"{sc.test_id}: actor must be a staff role that can open /users")
        if not opens("/users"):
            raise ValueError(f"{sc.test_id}: plan never opens /users")
        sel = [i for i, x in enumerate(steps) if x["action"] == "select"
               and x["testid"] == f"role-select-{tid}" and x["value"] in ROLES]
        clk = [i for i, x in enumerate(steps) if x["action"] == "click" and x["testid"] == f"role-submit-{tid}"]
        if not sel or not clk or max(clk) < min(sel):
            raise ValueError(f"{sc.test_id}: plan must select a valid role then click role-submit-{tid}")
        outcome = "allowed" if sc.actor_role == "admin" else "blocked"
    else:
        raise ValueError(f"no scenario checks for {t}")
    return sc.model_copy(update={"expected_outcome": outcome})


def _parse_scenarios(obj, rules):
    if not isinstance(obj, dict) or not isinstance(obj.get("scenarios"), list):
        raise ValueError("expected an object with a 'scenarios' list")
    by_id = {r.rule_id: r for r in rules}
    out = []
    for raw in obj["scenarios"][:12]:
        sc = Scenario.model_validate(raw)
        if sc.rule_id not in by_id:
            raise ValueError(f"{sc.test_id}: unknown rule_id {sc.rule_id}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", sc.test_id):
            raise ValueError(f"test_id {sc.test_id!r} must match [A-Za-z0-9_-]{{1,40}}")
        out.append(check_scenario(sc, by_id[sc.rule_id]))
    if len({s.test_id for s in out}) != len(out):
        raise ValueError("duplicate test_id")
    missing = set(by_id) - {s.rule_id for s in out}
    if missing:
        raise ValueError(f"no scenario for rules: {sorted(missing)}")
    return out


def _golden(name):
    return json.loads((GOLDEN / name).read_text())


def extract_rules(policy_text, transport=None, replay=False):
    reason = "replay requested" if replay else "model not configured"
    msgs = [{"role": "system", "content": RULES_PROMPT}, {"role": "user", "content": policy_text}]
    if not replay and (transport or configured()):
        try:
            res = chat_json(msgs, lambda o: _parse_rules(o, policy_text), transport)
            return {**res, "source": MODEL, "fallback_reason": None}
        except LLMError as e:
            reason = str(e)
    try:
        res = _parse_rules(_golden("rules.json"), policy_text)
    except ValueError:
        raise LLMError("model unavailable and no cached output matches this policy text") from None
    return {**res, "source": REPLAYED, "fallback_reason": reason}


def generate_scenarios(rules, transport=None, replay=False):
    reason = "replay requested" if replay else "model not configured"
    payload = json.dumps([r.model_dump() for r in rules])
    msgs = [{"role": "system", "content": SCENARIO_PROMPT}, {"role": "user", "content": payload}]
    if not replay and (transport or configured()):
        try:
            return {"scenarios": chat_json(msgs, lambda o: _parse_scenarios(o, rules), transport),
                    "source": MODEL, "fallback_reason": None}
        except LLMError as e:
            reason = str(e)
    try:
        return {"scenarios": _parse_scenarios(_golden("scenarios.json"), rules), "source": REPLAYED,
                "fallback_reason": reason}
    except ValueError:
        raise LLMError("model unavailable and no cached scenarios match these rules") from None


def analyze(policy_text, transport=None, replay=False):
    r = extract_rules(policy_text, transport, replay)
    s = generate_scenarios(r["rules"], transport, replay)
    return {"rules": r["rules"], "unsupported": r["unsupported"], "scenarios": s["scenarios"],
            "source": {"rules": r["source"], "scenarios": s["source"]},
            "fallback_reason": {"rules": r["fallback_reason"], "scenarios": s["fallback_reason"]}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", required=True)
    ap.add_argument("--replay", action="store_true")
    a = ap.parse_args()
    out = analyze(Path(a.policy).read_text(), replay=a.replay)
    print("source:", out["source"])
    for k, v in out["fallback_reason"].items():
        if v:
            print(f"  why {k} fell back to replay: {v}")
    for r in out["rules"]:
        print(f"RULE {r.rule_id} [{r.invariant_type}] {r.params}  <- \"{r.source_clause}\"")
    for u in out["unsupported"]:
        print(f"UNSUPPORTED (no verifier yet, not tested): {u}")
    for s in out["scenarios"]:
        print(f"SCENARIO {s.test_id} ({s.rule_id}) expect={s.expected_outcome} steps={len(s.steps)}")
