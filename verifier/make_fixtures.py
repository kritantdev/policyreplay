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


if __name__ == "__main__":
    base = ROOT / "runs" / "fixtures"
    for n, p, a in [("buggy_15000", "buggy", 15000), ("correct_15000", "correct", 15000),
                    ("correct_10000", "correct", 10000)]:
        print("created", make(n, p, a, base))
    for n, p in [("buggy_p05", "buggy"), ("correct_p05", "correct")]:
        print("created", make_p05(n, p, base))
