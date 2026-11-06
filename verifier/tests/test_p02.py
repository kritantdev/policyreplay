import json
from verifier.make_fixtures import make_p02
from verifier.verify import verify


def run(d, **o):
    f = dict(rule=d / "rule.json", scenario=d / "scenario.json", pre=d / "pre.db",
             post=d / "post.db", log=d / "http_log.json")
    f.update(o)
    return verify(f["rule"], f["scenario"], f["pre"], f["post"], f["log"])


def test_self_promotion_detected_in_buggy_build(tmp_path):
    r = run(make_p02("b", "buggy", tmp_path))
    assert r["verdict"] == "VIOLATION"
    assert r["observed"]["role_before"] == "support" and r["observed"]["role_after"] == "admin"


def test_correct_build_blocks_and_passes(tmp_path):
    r = run(make_p02("c", "correct", tmp_path))
    assert r["verdict"] == "PASS" and r["observed"]["attempt"]["status"] == 403


def test_manager_changing_roles_is_also_a_violation(tmp_path):
    assert run(make_p02("m", "buggy", tmp_path, actor="manager", target=4, new_role="support"))["verdict"] == "VIOLATION"


def test_admin_positive_control_passes_in_both_builds(tmp_path):
    for p in ("correct", "buggy"):
        r = run(make_p02(f"a{p}", p, tmp_path, actor="admin", target=4, new_role="manager"))
        assert r["verdict"] == "PASS", r


def test_unreadable_db_is_inconclusive(tmp_path):
    d = make_p02("c", "correct", tmp_path)
    assert run(d, post=d / "missing.db")["verdict"] == "INCONCLUSIVE"


def test_attempt_not_logged_is_inconclusive(tmp_path):
    d = make_p02("c", "correct", tmp_path)
    (d / "empty.json").write_text("[]")
    assert run(d, log=d / "empty.json")["verdict"] == "INCONCLUSIVE"


def test_conflicting_evidence_is_inconclusive(tmp_path):
    d = make_p02("b", "buggy", tmp_path)              # DB shows the role changed...
    log = json.loads((d / "http_log.json").read_text())
    log[0]["status"] = 403                            # ...but the log claims it was blocked
    (d / "lie.json").write_text(json.dumps(log))
    assert run(d, log=d / "lie.json")["verdict"] == "INCONCLUSIVE"


def test_wrong_seed_state_is_inconclusive(tmp_path):
    d = make_p02("c", "correct", tmp_path)
    s = json.loads((d / "scenario.json").read_text())
    s["preconditions"]["target_user_id"] = 99
    (d / "s2.json").write_text(json.dumps(s))
    assert run(d, scenario=d / "s2.json")["verdict"] == "INCONCLUSIVE"


def test_noop_role_change_is_inconclusive(tmp_path):
    d = make_p02("n", "buggy", tmp_path, new_role="support")   # support1 is already support
    assert run(d)["verdict"] == "INCONCLUSIVE"
