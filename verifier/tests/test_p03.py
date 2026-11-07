import json
from verifier.make_fixtures import make_p03
from verifier.verify import verify


def run(d, **o):
    f = dict(rule=d / "rule.json", scenario=d / "scenario.json", pre=d / "pre.db",
             post=d / "post.db", log=d / "http_log.json")
    f.update(o)
    return verify(f["rule"], f["scenario"], f["pre"], f["post"], f["log"])


def test_cancelling_a_shipped_order_is_a_violation_in_buggy_build(tmp_path):
    r = run(make_p03("b", "buggy", tmp_path))
    assert r["verdict"] == "VIOLATION"
    assert r["observed"]["status_before"] == "shipped" and r["observed"]["status_after"] == "cancelled"


def test_correct_build_blocks_and_passes(tmp_path):
    r = run(make_p03("c", "correct", tmp_path))
    assert r["verdict"] == "PASS" and r["observed"]["attempt"]["status"] == 409
    assert r["observed"]["status_after"] == "shipped"


def test_delivered_order_is_also_protected(tmp_path):
    # alice owns delivered order 1001: the buggy build cancels it, the correct build refuses (409)
    assert run(make_p03("d", "buggy", tmp_path, order_id=1001))["verdict"] == "VIOLATION"
    assert run(make_p03("dc", "correct", tmp_path, order_id=1001))["verdict"] == "PASS"


def test_control_cancelling_processing_order_passes_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        r = run(make_p03(f"ctl{p}", p, tmp_path, order_id=1004))
        assert r["verdict"] == "PASS", r


def test_unreadable_db_is_inconclusive(tmp_path):
    d = make_p03("c", "correct", tmp_path)
    assert run(d, post=d / "missing.db")["verdict"] == "INCONCLUSIVE"


def test_attempt_not_logged_is_inconclusive(tmp_path):
    d = make_p03("c", "correct", tmp_path)
    (d / "empty.json").write_text("[]")
    assert run(d, log=d / "empty.json")["verdict"] == "INCONCLUSIVE"


def test_wrong_seed_status_is_inconclusive(tmp_path):
    d = make_p03("c", "correct", tmp_path)
    s = json.loads((d / "scenario.json").read_text())
    s["preconditions"]["order_status"] = "processing"        # scenario claims a different status than the seed
    (d / "s2.json").write_text(json.dumps(s))
    assert run(d, scenario=d / "s2.json")["verdict"] == "INCONCLUSIVE"


def test_missing_or_unknown_declared_status_is_inconclusive(tmp_path):
    d = make_p03("c", "correct", tmp_path)
    for mut in (lambda p: p.pop("order_status"), lambda p: p.__setitem__("order_status", "lost")):
        s = json.loads((d / "scenario.json").read_text())
        mut(s["preconditions"])
        (d / "s3.json").write_text(json.dumps(s))
        assert run(d, scenario=d / "s3.json")["verdict"] == "INCONCLUSIVE"


def test_conflicting_evidence_log_says_409_but_db_shows_cancelled(tmp_path):
    d = make_p03("b", "buggy", tmp_path)
    log = json.loads((d / "http_log.json").read_text())
    log[0]["status"] = 409
    (d / "lie.json").write_text(json.dumps(log))
    assert run(d, log=d / "lie.json")["verdict"] == "INCONCLUSIVE"


def test_conflicting_evidence_log_says_200_but_db_unchanged(tmp_path):
    d = make_p03("c", "correct", tmp_path)
    log = json.loads((d / "http_log.json").read_text())
    log[0]["status"] = 200
    (d / "lie.json").write_text(json.dumps(log))
    assert run(d, log=d / "lie.json")["verdict"] == "INCONCLUSIVE"


def test_control_that_did_not_cancel_is_inconclusive(tmp_path):
    d = make_p03("x", "correct", tmp_path, order_id=1004)
    log = json.loads((d / "http_log.json").read_text())
    log[0]["status"] = 500
    (d / "bad.json").write_text(json.dumps(log))
    assert run(d, log=d / "bad.json")["verdict"] == "INCONCLUSIVE"


def test_wrong_rule_type_is_rejected(tmp_path):
    d = make_p03("c", "correct", tmp_path)
    r = json.loads((d / "rule.json").read_text())
    r["invariant_type"] = "owner_only_access"
    (d / "r2.json").write_text(json.dumps(r))
    try:
        run(d, rule=d / "r2.json")
    except ValueError:
        return
    raise AssertionError("rule/scenario mismatch must be rejected")
