import itertools, sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import os; os.environ["PR_AUTOSTART"] = "0"
from fastapi.testclient import TestClient
from app import create_app


_n = itertools.count()


def client(profile, tmp_path, user):
    # unique file per call: Windows cannot delete a SQLite file another app instance still holds open
    db = str(tmp_path / f"{profile}-{next(_n)}.db")
    c = TestClient(create_app(profile, db), follow_redirects=True)
    assert c.post("/login", data={"username": user}).status_code == 200
    return c, db


def violations(db):
    q = """SELECT r.id FROM refunds r WHERE r.amount > 10000 AND NOT EXISTS
           (SELECT 1 FROM approvals a WHERE a.order_id=r.order_id AND a.status='approved'
            AND a.amount>=r.amount)"""
    return sqlite3.connect(db).execute(q).fetchall()


def test_buggy_allows_unapproved_refund(tmp_path):
    c, db = client("buggy", tmp_path, "support1")
    assert c.post("/orders/1001/refund", data={"amount": 15000}).status_code == 200
    assert len(violations(db)) == 1


def test_correct_blocks_unapproved_refund(tmp_path):
    c, db = client("correct", tmp_path, "support1")
    r = c.post("/orders/1001/refund", data={"amount": 15000})
    assert r.status_code == 403 and 'data-testid="error"' in r.text
    assert violations(db) == []
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 0


def test_boundary_exactly_10000_allowed_in_both(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "support1")
        assert c.post("/orders/1001/refund", data={"amount": 10000}).status_code == 200
        assert violations(db) == []


def test_correct_allows_after_manager_approval(tmp_path):
    c, db = client("correct", tmp_path, "support1")
    c.post("/orders/1001/approval-request", data={"amount": 15000})
    m = TestClient(c.app, follow_redirects=True)
    m.post("/login", data={"username": "manager1"})
    m.post("/approvals/1/approve")
    assert c.post("/orders/1001/refund", data={"amount": 15000}).status_code == 200
    assert violations(db) == []


def test_customer_cannot_refund(tmp_path):
    c, _ = client("buggy", tmp_path, "alice")
    assert c.post("/orders/1001/refund", data={"amount": 500}).status_code == 403


# ---- P-05 (access control) ----
def _views(db):
    return sqlite3.connect(db).execute("SELECT user_id, order_id, status FROM order_views").fetchall()


def test_p05_buggy_serves_other_customers_order(tmp_path):
    c, db = client("buggy", tmp_path, "alice")
    assert c.get("/orders/1002").status_code == 200       # bob's order
    assert _views(db) == [(4, 1002, 200)]


def test_p05_correct_blocks_other_customers_order(tmp_path):
    c, db = client("correct", tmp_path, "alice")
    r = c.get("/orders/1002")
    assert r.status_code == 403 and "8000" not in r.text
    assert _views(db) == [(4, 1002, 403)]


def test_p05_customer_can_view_own_order_and_list_is_filtered(tmp_path):
    for p in ("correct", "buggy"):
        c, _ = client(p, tmp_path, "alice")
        assert c.get("/orders/1001").status_code == 200
        lst = c.get("/orders").text
        assert "order-row-1001" in lst and "order-row-1002" not in lst


def test_p05_staff_can_view_any_order(tmp_path):
    c, _ = client("correct", tmp_path, "support1")
    assert c.get("/orders/1002").status_code == 200


def test_p05_post_pages_never_leak_other_customers_order(tmp_path):
    for p in ("correct", "buggy"):
        c, _ = client(p, tmp_path, "alice")
        r = c.post("/orders/1002/refund", data={"amount": 100})
        assert r.status_code == 403 and "8000" not in r.text


# ---- P-02 (role changes) ----
def _role(db, uid):
    return sqlite3.connect(db).execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()[0]


def _audit(db):
    return sqlite3.connect(db).execute(
        "SELECT target_user_id, old_role, new_role, changed_by, actor_role, applied "
        "FROM role_changes").fetchall()


def test_p02_buggy_lets_support_promote_self(tmp_path):
    c, db = client("buggy", tmp_path, "support1")
    assert c.post("/users/1/role", data={"role": "admin"}).status_code == 200
    assert _role(db, 1) == "admin"
    assert _audit(db) == [(1, "support", "admin", 1, "support", 1)]   # actor role at the time


def test_p02_correct_blocks_support_self_promotion(tmp_path):
    c, db = client("correct", tmp_path, "support1")
    assert c.post("/users/1/role", data={"role": "admin"}).status_code == 403
    assert _role(db, 1) == "support"
    assert _audit(db) == [(1, "support", "admin", 1, "support", 0)]


def test_p02_admin_can_change_roles_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "admin1")
        assert c.post("/users/4/role", data={"role": "manager"}).status_code == 200
        assert _role(db, 4) == "manager"


def test_p02_customers_blocked_and_bad_input_rejected(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "alice")
        assert c.post("/users/4/role", data={"role": "admin"}).status_code == 403
        assert c.get("/users").status_code == 403
        assert _role(db, 4) == "customer"
    c, db = client("buggy", tmp_path, "admin1")
    assert c.post("/users/4/role", data={"role": "root"}).status_code == 400
    assert c.post("/users/99/role", data={"role": "admin"}).status_code == 404
    assert _audit(db) == []
