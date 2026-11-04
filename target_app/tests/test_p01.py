import sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import os; os.environ["PR_AUTOSTART"] = "0"
from fastapi.testclient import TestClient
from app import create_app


def client(profile, tmp_path, user):
    db = str(tmp_path / f"{profile}.db")
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
