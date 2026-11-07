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
    bad = profile == "buggy" and sc["test_id"] != "P-01-BND-001"
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
    assert "Refunds above INR 10,000" in page.input_value("#policy")
    assert page.is_disabled("#run")                                  # cannot run before approval
    page.click("#analyze")
    page.wait_for_selector("#rules .card")
    assert page.locator("#rules .card").count() == 3
    assert page.locator("#scenarios tbody tr").count() == 5
    assert "replayed model output" in page.inner_text("#src")        # labeled, not passed off as live
    assert "no verifier yet" in page.inner_text("#unsupported")
    assert page.is_disabled("#run")
    page.click("#approve")
    page.wait_for_function("!document.querySelector('#run').disabled")
    page.click("#run")
    page.wait_for_selector("#summary .tile", timeout=15000)
    assert page.locator("#cases .case").count() == 10
    tiles = page.inner_text("#summary")
    assert "planted violations detected" in tiles and "4" in tiles
    page.click("#open-report")
    page.wait_for_selector(".banner")
    assert "The policy said no. The application said yes." in page.inner_text(".banner")
    assert page.locator(".badge.VIOLATION").count() >= 4
    assert page.locator(".badge.INCONCLUSIVE").count() == 0
    assert "Limitations" in page.inner_text("#report")
    page.locator("details summary").first.click()
    page.wait_for_selector(".shots img")
    assert page.eval_on_selector(".shots img", "i => i.complete && i.naturalWidth > 0")   # screenshot really served
    assert page.errors == []


def test_partial_approval_and_html_in_data_is_not_executed(page):
    page.click("#analyze")
    page.wait_for_selector("#scenarios tbody tr")
    for i in range(1, 5):
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
    page.fill("#policy", "Refunds above INR 99 require a smile.")
    page.click("#analyze")
    page.wait_for_function("document.querySelector('#analyze-msg').textContent.length > 0")
    assert "no cached output" in page.inner_text("#analyze-msg")
    assert page.is_hidden("#analysis")
