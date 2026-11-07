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


FOREIGN_VIEWS = """
SELECT v.id, v.user_id, v.order_id, v.status FROM order_views v
JOIN users u ON u.id = v.user_id
JOIN orders o ON o.id = v.order_id
WHERE u.role = 'customer' AND o.customer_id != v.user_id AND v.status = 200
"""


def owner_only_access(rule, scenario, pre, post, http_log):
    """P-05. A customer must never be served another customer's order.

    Evidence is two independent sources that must agree: the browser-observed HTTP
    status for GET /orders/<id>, and the app's order_views audit table (fixed SQL).
    Limitation: the audit table is maintained by the app under test.
    """
    try:
        order_id = int(scenario.preconditions["order_id"])
    except (KeyError, TypeError, ValueError):
        return _inc("scenario order_id missing or not an integer")

    checks, observed = {}, {}
    try:
        owner = pre.execute("SELECT u.id, u.role FROM orders o JOIN users u ON u.id=o.customer_id "
                            "WHERE o.id=?", (order_id,)).fetchone()
        customers = pre.execute("SELECT COUNT(*) FROM users WHERE role='customer'").fetchone()[0]
        pre_ids = {r[0] for r in pre.execute(FOREIGN_VIEWS)}
    except sqlite3.Error as e:
        return _inc(f"pre-run snapshot unreadable: {e}")
    checks["precondition_order_exists"] = owner is not None
    checks["precondition_owner_is_customer"] = bool(owner) and owner[1] == "customer"
    checks["precondition_other_customer_exists"] = customers >= 2
    if not all(checks.values()):
        return _inc("seed state does not match scenario preconditions", checks)

    attempts = [e for e in http_log
                if e.get("method") == "GET" and e.get("path") == f"/orders/{order_id}"]
    checks["attempt_logged"] = bool(attempts)
    if not attempts:
        return _inc("order access attempt not found in HTTP log", checks)
    try:
        status = int(attempts[-1]["status"])
    except (KeyError, TypeError, ValueError):
        return _inc("attempt log entry has no usable status", checks)
    observed["attempt"] = {"path": f"/orders/{order_id}", "status": status}

    try:
        post_rows = post.execute(FOREIGN_VIEWS).fetchall()
    except sqlite3.Error as e:
        return _inc(f"post-run snapshot unreadable: {e}", checks, observed)
    new = [list(r) for r in post_rows if r[0] not in pre_ids and r[2] == order_id]
    observed["new_foreign_views"] = new

    if new and status == 200:
        checks["invariant_upheld"] = False
        return Result("VIOLATION", [f"a customer who does not own order {order_id} was served it (HTTP 200) "
                                    f"and the audit log recorded the access"], checks, observed)
    if new:
        return _inc("audit log shows foreign access but the HTTP log does not show a 200", checks, observed)
    if status in (401, 403, 404):
        checks.update(attempt_blocked=True, invariant_upheld=True)
        return Result("PASS", [f"access to order {order_id} was blocked (HTTP {status}) and no foreign "
                               f"view was served"], checks, observed)
    return _inc("page was served but no foreign-customer access is recorded; the scenario did not "
                "exercise a non-owner customer", checks, observed)


VALID_ROLES = {"support", "manager", "admin", "customer"}


def role_change_admin_only(rule, scenario, pre, post, http_log):
    """P-02. Only administrators may change user roles.

    Evidence (all must agree): the browser-observed HTTP status, the users table before vs after,
    and the role_changes audit row, which records the actor's role at the time of the attempt.
    Limitation: the audit table is maintained by the app under test.
    """
    try:
        target = int(scenario.preconditions["target_user_id"])
    except (KeyError, TypeError, ValueError):
        return _inc("scenario target_user_id missing or not an integer")
    actor = scenario.actor_role
    if actor not in VALID_ROLES:
        return _inc("scenario actor_role is not a known role")

    checks, observed = {}, {}
    try:
        pre_row = pre.execute("SELECT role FROM users WHERE id=?", (target,)).fetchone()
        actors = pre.execute("SELECT COUNT(*) FROM users WHERE role=?", (actor,)).fetchone()[0]
        pre_ids = {r[0] for r in pre.execute(
            "SELECT id FROM role_changes WHERE target_user_id=?", (target,))}
    except sqlite3.Error as e:
        return _inc(f"pre-run snapshot unreadable: {e}")
    checks["precondition_target_exists"] = pre_row is not None
    checks["precondition_actor_role_exists"] = actors >= 1
    if not all(checks.values()):
        return _inc("seed state does not match scenario preconditions", checks)

    attempts = [e for e in http_log
                if e.get("method") == "POST" and e.get("path") == f"/users/{target}/role"]
    checks["attempt_logged"] = bool(attempts)
    if not attempts:
        return _inc("role-change attempt not found in HTTP log", checks)
    try:
        new_role = attempts[-1]["form"]["role"]
        status = int(attempts[-1]["status"])
    except (KeyError, TypeError, ValueError):
        return _inc("attempt log entry has no usable role or status", checks)
    if not isinstance(new_role, str) or new_role not in VALID_ROLES:
        return _inc("attempted role is not a known role", checks)
    pre_role = pre_row[0]
    checks["precondition_role_differs"] = pre_role != new_role
    observed["attempt"] = {"target_user_id": target, "new_role": new_role, "status": status}
    if pre_role == new_role:
        return _inc("attempted role equals the current role; no change is observable", checks, observed)

    try:
        post_row = post.execute("SELECT role FROM users WHERE id=?", (target,)).fetchone()
        post_audit = post.execute("SELECT id, actor_role, applied, new_role FROM role_changes "
                                  "WHERE target_user_id=?", (target,)).fetchall()
    except sqlite3.Error as e:
        return _inc(f"post-run snapshot unreadable: {e}", checks, observed)
    if post_row is None:
        return _inc("target user missing from post-run snapshot", checks, observed)
    post_role = post_row[0]
    new_audit = [list(r) for r in post_audit if r[0] not in pre_ids]
    applied = [a for a in new_audit if a[2] == 1 and a[1] == actor and a[3] == new_role]
    observed.update(role_before=pre_role, role_after=post_role, new_audit_rows=new_audit)
    changed = post_role != pre_role

    if actor == "admin":
        if status == 200 and post_role == new_role and applied:
            checks["invariant_upheld"] = True
            return Result("PASS", [f"an administrator changed the role to {new_role}; permitted"],
                          checks, observed)
        return _inc("administrator change was not observed end to end", checks, observed)

    if status == 200 and post_role == new_role and applied:
        checks["invariant_upheld"] = False
        return Result("VIOLATION", [f"a {actor} changed user {target} from {pre_role} to {new_role}; "
                                    f"only administrators may modify roles"], checks, observed)
    if status in (401, 403) and not changed and not any(a[2] == 1 for a in new_audit):
        checks.update(attempt_blocked=True, invariant_upheld=True)
        return Result("PASS", [f"role change by a {actor} was blocked (HTTP {status}); "
                               f"role unchanged and no change applied"], checks, observed)
    return _inc("conflicting evidence between HTTP log, users table and audit log", checks, observed)


NON_CANCELLABLE = {"shipped", "delivered"}
CANCELLABLE = {"placed", "processing"}


def no_cancel_after_ship(rule, scenario, pre, post, http_log):
    """P-03. An order that is shipped or delivered cannot be cancelled; placed or processing can.

    Evidence (all must agree): the browser-observed HTTP status for POST /orders/<id>/cancel, the
    order's status before vs after, and the order_status_changes audit row, which records the
    status AT THE TIME of the attempt and whether the change was applied.
    Limitation: the audit table is maintained by the app under test.
    """
    try:
        order_id = int(scenario.preconditions["order_id"])
        declared = scenario.preconditions["order_status"]
    except (KeyError, TypeError, ValueError):
        return _inc("scenario order_id or order_status missing or invalid")
    if not isinstance(declared, str) or declared not in NON_CANCELLABLE | CANCELLABLE:
        return _inc("scenario order_status is not a known status")

    checks, observed = {}, {}
    try:
        pre_row = pre.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        pre_ids = {r[0] for r in pre.execute(
            "SELECT id FROM order_status_changes WHERE order_id=?", (order_id,))}
    except sqlite3.Error as e:
        return _inc(f"pre-run snapshot unreadable: {e}")
    checks["precondition_order_exists"] = pre_row is not None
    checks["precondition_status_matches"] = bool(pre_row) and pre_row[0] == declared
    if not all(checks.values()):
        return _inc("seed state does not match scenario preconditions", checks)
    pre_status = pre_row[0]

    attempts = [e for e in http_log
                if e.get("method") == "POST" and e.get("path") == f"/orders/{order_id}/cancel"]
    checks["attempt_logged"] = bool(attempts)
    if not attempts:
        return _inc("cancellation attempt not found in HTTP log", checks)
    try:
        status = int(attempts[-1]["status"])
    except (KeyError, TypeError, ValueError):
        return _inc("attempt log entry has no usable status", checks)
    observed["attempt"] = {"order_id": order_id, "status": status}

    try:
        post_row = post.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        post_audit = post.execute("SELECT id, old_status, new_status, applied FROM order_status_changes "
                                  "WHERE order_id=?", (order_id,)).fetchall()
    except sqlite3.Error as e:
        return _inc(f"post-run snapshot unreadable: {e}", checks, observed)
    if post_row is None:
        return _inc("order missing from post-run snapshot", checks, observed)
    post_status = post_row[0]
    new_audit = [list(r) for r in post_audit if r[0] not in pre_ids]
    applied = [a for a in new_audit if a[3] == 1 and a[1] == pre_status and a[2] == "cancelled"]
    observed.update(status_before=pre_status, status_after=post_status, new_audit_rows=new_audit)
    cancelled = post_status == "cancelled"

    if pre_status in CANCELLABLE:        # positive control: the invariant is not engaged
        if status == 200 and cancelled and applied:
            checks["invariant_upheld"] = True
            return Result("PASS", [f"order {order_id} was {pre_status} and was cancelled; permitted"],
                          checks, observed)
        return _inc("cancellation of a cancellable order was not observed end to end", checks, observed)

    if status == 200 and cancelled and applied:
        checks["invariant_upheld"] = False
        return Result("VIOLATION", [f"order {order_id} was {pre_status} and was cancelled anyway; "
                                    f"shipped orders cannot be cancelled"], checks, observed)
    if status in (403, 409) and post_status == pre_status and not any(a[3] == 1 for a in new_audit):
        checks.update(attempt_blocked=True, invariant_upheld=True)
        return Result("PASS", [f"cancellation of {pre_status} order {order_id} was blocked "
                               f"(HTTP {status}); status unchanged and no change applied"], checks, observed)
    return _inc("conflicting evidence between HTTP log, orders table and audit log", checks, observed)


def max_discount_pct(rule, scenario, pre, post, http_log):
    """P-04. A discount may not exceed the rule's maximum percent (strictly greater is a violation:
    30 is allowed, 31 is not). The maximum comes from the validated Rule, never from the model or
    the scenario; the attempted percent is read from the observed HTTP log.

    Evidence (all must agree): the HTTP status for POST /orders/<id>/discount, the order's
    discount_pct before vs after, and the discount_changes audit row.
    Limitation: the audit table is maintained by the app under test.
    """
    try:
        maximum = rule.params["max_percent"]
        if set(rule.params) != {"max_percent"} or isinstance(maximum, bool) or not isinstance(maximum, int) \
                or not 1 <= maximum <= 100:
            raise ValueError
        order_id = int(scenario.preconditions["order_id"])
    except (KeyError, TypeError, ValueError):
        return _inc("rule max_percent or scenario order_id missing or invalid")

    checks, observed = {}, {}
    try:
        pre_row = pre.execute("SELECT discount_pct FROM orders WHERE id=?", (order_id,)).fetchone()
        pre_ids = {r[0] for r in pre.execute(
            "SELECT id FROM discount_changes WHERE order_id=?", (order_id,))}
    except sqlite3.Error as e:
        return _inc(f"pre-run snapshot unreadable: {e}")
    checks["precondition_order_exists"] = pre_row is not None
    checks["precondition_discount_within_max"] = bool(pre_row) and pre_row[0] is not None \
        and 0 <= pre_row[0] <= maximum
    if not all(checks.values()):
        return _inc("seed state does not match scenario preconditions", checks)
    pre_pct = pre_row[0]

    attempts = [e for e in http_log
                if e.get("method") == "POST" and e.get("path") == f"/orders/{order_id}/discount"]
    checks["attempt_logged"] = bool(attempts)
    if not attempts:
        return _inc("discount attempt not found in HTTP log", checks)
    try:
        percent = int(attempts[-1]["form"]["percent"])
        status = int(attempts[-1]["status"])
    except (KeyError, TypeError, ValueError):
        return _inc("attempt log entry has no usable percent or status", checks)
    if not 0 <= percent <= 100:
        return _inc("attempted percent is outside 0..100", checks)
    observed["attempt"] = {"order_id": order_id, "percent": percent, "status": status, "maximum": maximum}

    try:
        post_row = post.execute("SELECT discount_pct FROM orders WHERE id=?", (order_id,)).fetchone()
        post_audit = post.execute("SELECT id, old_pct, new_pct, applied FROM discount_changes "
                                  "WHERE order_id=?", (order_id,)).fetchall()
    except sqlite3.Error as e:
        return _inc(f"post-run snapshot unreadable: {e}", checks, observed)
    if post_row is None:
        return _inc("order missing from post-run snapshot", checks, observed)
    post_pct = post_row[0]
    new_audit = [list(r) for r in post_audit if r[0] not in pre_ids]
    applied = [a for a in new_audit if a[3] == 1 and a[1] == pre_pct and a[2] == percent]
    observed.update(discount_before=pre_pct, discount_after=post_pct, new_audit_rows=new_audit)

    if percent <= maximum:               # boundary / allowed: the invariant is not engaged
        if status == 200 and post_pct == percent and applied:
            checks["invariant_upheld"] = True
            return Result("PASS", [f"discount of {percent}% is at or below {maximum}%; allowed and applied"],
                          checks, observed)
        return _inc("allowed discount was not observed end to end", checks, observed)

    if status == 200 and post_pct == percent and applied:
        checks["invariant_upheld"] = False
        return Result("VIOLATION", [f"a discount of {percent}% was applied; discounts cannot exceed {maximum}%"],
                      checks, observed)
    if status in (403, 422) and post_pct == pre_pct and not any(a[3] == 1 for a in new_audit):
        checks.update(attempt_blocked=True, invariant_upheld=True)
        return Result("PASS", [f"discount of {percent}% was blocked (HTTP {status}); discount unchanged "
                               f"and no change applied"], checks, observed)
    return _inc("conflicting evidence between HTTP log, orders table and audit log", checks, observed)
