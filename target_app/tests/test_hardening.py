import itertools
import os
import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
os.environ["PR_AUTOSTART"] = "0"
from fastapi.testclient import TestClient  # noqa: E402
from app import create_app  # noqa: E402

_n = itertools.count()


def test_pathologically_long_percent_is_a_400_not_a_500_and_leaves_no_trace(tmp_path):
    for profile in ("buggy", "correct"):
        db = str(tmp_path / f"h-{profile}-{next(_n)}.db")
        c = TestClient(create_app(profile, db), follow_redirects=True, raise_server_exceptions=False)
        c.post("/login", data={"username": "support1"})
        for bad in ("9" * 5000, "0" * 4, "1000", "-5", "3.5", "\u0663\u0660", ""):    # includes non-ASCII digits
            assert c.post("/orders/1004/discount", data={"percent": bad}).status_code == 400, bad
        con = sqlite3.connect(db)
        assert con.execute("SELECT COUNT(*) FROM discount_changes").fetchone()[0] == 0
        assert con.execute("SELECT discount_pct FROM orders WHERE id=1004").fetchone()[0] == 0
        assert c.post("/orders/1004/discount", data={"percent": "30"}).status_code == 200   # boundary still fine
