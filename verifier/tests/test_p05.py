from verifier.make_fixtures import make_p05
from verifier.verify import verify


def run(d, **o):
    f = dict(rule=d / "rule.json", scenario=d / "scenario.json", pre=d / "pre.db",
             post=d / "post.db", log=d / "http_log.json")
    f.update(o)
    return verify(f["rule"], f["scenario"], f["pre"], f["post"], f["log"])


def test_idor_detected_in_buggy_build(tmp_path):
    r = run(make_p05("b", "buggy", tmp_path))
    assert r["verdict"] == "VIOLATION" and r["observed"]["attempt"]["status"] == 200


def test_correct_build_blocks_and_passes(tmp_path):
    r = run(make_p05("c", "correct", tmp_path))
    assert r["verdict"] == "PASS" and r["observed"]["attempt"]["status"] == 403


def test_staff_viewing_any_order_is_not_a_violation(tmp_path):
    r = run(make_p05("s", "buggy", tmp_path, user="support1"))
    assert r["verdict"] != "VIOLATION"


def test_customer_viewing_own_order_is_not_a_violation(tmp_path):
    r = run(make_p05("o", "buggy", tmp_path, order_id=1001, user="alice"))
    assert r["verdict"] == "INCONCLUSIVE"


def test_unreadable_db_is_inconclusive(tmp_path):
    d = make_p05("c", "correct", tmp_path)
    assert run(d, post=d / "missing.db")["verdict"] == "INCONCLUSIVE"


def test_attempt_not_logged_is_inconclusive(tmp_path):
    d = make_p05("c", "correct", tmp_path)
    (d / "empty.json").write_text("[]")
    assert run(d, log=d / "empty.json")["verdict"] == "INCONCLUSIVE"
