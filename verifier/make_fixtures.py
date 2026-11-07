"""Generate evidence fixtures by driving the target app in-process (no browser, no Docker).
Later, the Playwright worker produces the same file set inside the sandbox.

  python -m verifier.make_fixtures          -> runs/fixtures/{buggy_15000,correct_15000,correct_10000}
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULE = {"rule_id": "P-01", "source_clause": "Refunds above INR 10000 require manager approval.",
        "actor": "support", "action": "issue_refund", "params": {"threshold": 10000},
        "invariant_type": "approval_required_over_threshold"}


RULE_P05 = {"rule_id": "P-05", "source_clause": "Customers cannot access another customer's order.",
            "actor": "customer", "action": "view_order", "params": {},
            "invariant_type": "owner_only_access"}
ORDER_TOTALS = {1001: 20000}


def scenario_dict(name, amount, order_id=1001):
    """The compiled scenario (steps use only the fixed action vocabulary)."""
    return {"test_id": f"P-01-{name}", "rule_id": "P-01", "actor_role": "support",
            "preconditions": {"order_id": order_id, "approval_state": "none"},
            "steps": [{"action": "login", "role": "support"},
                      {"action": "goto", "path": f"/orders/{order_id}"},
                      {"action": "expect", "testid": "order-total", "text": str(ORDER_TOTALS[order_id])},
                      {"action": "fill", "testid": "refund-amount", "value": str(amount)},
                      {"action": "click", "testid": "refund-submit"}],
            "expected_outcome": "blocked" if amount > 10000 else "allowed",
            "invariant_type": "approval_required_over_threshold"}


def scenario_dict_p05(name, order_id=1002):
    return {"test_id": f"P-05-{name}", "rule_id": "P-05", "actor_role": "customer",
            "preconditions": {"order_id": order_id},
            "steps": [{"action": "login", "role": "customer"},
                      {"action": "goto", "path": f"/orders/{order_id}"}],
            "expected_outcome": "blocked", "invariant_type": "owner_only_access"}


def snapshot(src, dst):
    Path(dst).unlink(missing_ok=True)
    s, d = sqlite3.connect(src), sqlite3.connect(dst)
    s.backup(d)
    d.close()
    s.close()


def make(name, profile, amount, outdir, order_id=1001):
    sys.path.insert(0, str(ROOT / "target_app"))
    os.environ["PR_AUTOSTART"] = "0"
    from app import create_app
    from fastapi.testclient import TestClient

    out = Path(outdir) / name
    out.mkdir(parents=True, exist_ok=True)
    live = out / "live.db"
    c = TestClient(create_app(profile, str(live)), follow_redirects=True)
    c.post("/login", data={"username": "support1"})
    snapshot(live, out / "pre.db")
    r = c.post(f"/orders/{order_id}/refund", data={"amount": amount})
    snapshot(live, out / "post.db")

    (out / "http_log.json").write_text(json.dumps([
        {"method": "POST", "path": f"/orders/{order_id}/refund",
         "form": {"amount": str(amount)}, "status": r.status_code}], indent=2))
    (out / "rule.json").write_text(json.dumps(RULE, indent=2))
    (out / "scenario.json").write_text(json.dumps(scenario_dict(name, amount, order_id), indent=2))
    return out


def make_p05(name, profile, outdir, order_id=1002, user="alice"):
    sys.path.insert(0, str(ROOT / "target_app"))
    os.environ["PR_AUTOSTART"] = "0"
    from app import create_app
    from fastapi.testclient import TestClient

    out = Path(outdir) / name
    out.mkdir(parents=True, exist_ok=True)
    live = out / "live.db"
    c = TestClient(create_app(profile, str(live)), follow_redirects=True)
    c.post("/login", data={"username": user})
    snapshot(live, out / "pre.db")
    r = c.get(f"/orders/{order_id}")
    snapshot(live, out / "post.db")
    (out / "http_log.json").write_text(json.dumps([
        {"method": "GET", "path": f"/orders/{order_id}", "status": r.status_code}], indent=2))
    (out / "rule.json").write_text(json.dumps(RULE_P05, indent=2))
    (out / "scenario.json").write_text(json.dumps(scenario_dict_p05(name, order_id), indent=2))
    return out


RULE_P02 = {"rule_id": "P-02", "source_clause": "Only administrators can modify user roles.",
            "actor": "support", "action": "modify_user_role", "params": {},
            "invariant_type": "role_change_admin_only"}
USER_OF_ROLE = {"support": "support1", "manager": "manager1", "admin": "admin1", "customer": "alice"}


def scenario_dict_p02(name, actor="support", target=1, new_role="admin"):
    return {"test_id": f"P-02-{name}", "rule_id": "P-02", "actor_role": actor,
            "preconditions": {"target_user_id": target},
            "steps": [{"action": "login", "role": actor},
                      {"action": "goto", "path": "/users"},
                      {"action": "select", "testid": f"role-select-{target}", "value": new_role},
                      {"action": "click", "testid": f"role-submit-{target}"}],
            "expected_outcome": "allowed" if actor == "admin" else "blocked",
            "invariant_type": "role_change_admin_only"}


RULE_P03 = {"rule_id": "P-03", "source_clause": "Shipped orders cannot be cancelled.",
            "actor": "customer", "action": "cancel_order", "params": {},
            "invariant_type": "no_cancel_after_ship"}
ORDER_STATUS = {1001: "delivered", 1002: "delivered", 1003: "shipped", 1004: "processing"}


def scenario_dict_p03(name, order_id=1003):
    status = ORDER_STATUS[order_id]
    return {"test_id": f"P-03-{name}", "rule_id": "P-03", "actor_role": "customer",
            "preconditions": {"order_id": order_id, "order_status": status},
            "steps": [{"action": "login", "role": "customer"},
                      {"action": "goto", "path": f"/orders/{order_id}"},
                      {"action": "click", "testid": "cancel-submit"}],
            "expected_outcome": "blocked" if status in ("shipped", "delivered") else "allowed",
            "invariant_type": "no_cancel_after_ship"}


RULE_P04 = {"rule_id": "P-04", "source_clause": "Discounts cannot exceed 30%.",
            "actor": "support", "action": "apply_discount", "params": {"max_percent": 30},
            "invariant_type": "max_discount_pct"}


def scenario_dict_p04(name, percent=40, order_id=1004, actor="support"):
    return {"test_id": f"P-04-{name}", "rule_id": "P-04", "actor_role": actor,
            "preconditions": {"order_id": order_id},
            "steps": [{"action": "login", "role": actor},
                      {"action": "goto", "path": f"/orders/{order_id}"},
                      {"action": "fill", "testid": "discount-percent", "value": str(percent)},
                      {"action": "click", "testid": "discount-submit"}],
            "expected_outcome": "blocked" if int(percent) > 30 else "allowed",
            "invariant_type": "max_discount_pct"}


def case_files(policy, name, params):
    """(rule, scenario) for a test case; shared by the local and Docker runners."""
    if policy == "P-01":
        return RULE, scenario_dict(name, params["amount"])
    if policy == "P-05":
        return RULE_P05, scenario_dict_p05(name)
    if policy == "P-02":
        return RULE_P02, scenario_dict_p02(name, **params)
    if policy == "P-03":
        return RULE_P03, scenario_dict_p03(name, **params)
    if policy == "P-04":
        return RULE_P04, scenario_dict_p04(name, **params)
    raise ValueError(f"unknown policy {policy}")


def make_p02(name, profile, outdir, actor="support", target=1, new_role="admin"):
    sys.path.insert(0, str(ROOT / "target_app"))
    os.environ["PR_AUTOSTART"] = "0"
    from app import create_app
    from fastapi.testclient import TestClient

    out = Path(outdir) / name
    out.mkdir(parents=True, exist_ok=True)
    live = out / "live.db"
    c = TestClient(create_app(profile, str(live)), follow_redirects=True)
    c.post("/login", data={"username": USER_OF_ROLE[actor]})
    snapshot(live, out / "pre.db")
    r = c.post(f"/users/{target}/role", data={"role": new_role})
    snapshot(live, out / "post.db")
    (out / "http_log.json").write_text(json.dumps([
        {"method": "POST", "path": f"/users/{target}/role", "form": {"role": new_role},
         "status": r.status_code}], indent=2))
    (out / "rule.json").write_text(json.dumps(RULE_P02, indent=2))
    (out / "scenario.json").write_text(json.dumps(
        scenario_dict_p02(name, actor, target, new_role), indent=2))
    return out


def make_p03(name, profile, outdir, order_id=1003, user="alice"):
    sys.path.insert(0, str(ROOT / "target_app"))
    os.environ["PR_AUTOSTART"] = "0"
    from app import create_app
    from fastapi.testclient import TestClient

    out = Path(outdir) / name
    out.mkdir(parents=True, exist_ok=True)
    live = out / "live.db"
    c = TestClient(create_app(profile, str(live)), follow_redirects=True)
    c.post("/login", data={"username": user})
    snapshot(live, out / "pre.db")
    r = c.post(f"/orders/{order_id}/cancel")
    snapshot(live, out / "post.db")
    (out / "http_log.json").write_text(json.dumps([
        {"method": "POST", "path": f"/orders/{order_id}/cancel", "form": {}, "status": r.status_code}], indent=2))
    (out / "rule.json").write_text(json.dumps(RULE_P03, indent=2))
    (out / "scenario.json").write_text(json.dumps(scenario_dict_p03(name, order_id), indent=2))
    return out


def make_p04(name, profile, outdir, percent=40, order_id=1004, actor="support"):
    sys.path.insert(0, str(ROOT / "target_app"))
    os.environ["PR_AUTOSTART"] = "0"
    from app import create_app
    from fastapi.testclient import TestClient

    out = Path(outdir) / name
    out.mkdir(parents=True, exist_ok=True)
    live = out / "live.db"
    c = TestClient(create_app(profile, str(live)), follow_redirects=True)
    c.post("/login", data={"username": USER_OF_ROLE[actor]})
    snapshot(live, out / "pre.db")
    r = c.post(f"/orders/{order_id}/discount", data={"percent": str(percent)})
    snapshot(live, out / "post.db")
    (out / "http_log.json").write_text(json.dumps([
        {"method": "POST", "path": f"/orders/{order_id}/discount", "form": {"percent": str(percent)},
         "status": r.status_code}], indent=2))
    (out / "rule.json").write_text(json.dumps(RULE_P04, indent=2))
    (out / "scenario.json").write_text(json.dumps(scenario_dict_p04(name, percent, order_id, actor), indent=2))
    return out


if __name__ == "__main__":
    base = ROOT / "runs" / "fixtures"
    for n, p, a in [("buggy_15000", "buggy", 15000), ("correct_15000", "correct", 15000),
                    ("correct_10000", "correct", 10000)]:
        print("created", make(n, p, a, base))
    for n, p in [("buggy_p05", "buggy"), ("correct_p05", "correct")]:
        print("created", make_p05(n, p, base))
    for n, p in [("buggy_p02", "buggy"), ("correct_p02", "correct")]:
        print("created", make_p02(n, p, base))
    for n, p, o in [("buggy_p03", "buggy", 1003), ("correct_p03", "correct", 1003),
                    ("ctl_p03", "correct", 1004)]:
        print("created", make_p03(n, p, base, o))
    for n, p, pct in [("buggy_p04", "buggy", 40), ("correct_p04", "correct", 40), ("bnd_p04", "correct", 30)]:
        print("created", make_p04(n, p, base, pct))
