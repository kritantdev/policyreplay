import os
import sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "orchestrator"))
pytest.importorskip("playwright")
from local_e2e import CASES, run  # noqa: E402

NS = os.environ.get("PR_NO_SANDBOX") == "1"


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_full_pipeline(tmp_path, c):
    r = run(c, tmp_path, NS)
    assert r["verdict"] == c["expected"], r
    assert r["worker"]["exit_code"] == 0
