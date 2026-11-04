import json
import pytest
from verifier.make_fixtures import make
from verifier.verify import verify
from pydantic import ValidationError


def run(d, **override):
    f = {"rule": d / "rule.json", "scenario": d / "scenario.json", "pre": d / "pre.db",
         "post": d / "post.db", "log": d / "http_log.json"}
    f.update(override)
    return verify(f["rule"], f["scenario"], f["pre"], f["post"], f["log"])


def test_planted_violation_detected(tmp_path):
    assert run(make("b", "buggy", 15000, tmp_path))["verdict"] == "VIOLATION"


def test_clean_twin_passes(tmp_path):
    assert run(make("c", "correct", 15000, tmp_path))["verdict"] == "PASS"


def test_boundary_exactly_threshold_is_allowed(tmp_path):
    for p in ("correct", "buggy"):
        assert run(make(f"x{p}", p, 10000, tmp_path))["verdict"] == "PASS"


def test_unreadable_db_is_inconclusive_not_pass(tmp_path):
    d = make("c", "correct", 15000, tmp_path)
    assert run(d, post=d / "missing.db")["verdict"] == "INCONCLUSIVE"
    (d / "bad.db").write_bytes(b"not a database at all" * 100)
    assert run(d, post=d / "bad.db")["verdict"] == "INCONCLUSIVE"


def test_attempt_not_logged_is_inconclusive(tmp_path):
    d = make("c", "correct", 15000, tmp_path)
    (d / "empty.json").write_text("[]")
    assert run(d, log=d / "empty.json")["verdict"] == "INCONCLUSIVE"


def test_wrong_seed_state_is_inconclusive(tmp_path):
    d = make("c", "correct", 15000, tmp_path)
    s = json.loads((d / "scenario.json").read_text())
    s["preconditions"]["order_id"] = 9999
    (d / "s2.json").write_text(json.dumps(s))
    assert run(d, scenario=d / "s2.json")["verdict"] == "INCONCLUSIVE"


def test_conflicting_evidence_is_inconclusive(tmp_path):
    d = make("b", "buggy", 15000, tmp_path)   # DB shows violation...
    log = json.loads((d / "http_log.json").read_text())
    log[0]["status"] = 403                    # ...but log claims it was blocked
    (d / "lie.json").write_text(json.dumps(log))
    assert run(d, log=d / "lie.json")["verdict"] == "INCONCLUSIVE"


def test_unknown_invariant_type_rejected(tmp_path):
    d = make("c", "correct", 15000, tmp_path)
    r = json.loads((d / "rule.json").read_text())
    r["invariant_type"] = "drop_all_tables"
    (d / "r2.json").write_text(json.dumps(r))
    with pytest.raises(ValidationError):
        run(d, rule=d / "r2.json")
