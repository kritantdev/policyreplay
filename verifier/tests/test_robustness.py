"""Malformed evidence or scenario values must give INCONCLUSIVE, never an unhandled exception."""
import json

import pytest

from verifier.make_fixtures import make_p02, make_p03
from verifier.verify import verify


def run(d, **o):
    f = dict(rule=d / "rule.json", scenario=d / "scenario.json", pre=d / "pre.db",
             post=d / "post.db", log=d / "http_log.json")
    f.update(o)
    return verify(f["rule"], f["scenario"], f["pre"], f["post"], f["log"])


@pytest.mark.parametrize("bad", [["shipped"], {"a": 1}, 7, None, True])
def test_p03_non_string_declared_status_is_inconclusive(tmp_path, bad):
    d = make_p03("x", "buggy", tmp_path)
    s = json.loads((d / "scenario.json").read_text())
    s["preconditions"]["order_status"] = bad
    (d / "s2.json").write_text(json.dumps(s))
    assert run(d, scenario=d / "s2.json")["verdict"] == "INCONCLUSIVE"


@pytest.mark.parametrize("bad", [["admin"], {"a": 1}, 7, None])
def test_p02_non_string_role_in_the_http_log_is_inconclusive(tmp_path, bad):
    d = make_p02("x", "buggy", tmp_path)
    log = json.loads((d / "http_log.json").read_text())
    log[0]["form"]["role"] = bad
    (d / "log2.json").write_text(json.dumps(log))
    assert run(d, log=d / "log2.json")["verdict"] == "INCONCLUSIVE"
