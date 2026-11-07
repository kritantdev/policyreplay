import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
pytest.importorskip("playwright")
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402
from orchestrator.main import create_api  # noqa: E402

GOLD_RULES = json.loads((ROOT / "orchestrator/golden/rules.json").read_text())
SCENARIOS = json.loads((ROOT / "orchestrator/golden/scenarios.json").read_text())["scenarios"]
N_RULES, N_SCENARIOS = len(GOLD_RULES["rules"]), len(SCENARIOS)
N_BLOCKED = sum(x["expected_outcome"] == "blocked" for x in SCENARIOS)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def fake_runner(rule, sc, profile, out, on_event):
    out = Path(out)
    (out / "evidence" / "screenshots").mkdir(parents=True, exist_ok=True)
    (out / "evidence" / "screenshots" / "01_login.png").write_bytes(PNG)
    (out / "evidence" / "trace.json").write_text(json.dumps([
        {"step": 1, "action": "login", "role": "support", "ok": True, "error": None,
         "screenshot": "screenshots/01_login.png", "ts": 1}]))
    (out / "evidence" / "manifest.json").write_text(json.dumps({"screenshots/01_login.png": "ab" * 32}))
    on_event({"type": "stage", "text": f"fake run on {profile}"})
    time.sleep(0.05)
    bad = profile == "buggy" and sc["expected_outcome"] == "blocked"
    return {"verdict": "VIOLATION" if bad else "PASS", "reasons": ["fake reason <b>x</b>"],
            "checks": {"attempt_logged": True}, "observed": {"k": 1}, "artifact_sha256": {"post.db": "cd" * 32}}


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    app = create_api(runs_dir=tmp_path_factory.mktemp("runs"), runner=fake_runner, mode_label="test mode")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.fixture(scope="module")
def site_unsupported(tmp_path_factory):
    """A server whose (stubbed) model leaves one clause unsupported: keeps the 'not tested' UI covered."""
    rules = json.loads(json.dumps(GOLD_RULES))
    rules["rules"] = [r for r in rules["rules"] if r["rule_id"] != "P-04"]
    rules["unsupported_clauses"] = ["Discounts cannot exceed 30%."]
    sc = [x for x in SCENARIOS if x["rule_id"] != "P-04"]

    def model(messages, name):
        return json.dumps(rules if messages[0]["content"].startswith("You extract") else {"scenarios": sc})
    app = create_api(runs_dir=tmp_path_factory.mktemp("runs2"), runner=fake_runner, mode_label="test mode",
                     transport=model)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.fixture()
def page(site):
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"] if os.environ.get("PR_NO_SANDBOX") == "1" else [])
        pg = b.new_page()
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(site)
        yield pg
        b.close()


def test_full_flow_through_the_ui(page):
    assert page.inner_text("#mode") == "test mode"
    page.wait_for_function("document.querySelector('#policy').value.length > 0")   # loaded via fetch
    assert "Refunds above INR 10,000" in page.input_value("#policy")
    assert page.is_disabled("#run")                                  # cannot run before approval
    page.click("#analyze")
    page.wait_for_selector("#rules .card")
    assert page.locator("#rules .card").count() == N_RULES
    assert page.locator("#scenarios tbody tr").count() == N_SCENARIOS
    assert "replayed model output" in page.inner_text("#src")        # labeled, not passed off as live
    assert page.inner_text("#unsupported").strip() == ""             # sample policy: all five clauses are tested
    assert page.is_disabled("#run")
    page.click("#approve")
    page.wait_for_function("!document.querySelector('#run').disabled")
    page.click("#run")
    page.wait_for_selector("#summary .tile", timeout=15000)
    assert page.locator("#cases .case").count() == 2 * N_SCENARIOS == 18
    assert (N_RULES, N_SCENARIOS, N_BLOCKED) == (5, 9, 5)
    tiles = page.inner_text("#summary")
    assert "planted violations detected" in tiles and str(N_BLOCKED) in tiles
    page.click("#open-report")
    page.wait_for_selector(".banner")
    assert "The policy said no. The application said yes." in page.inner_text(".banner")
    assert page.locator(".badge.VIOLATION").count() >= N_BLOCKED
    assert page.locator(".badge.INCONCLUSIVE").count() == 0
    assert "Limitations" in page.inner_text("#report")
    page.locator("details summary").first.click()
    page.wait_for_selector(".shots img")
    assert page.eval_on_selector(".shots img", "i => i.complete && i.naturalWidth > 0")   # screenshot really served
    assert page.errors == []


def test_partial_approval_and_html_in_data_is_not_executed(page):
    page.click("#analyze")
    page.wait_for_selector("#scenarios tbody tr")
    for i in range(1, N_SCENARIOS):                                  # keep only the first scenario
        page.locator(".pick").nth(i).uncheck()
    page.click("#approve")
    page.wait_for_function("!document.querySelector('#run').disabled")
    page.click("#run")
    page.wait_for_selector("#summary .tile", timeout=15000)
    assert page.locator("#cases .case").count() == 2
    page.click("#open-report")
    page.wait_for_selector("#report .panel")
    assert page.locator("#report b").count() == 0 or "x" not in page.inner_text("#report b")  # <b>x</b> shown as text
    assert "<b>x</b>" in page.inner_text("#report")
    assert page.errors == []


def test_api_error_is_shown_not_swallowed(page):
    # Wait for the page's initial policy fetch first, or it can overwrite the text typed below (a race).
    page.wait_for_function("document.querySelector('#policy').value.length > 0")
    page.fill("#policy", "Refunds above INR 99 require a smile.")
    page.click("#analyze")
    page.wait_for_function("document.querySelector('#analyze-msg').textContent.length > 0")
    assert "no cached output" in page.inner_text("#analyze-msg")
    assert page.is_hidden("#analysis")


def test_unsupported_clause_is_shown_as_not_tested(site_unsupported):
    with sync_playwright() as p:
        b = p.chromium.launch(args=["--no-sandbox"] if os.environ.get("PR_NO_SANDBOX") == "1" else [])
        pg = b.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(site_unsupported)
        pg.click("#analyze")
        pg.wait_for_selector("#rules .card")
        assert pg.locator("#rules .card").count() == N_RULES - 1
        assert "no verifier yet" in pg.inner_text("#unsupported")
        assert "Discounts cannot exceed 30%" in pg.inner_text("#unsupported")
        assert "live model output" in pg.inner_text("#src")
        assert errors == []
        b.close()
