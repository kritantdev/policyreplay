"""The deploy files are code: they must not silently drift from the app or expose the API."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient  # noqa: E402
from orchestrator.main import create_api  # noqa: E402

UNIT = (ROOT / "deploy" / "policyreplay.service").read_text()
POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()


def lines(prefix):
    return [ln.split("=", 1)[1].strip() for ln in UNIT.splitlines() if ln.startswith(prefix)]


def test_service_unit_is_safe():
    exec_start = lines("ExecStart=")[0]
    assert "orchestrator.main:app" in exec_start
    assert "--host 127.0.0.1" in exec_start and "0.0.0.0" not in exec_start     # only the reverse proxy may reach it
    assert lines("User=") == ["deploy"]                                          # never root
    assert lines("EnvironmentFile=") == ["/etc/policyreplay.env"]
    assert "PR_RUN_MODE=docker" in lines("Environment=")
    assert not re.search(r"(KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S", "\n".join(lines("Environment=")), re.I)


def test_daily_cap_line_is_a_sane_integer():
    caps = [v for v in lines("Environment=") if v.startswith("PR_DAILY_MODEL_CALLS=")]
    assert len(caps) == 1                                                        # exactly one place sets it
    assert 1 <= int(caps[0].split("=")[1]) <= 5000


def test_every_runtime_setting_is_documented_in_env_example():
    documented = (ROOT / ".env.example").read_text()
    used = set()
    for f in (ROOT / "orchestrator").glob("*.py"):
        used |= set(re.findall(r'environ(?:\.get)?\(\s*"(PR_[A-Z_]+)"', f.read_text()))
        used |= set(re.findall(r'environ\["(PR_[A-Z_]+)"\]', f.read_text()))
    assert {"PR_DAILY_MODEL_CALLS", "PR_RUN_MODE", "PR_TRUST_PROXY", "PR_TEXT_MODEL"} <= used
    assert [v for v in sorted(used) if v not in documented] == []


def test_the_cap_is_really_read_from_the_environment(tmp_path, monkeypatch):
    """Setting PR_DAILY_MODEL_CALLS must change behaviour: with a cap of 1 only the first of the two calls is live."""
    monkeypatch.setenv("PR_DAILY_MODEL_CALLS", "1")
    rules = (ROOT / "orchestrator/golden/rules.json").read_text()
    scen = (ROOT / "orchestrator/golden/scenarios.json").read_text()
    calls = []

    def model(messages, name):
        calls.append(1)
        return rules if messages[0]["content"].startswith("You extract") else scen
    c = TestClient(create_api(runs_dir=tmp_path, runner=lambda *a, **k: {}, transport=model))
    a = c.post("/api/analyze", json={}).json()
    assert a["source"] == {"rules": "model", "scenarios": "replayed model output"} and len(calls) == 1


def test_raising_the_cap_lets_more_calls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_DAILY_MODEL_CALLS", "4")
    rules = (ROOT / "orchestrator/golden/rules.json").read_text()
    scen = (ROOT / "orchestrator/golden/scenarios.json").read_text()
    model = lambda messages, name: rules if messages[0]["content"].startswith("You extract") else scen  # noqa: E731
    c = TestClient(create_api(runs_dir=tmp_path, runner=lambda *a, **k: {}, transport=model))
    sources = [c.post("/api/analyze", json={}).json()["source"] for _ in range(3)]
    assert sources[0] == sources[1] == {"rules": "model", "scenarios": "model"}      # 4 calls = 2 full analyses
    assert sources[2]["rules"] == "replayed model output"                            # then the labeled fallback
