import os
import sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))
pytest.importorskip("playwright")
from local_e2e import run  # noqa: E402

NS = os.environ.get("PR_NO_SANDBOX") == "1"


@pytest.mark.parametrize("name,profile,amount,verdict", [
    ("buggy_15000", "buggy", 15000, "VIOLATION"),
    ("correct_15000", "correct", 15000, "PASS"),
    ("correct_10000", "correct", 10000, "PASS"),
])
def test_full_pipeline(tmp_path, name, profile, amount, verdict):
    r = run(name, profile, amount, tmp_path, NS)
    assert r["verdict"] == verdict, r
    assert r["worker"]["exit_code"] == 0
