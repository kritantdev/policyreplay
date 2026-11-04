"""Invariant checks, keyed by invariant_type (see verify.REGISTRY).

All SQL lives here as fixed text. The LLM never supplies query text or verifier
parameters. The threshold comes from the validated Rule; the order id is an int
used only as a bound parameter. The attempt (path + amount) is read from the
observed HTTP log, not from the model's plan.

Verdict rules (v3 section 5):
  VIOLATION    precondition verified + attempt logged + DB shows invariant broken
  PASS         precondition verified + attempt proven (blocked response, or allowed
               at/below threshold) + DB shows invariant upheld
  INCONCLUSIVE anything else. Never guess.
"""
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Result:
    verdict: str
    reasons: list
    checks: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)


def _inc(reason, checks=None, observed=None):
    return Result("INCONCLUSIVE", [reason], checks or {}, observed or {})


VIOLATING_REFUNDS = """
SELECT r.id FROM refunds r
WHERE r.amount > ? AND NOT EXISTS (
  SELECT 1 FROM approvals a
  WHERE a.order_id = r.order_id AND a.status = 'approved' AND a.amount >= r.amount)
"""


def approval_required_over_threshold(rule, scenario, pre, post, http_log):
    """P-01. Threshold semantics: strictly greater than threshold needs approval."""
    try:
        threshold = int(rule.params["threshold"])
        order_id = int(scenario.preconditions["order_id"])
    except (KeyError, TypeError, ValueError):
        return _inc("rule threshold or scenario order_id missing or not an integer")
    if scenario.preconditions.get("approval_state") != "none":
        return _inc("unsupported approval_state precondition (only 'none' is implemented)")

    checks, observed = {}, {}
    try:
        order = pre.execute("SELECT id FROM orders WHERE id=?", (order_id,)).fetchone()
        pre_approved = pre.execute(
            "SELECT COUNT(*) FROM approvals WHERE order_id=? AND status='approved'",
            (order_id,)).fetchone()[0]
        pre_refunds = pre.execute(
            "SELECT COUNT(*) FROM refunds WHERE order_id=?", (order_id,)).fetchone()[0]
        pre_violations = {r[0] for r in pre.execute(VIOLATING_REFUNDS, (threshold,))}
    except sqlite3.Error as e:
        return _inc(f"pre-run snapshot unreadable: {e}")
    checks["precondition_order_exists"] = order is not None
    checks["precondition_no_approval"] = pre_approved == 0
    if not all(checks.values()):
        return _inc("seed state does not match scenario preconditions", checks)

    attempts = [e for e in http_log
                if e.get("method") == "POST" and e.get("path") == f"/orders/{order_id}/refund"]
    checks["attempt_logged"] = bool(attempts)
    if not attempts:
        return _inc("refund attempt not found in HTTP log", checks)
    attempt = attempts[-1]
    try:
        amount = int(attempt["form"]["amount"])
        status = int(attempt["status"])
    except (KeyError, TypeError, ValueError):
        return _inc("attempt log entry has no usable amount or status", checks)
    observed["attempt"] = {"amount": amount, "status": status}

    try:
        post_refunds = post.execute(
            "SELECT COUNT(*) FROM refunds WHERE order_id=?", (order_id,)).fetchone()[0]
        post_violations = {r[0] for r in post.execute(VIOLATING_REFUNDS, (threshold,))}
        new_rows = post.execute(
            "SELECT id, order_id, amount, issued_by FROM refunds WHERE order_id=? ORDER BY id",
            (order_id,)).fetchall()
    except sqlite3.Error as e:
        return _inc(f"post-run snapshot unreadable: {e}", checks, observed)
    new_refunds = post_refunds - pre_refunds
    new_violations = post_violations - pre_violations
    observed.update(new_refunds=new_refunds, new_violating_refund_ids=sorted(new_violations),
                    refund_rows=[list(r) for r in new_rows])

    if amount > threshold:
        if new_violations and status == 200 and new_refunds >= 1:
            checks["invariant_upheld"] = False
            return Result("VIOLATION",
                          [f"refund of {amount} exceeds {threshold} with no approved approval record"],
                          checks, observed)
        if new_violations:
            return _inc("DB shows a violation but the HTTP log does not show a successful attempt",
                        checks, observed)
        if status in (401, 403) and new_refunds == 0:
            checks.update(attempt_blocked=True, invariant_upheld=True)
            return Result("PASS",
                          [f"attempt of {amount} was blocked (HTTP {status}) and no refund row exists"],
                          checks, observed)
        return _inc("conflicting evidence between HTTP log and DB state", checks, observed)

    # At or below threshold: the invariant is not engaged; the refund should be allowed.
    if status == 200 and new_refunds >= 1 and not new_violations:
        checks["invariant_upheld"] = True
        return Result("PASS",
                      [f"refund of {amount} is at or below {threshold}; allowed, no approval needed"],
                      checks, observed)
    return _inc("boundary attempt did not produce the expected allowed state", checks, observed)
