import json
import pytest
from verifier.make_fixtures import make_p04
from verifier.verify import verify


def run(d, **o):
    f = dict(rule=d / "rule.json", scenario=d / "scenario.json", pre=d / "pre.db",
             post=d / "post.db", log=d / "http_log.json")
    f.update(o)
    return verify(f["rule"], f["scenario"], f["pre"], f["post"], f["log"])


def tweak(d, name, fname, fn):
    obj = json.loads((d / fname).read_text())
    fn(obj)
    (d / name).write_text(json.dumps(obj))
    return d / name


def test_40_percent_is_a_violation_in_buggy_build(tmp_path):
    r = run(make_p04("b", "buggy", tmp_path))
    assert r["verdict"] == "VIOLATION"
    assert r["observed"]["discount_before"] == 0 and r["observed"]["discount_after"] == 40


def test_correct_build_blocks_40_with_422_and_passes(tmp_path):
    r = run(make_p04("c", "correct", tmp_path))
    assert r["verdict"] == "PASS" and r["observed"]["attempt"]["status"] == 422
    assert r["observed"]["discount_after"] == 0


def test_boundary_exactly_30_passes_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        r = run(make_p04(f"bnd{p}", p, tmp_path, percent=30))
        assert r["verdict"] == "PASS", r


def test_31_is_a_violation_in_buggy_and_blocked_in_correct(tmp_path):
    assert run(make_p04("b31", "buggy", tmp_path, percent=31))["verdict"] == "VIOLATION"
    assert run(make_p04("c31", "correct", tmp_path, percent=31))["verdict"] == "PASS"


def test_threshold_comes_from_the_rule_not_the_scenario(tmp_path):
    d = make_p04("b", "buggy", tmp_path, percent=40)
    # the rule says 50: 40 is within the maximum, so a successful discount is a PASS, not a violation
    r50 = tweak(d, "r50.json", "rule.json", lambda r: r["params"].__setitem__("max_percent", 50))
    assert run(d, rule=r50)["verdict"] == "PASS"
    # a model-supplied extra field in the scenario cannot move the threshold
    s = tweak(d, "s_extra.json", "scenario.json", lambda s: s["preconditions"].__setitem__("max_percent", 90))
    assert run(d, scenario=s)["verdict"] == "VIOLATION"


@pytest.mark.parametrize("params", [{}, {"max_percent": 0}, {"max_percent": 101}, {"max_percent": "30"},
                                    {"max_percent": True}, {"max_percent": 30, "x": 1}])
def test_invalid_rule_params_are_inconclusive(tmp_path, params):
    d = make_p04("c", "correct", tmp_path)
    r = tweak(d, "rbad.json", "rule.json", lambda r: r.__setitem__("params", params))
    assert run(d, rule=r)["verdict"] == "INCONCLUSIVE"


def test_unreadable_db_is_inconclusive(tmp_path):
    d = make_p04("c", "correct", tmp_path)
    assert run(d, post=d / "missing.db")["verdict"] == "INCONCLUSIVE"
    assert run(d, pre=d / "missing.db")["verdict"] == "INCONCLUSIVE"


def test_attempt_not_logged_is_inconclusive(tmp_path):
    d = make_p04("c", "correct", tmp_path)
    (d / "empty.json").write_text("[]")
    assert run(d, log=d / "empty.json")["verdict"] == "INCONCLUSIVE"


def test_log_entry_without_percent_is_inconclusive(tmp_path):
    d = make_p04("c", "correct", tmp_path)
    log = tweak(d, "nopct.json", "http_log.json", lambda l: l[0].pop("form"))
    assert run(d, log=log)["verdict"] == "INCONCLUSIVE"


def test_unknown_order_is_inconclusive(tmp_path):
    d = make_p04("c", "correct", tmp_path)
    s = tweak(d, "s2.json", "scenario.json", lambda s: s["preconditions"].__setitem__("order_id", 99))
    assert run(d, scenario=s)["verdict"] == "INCONCLUSIVE"


def test_pre_run_discount_above_max_is_inconclusive(tmp_path):
    import sqlite3
    d = make_p04("c", "correct", tmp_path)
    con = sqlite3.connect(d / "pre.db")
    con.execute("UPDATE orders SET discount_pct=45 WHERE id=1004")
    con.commit()
    con.close()
    assert run(d)["verdict"] == "INCONCLUSIVE"


def test_conflicting_evidence_log_says_422_but_db_shows_discount(tmp_path):
    d = make_p04("b", "buggy", tmp_path)
    log = tweak(d, "lie.json", "http_log.json", lambda l: l[0].__setitem__("status", 422))
    assert run(d, log=log)["verdict"] == "INCONCLUSIVE"


def test_conflicting_evidence_log_says_200_but_db_unchanged(tmp_path):
    d = make_p04("c", "correct", tmp_path)
    log = tweak(d, "lie.json", "http_log.json", lambda l: l[0].__setitem__("status", 200))
    assert run(d, log=log)["verdict"] == "INCONCLUSIVE"


def test_log_percent_differs_from_what_the_db_shows_is_inconclusive(tmp_path):
    d = make_p04("b", "buggy", tmp_path, percent=40)
    log = tweak(d, "lie.json", "http_log.json", lambda l: l[0]["form"].__setitem__("percent", "20"))
    assert run(d, log=log)["verdict"] == "INCONCLUSIVE"       # 20 <= 30 but DB shows 40: not proven


def test_allowed_boundary_that_was_not_applied_is_inconclusive(tmp_path):
    d = make_p04("c", "correct", tmp_path, percent=30)
    log = tweak(d, "bad.json", "http_log.json", lambda l: l[0].__setitem__("status", 500))
    assert run(d, log=log)["verdict"] == "INCONCLUSIVE"
