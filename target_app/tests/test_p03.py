import itertools, sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import os; os.environ["PR_AUTOSTART"] = "0"
from fastapi.testclient import TestClient
from app import create_app

_n = itertools.count()


def client(profile, tmp_path, user):
    db = str(tmp_path / f"p03-{profile}-{next(_n)}.db")
    c = TestClient(create_app(profile, db), follow_redirects=True)
    assert c.post("/login", data={"username": user}).status_code == 200
    return c, db


def status(db, oid):
    return sqlite3.connect(db).execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0]


def audit(db):
    return sqlite3.connect(db).execute(
        "SELECT order_id, old_status, new_status, changed_by, actor_role, applied "
        "FROM order_status_changes ORDER BY id").fetchall()


def test_buggy_cancels_a_shipped_order(tmp_path):
    c, db = client("buggy", tmp_path, "alice")
    assert c.post("/orders/1003/cancel").status_code == 200
    assert status(db, 1003) == "cancelled"
    assert audit(db) == [(1003, "shipped", "cancelled", 4, "customer", 1)]


def test_correct_refuses_a_shipped_order_with_409(tmp_path):
    c, db = client("correct", tmp_path, "alice")
    r = c.post("/orders/1003/cancel")
    assert r.status_code == 409 and 'data-testid="error"' in r.text
    assert status(db, 1003) == "shipped"
    assert audit(db) == [(1003, "shipped", "cancelled", 4, "customer", 0)]   # attempt recorded, not applied


def test_correct_refuses_a_delivered_order(tmp_path):
    c, db = client("correct", tmp_path, "support1")
    assert c.post("/orders/1001/cancel").status_code == 409
    assert status(db, 1001) == "delivered"


def test_processing_order_can_be_cancelled_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "alice")
        r = c.post("/orders/1004/cancel")
        assert r.status_code == 200 and 'data-testid="flash-success"' in r.text
        assert status(db, 1004) == "cancelled"
        assert audit(db) == [(1004, "processing", "cancelled", 4, "customer", 1)]


def test_customer_cannot_cancel_someone_elses_order(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "bob")
        assert c.post("/orders/1004/cancel").status_code == 403
        assert status(db, 1004) == "processing" and audit(db) == []


def test_staff_may_cancel_a_processing_order(tmp_path):
    c, db = client("correct", tmp_path, "support1")
    assert c.post("/orders/1004/cancel").status_code == 200
    assert audit(db)[0][4] == "support"


def test_audit_records_status_at_the_time(tmp_path):
    c, db = client("buggy", tmp_path, "alice")
    c.post("/orders/1004/cancel")
    assert c.post("/orders/1004/cancel").status_code == 409          # already cancelled: refused, no new row
    assert audit(db) == [(1004, "processing", "cancelled", 4, "customer", 1)]


def test_unknown_order_and_anonymous(tmp_path):
    c, db = client("buggy", tmp_path, "alice")
    assert c.post("/orders/9999/cancel").status_code == 404
    anon = TestClient(c.app, follow_redirects=False)
    assert anon.post("/orders/1003/cancel").status_code == 303
    assert audit(db) == []


def test_seed_orders_for_p01_and_p05_unchanged(tmp_path):
    c, db = client("correct", tmp_path, "alice")
    con = sqlite3.connect(db)
    assert con.execute("SELECT id,customer_id,total,status FROM orders WHERE id IN (1001,1002) ORDER BY id").fetchall() \
        == [(1001, 4, 20000, "delivered"), (1002, 5, 8000, "delivered")]
    assert con.execute("SELECT id,customer_id,total,status FROM orders WHERE id IN (1003,1004) ORDER BY id").fetchall() \
        == [(1003, 4, 5000, "shipped"), (1004, 4, 3000, "processing")]
