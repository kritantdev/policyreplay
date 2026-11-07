import itertools, sqlite3, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
import os; os.environ["PR_AUTOSTART"] = "0"
from fastapi.testclient import TestClient
from app import create_app

_n = itertools.count()


def client(profile, tmp_path, user):
    db = str(tmp_path / f"p04-{profile}-{next(_n)}.db")
    c = TestClient(create_app(profile, db), follow_redirects=True)
    assert c.post("/login", data={"username": user}).status_code == 200
    return c, db


def disc(db, oid=1004):
    return sqlite3.connect(db).execute("SELECT discount_pct FROM orders WHERE id=?", (oid,)).fetchone()[0]


def audit(db):
    return sqlite3.connect(db).execute(
        "SELECT order_id, old_pct, new_pct, changed_by, actor_role, applied FROM discount_changes ORDER BY id").fetchall()


def test_buggy_accepts_40_percent(tmp_path):
    c, db = client("buggy", tmp_path, "support1")
    assert c.post("/orders/1004/discount", data={"percent": "40"}).status_code == 200
    assert disc(db) == 40
    assert audit(db) == [(1004, 0, 40, 1, "support", 1)]


def test_correct_refuses_40_and_31_with_422(tmp_path):
    for pct in ("40", "31", "100"):
        c, db = client("correct", tmp_path, "support1")
        r = c.post("/orders/1004/discount", data={"percent": pct})
        assert r.status_code == 422 and 'data-testid="error"' in r.text
        assert disc(db) == 0
        assert audit(db) == [(1004, 0, int(pct), 1, "support", 0)]       # attempt recorded, not applied


def test_exactly_30_is_allowed_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "support1")
        r = c.post("/orders/1004/discount", data={"percent": "30"})
        assert r.status_code == 200 and 'data-testid="flash-success"' in r.text
        assert disc(db) == 30 and audit(db) == [(1004, 0, 30, 1, "support", 1)]


def test_discount_is_visible_on_the_order_page(tmp_path):
    c, _ = client("correct", tmp_path, "support1")
    assert '<p data-testid="order-discount">0</p>' in c.get("/orders/1004").text
    c.post("/orders/1004/discount", data={"percent": "25"})
    assert '<p data-testid="order-discount">25</p>' in c.get("/orders/1004").text


def test_customers_get_403_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "alice")
        assert c.post("/orders/1004/discount", data={"percent": "10"}).status_code == 403
        assert disc(db) == 0 and audit(db) == []


def test_bad_input_gets_400_in_both_builds_and_leaves_no_audit_row(tmp_path):
    for p in ("correct", "buggy"):
        c, db = client(p, tmp_path, "manager1")
        for bad in ("", "abc", "-5", "101", "12.5", "1e1", " ", "٣٠"):
            assert c.post("/orders/1004/discount", data={"percent": bad}).status_code == 400, bad
        assert c.post("/orders/1004/discount", data={}).status_code == 400          # field missing
        assert disc(db) == 0 and audit(db) == []


def test_unknown_order_and_anonymous(tmp_path):
    c, db = client("buggy", tmp_path, "support1")
    assert c.post("/orders/9999/discount", data={"percent": "10"}).status_code == 404
    anon = TestClient(c.app, follow_redirects=False)
    assert anon.post("/orders/1004/discount", data={"percent": "10"}).status_code == 303
    assert audit(db) == []


def test_old_pct_records_the_value_at_the_time(tmp_path):
    c, db = client("buggy", tmp_path, "admin1")
    c.post("/orders/1004/discount", data={"percent": "10"})
    c.post("/orders/1004/discount", data={"percent": "50"})
    assert audit(db) == [(1004, 0, 10, 3, "admin", 1), (1004, 10, 50, 3, "admin", 1)]


def test_seed_orders_start_with_no_discount(tmp_path):
    _, db = client("correct", tmp_path, "support1")
    assert sqlite3.connect(db).execute("SELECT DISTINCT discount_pct FROM orders").fetchall() == [(0,)]
