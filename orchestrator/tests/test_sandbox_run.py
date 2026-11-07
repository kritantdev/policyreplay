"""Control-flow tests for the Docker runner using a stub `docker` (proves the Python logic, not containment)."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator import sandbox_run as sr  # noqa: E402
from orchestrator.local_e2e import CASES  # noqa: E402
from verifier.make_fixtures import case_files  # noqa: E402


@pytest.fixture()
def stub_docker(monkeypatch):
    calls = []

    def fake(args, timeout=60, text=True):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, stdout="" if text else b"", stderr="" if text else b"")
    monkeypatch.setattr(sr, "docker", fake)
    return calls


def test_second_run_survives_an_undeletable_previous_folder(tmp_path, stub_docker, monkeypatch):
    """The VM crashed with FileExistsError here: worker-written files (uid 10001) could not be deleted."""
    monkeypatch.setattr(sr.shutil, "rmtree", lambda *a, **k: None)        # simulate: cannot delete
    rule, sc = case_files("P-05", "x", {})
    out = tmp_path / "runs" / "case"
    first = sr.run_scenario(rule, sc, "buggy", out)
    second = sr.run_scenario(rule, sc, "buggy", out)                     # must not raise
    assert first["sandbox"]["evidence_dir"] == str(out)
    assert second["sandbox"]["evidence_dir"] != str(out)                 # fresh folder; old evidence untouched
    assert (out / "evidence").is_dir() and Path(second["sandbox"]["evidence_dir"], "evidence").is_dir()
    assert second["verdict"] == "INCONCLUSIVE"                           # no evidence -> never a guess


def test_stale_folder_that_can_be_deleted_is_reused(tmp_path, stub_docker):
    rule, sc = case_files("P-05", "x", {})
    out = tmp_path / "runs" / "case"
    sr.run_scenario(rule, sc, "buggy", out)
    again = sr.run_scenario(rule, sc, "buggy", out)
    assert again["sandbox"]["evidence_dir"] == str(out)


@pytest.mark.parametrize("c", CASES, ids=[c["name"] for c in CASES])
def test_every_case_in_the_table_flows_through_the_runner_and_cleans_up(tmp_path, stub_docker, c):
    r = sr.run_case(c, tmp_path)
    assert r["sandbox"]["cleanup_confirmed"] is True and r["sandbox"]["error"] is None
    assert r["verdict"] == "INCONCLUSIVE"                                # stub produces no evidence
    assert any(a[:2] == ["rm", "-f"] for a in stub_docker)               # containers removed
    assert any(a[:2] == ["network", "rm"] for a in stub_docker)          # run network removed
