import os
import sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))
pytest.importorskip("playwright")
from local_e2e import CASES, run  # noqa: E402

NS = os.environ.get("PR_NO_SANDBOX") == "1"
EXPECTED = {"buggy_15000": "VIOLATION", "correct_15000": "PASS", "correct_10000": "PASS",
            "buggy_p05": "VIOLATION", "correct_p05": "PASS"}


@pytest.mark.parametrize("policy,name,profile,amount", CASES)
def test_full_pipeline(tmp_path, policy, name, profile, amount):
    r = run(policy, name, profile, amount, tmp_path, NS)
    assert r["verdict"] == EXPECTED[name], r
    assert r["worker"]["exit_code"] == 0
