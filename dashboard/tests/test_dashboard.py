import base64
import json
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
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
N_BLOCKED = sum(x["expected_outcome"] == "blocked" for x in SCENARIOS)       # one adversarial scenario per policy
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
LAUNCH = {"args": ["--no-sandbox"]} if os.environ.get("PR_NO_SANDBOX") == "1" else {}

CONTAINMENT = {
    "network_internal": "true",
    "app": {"ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "PidsLimit": 128,
            "MemoryBytes": 268435456, "NanoCpus": 500000000, "NetworkMode": "pr-net-x", "User": "10001"},
    "worker": {"ReadonlyRootfs": True, "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"], "PidsLimit": 256,
               "MemoryBytes": 1073741824, "NanoCpus": 1000000000, "NetworkMode": "pr-net-x", "User": "10001"},
}


def make_runner(sandbox=False, fp_for=None, miss_for=None, inconclusive_for=None, delay=0.05):
    """A stand-in for the pipeline that writes the same artifact layout and a chosen verdict pattern."""
    def runner(rule, sc, profile, out, on_event):
        out = Path(out)
        (out / "evidence" / "screenshots").mkdir(parents=True, exist_ok=True)
        (out / "evidence" / "screenshots" / "01_login.png").write_bytes(PNG)
        (out / "evidence" / "trace.json").write_text(json.dumps([
            {"step": 1, "action": "login", "role": "support", "ok": True, "error": None,
             "screenshot": "screenshots/01_login.png", "ts": 1}]))
        (out / "evidence" / "manifest.json").write_text(json.dumps({"screenshots/01_login.png": "ab" * 32}))
        on_event({"type": "stage", "text": f"fake run on {profile}"})
        time.sleep(delay)
        tid = sc["test_id"]
        v = "VIOLATION" if profile == "buggy" and sc["expected_outcome"] == "blocked" and tid != miss_for else "PASS"
        if fp_for == tid and profile == "correct":
            v = "VIOLATION"
        if inconclusive_for == tid and profile == "correct":
            v = "INCONCLUSIVE"
        res = {"verdict": v, "reasons": ["fake reason <b>x</b>"], "checks": {"attempt_logged": True},
               "observed": {"k": 1}, "artifact_sha256": {"post.db": "cd" * 32}}
        if sandbox:
            res["sandbox"] = {"cleanup_confirmed": True, "containment": CONTAINMENT, "leftovers": [], "worker_exit": 0}
        return res
    return runner


@contextmanager
def serve(app):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True


def api(tmp, runner, label="test mode", transport=None):
    return create_api(runs_dir=tmp, runner=runner, mode_label=label, transport=transport,
                      limits={"analyze": (10_000, 3600), "run": (10_000, 3600)})


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    with serve(api(tmp_path_factory.mktemp("local"), make_runner())) as url:
        yield url


@pytest.fixture(scope="module")
def sites(tmp_path_factory):
    """One server per scenario of interest: the headline must be right in every one of them."""
    variants = {
        "ok": make_runner(sandbox=True),
        "false_positive": make_runner(sandbox=True, fp_for="P-03-ADV-001"),
        "missed": make_runner(sandbox=True, miss_for="P-03-ADV-001"),
        "inconclusive": make_runner(sandbox=True, inconclusive_for="P-04-ADV-001"),
        "slow": make_runner(sandbox=True, delay=0.45),
    }
    ctxs, urls = [], {}
    for name, runner in variants.items():
        c = serve(api(tmp_path_factory.mktemp(name), runner, label="docker sandbox"))
        urls[name] = c.__enter__()
        ctxs.append(c)
    yield urls
    for c in ctxs:
        c.__exit__(None, None, None)


@pytest.fixture(scope="module")
def site_unsupported(tmp_path_factory):
    """A server whose (stubbed) model leaves one clause unsupported: keeps the 'not tested' UI covered."""
    rules = json.loads(json.dumps(GOLD_RULES))
    rules["rules"] = [r for r in rules["rules"] if r["rule_id"] != "P-04"]
    rules["unsupported_clauses"] = ["Discounts cannot exceed 30%."]
    sc = [x for x in SCENARIOS if x["rule_id"] != "P-04"]

    def model(messages, name):
        return json.dumps(rules if messages[0]["content"].startswith("You extract") else {"scenarios": sc})
    with serve(api(tmp_path_factory.mktemp("unsup"), make_runner(), transport=model)) as url:
        yield url


@contextmanager
def browser_page(url, viewport=None):
    with sync_playwright() as p:
        b = p.chromium.launch(**LAUNCH)
        pg = b.new_page(**({"viewport": viewport} if viewport else {}))
        pg.errors = []
        pg.on("pageerror", lambda e: pg.errors.append(str(e)))
        pg.goto(url)
        try:
            yield pg
        finally:
            b.close()


def go(pg, keep=None, wait=True):
    """Analyze, approve (all, or only the scenario ids in `keep`), run, optionally wait for the report."""
    pg.wait_for_function("document.querySelector('#policy').value.length > 0")
    pg.click("#analyze")
    pg.wait_for_selector("#rules .rule")
    if keep is not None:
        for cb in pg.locator(".pick").all():
            if cb.get_attribute("value") not in keep:
                cb.uncheck()
    pg.click("#approve")
    pg.wait_for_function("!document.querySelector('#run').disabled")
    pg.click("#run")
    if wait:
        pg.wait_for_selector("#headline-report", state="attached", timeout=30000)


def no_hscroll(pg):
    return pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


# ------------------------------------------------------------------ flows ---

def test_full_flow_through_the_ui(site):
    with browser_page(site) as pg:
        assert pg.inner_text("#mode") == "test mode"
        pg.wait_for_function("document.querySelector('#policy').value.length > 0")
        assert "Refunds above INR 10,000" in pg.input_value("#policy")
        assert pg.is_disabled("#run")                                         # cannot run before approval
        pg.click("#analyze")
        pg.wait_for_selector("#rules .rule")
        assert pg.locator("#rules .rule").count() == N_RULES
        assert pg.locator("#scenarios tbody tr").count() == N_SCENARIOS
        assert "replayed model output" in pg.inner_text("#src")               # labeled, not passed off as live
        assert pg.inner_text("#unsupported").strip() == ""                    # sample policy: all five clauses tested
        assert pg.is_disabled("#run")
        pg.click("#approve")
        pg.wait_for_function("!document.querySelector('#run').disabled")
        pg.click("#run")
        pg.wait_for_selector("#headline-report", state="attached", timeout=30000)
        assert (N_RULES, N_SCENARIOS, N_BLOCKED) == (5, 9, 5)
        assert pg.locator("#cases .case").count() == 2 * N_SCENARIOS == 18
        head = pg.inner_text("#headline")
        assert "5 of 5 policies: planted violation detected · 0 false positives" in head
        assert pg.get_attribute("#headline", "class").split()[-1] == "warn"   # local mode: cleanup not applicable
        assert "cleanup not applicable in local mode" in head
        tiles = pg.inner_text("#summary")
        assert "planted violations detected" in tiles and "5 of 5" in tiles
        pg.click("#open-report")
        assert pg.get_attribute("#tabs button[data-tab='3']", "aria-selected") == "true"
        pg.wait_for_selector(".banner")
        assert "The policy said no. The application said yes." in pg.inner_text(".banner")
        assert pg.locator("#report .badge.VIOLATION").count() >= N_BLOCKED
        assert pg.locator("#report .badge.INCONCLUSIVE").count() == 0
        assert pg.locator("#report .matrix tbody tr:not(.grp)").count() == N_SCENARIOS
        assert "Limitations" in pg.inner_text("#report")
        pg.locator("#report .build details summary").first.click()
        pg.wait_for_selector("#report .shots img")
        assert pg.eval_on_selector("#report .shots img", "i => i.complete && i.naturalWidth > 0")   # really served
        assert pg.errors == []


@pytest.mark.parametrize("variant,state_class,text,sub", [
    ("ok", "ok", "5 of 5 policies: planted violation detected · 0 false positives", "all environments destroyed"),
    ("false_positive", "bad", "5 of 5 policies: planted violation detected · 1 false positive", None),
    ("missed", "bad", "4 of 5 policies: planted violation detected · 0 false positives", None),
    ("inconclusive", "warn", "5 of 5 policies: planted violation detected · 0 false positives", "1 inconclusive"),
])
def test_headline_state_is_right_and_identical_on_both_screens(sites, variant, state_class, text, sub):
    with browser_page(sites[variant]) as pg:
        go(pg)
        for sel in ("#headline", "#headline-report .headline"):                # live screen and report must agree
            cls = pg.get_attribute(sel, "class").split()[-1]
            assert cls == state_class, (sel, cls)
            assert text in pg.inner_text(sel)
            if sub:
                assert sub in pg.inner_text(sel)
        assert pg.errors == []


def test_containment_evidence_is_shown_in_the_report(sites):
    with browser_page(sites["ok"]) as pg:
        go(pg)
        pg.click("#open-report")
        pg.locator("#report .build details summary").first.click()
        body = pg.inner_text("#report")
        for needle in ("Container hardening", "read-only", "all dropped", "blocked (no-new-privileges)",
                       "128", "256", "256 MB", "1024 MB", "0.5 CPU", "1 CPU", "10001",
                       "internal only, no external egress", "containers left behind: none"):
            assert needle in body, needle
        assert "cleanup confirmed" in body.replace("cleanup\nconfirmed", "cleanup confirmed")


def test_live_case_logs_are_collapsible(sites):
    with browser_page(sites["slow"]) as pg:
        go(pg, keep={"P-01-ADV-001", "P-01-BND-001", "P-05-ADV-001"}, wait=False)   # 3 scenarios = 6 cases
        pg.wait_for_function("/^Running case \\d+ of 6/.test(document.querySelector('#headline .t').textContent)")
        pg.wait_for_function("document.querySelectorAll('#cases .badge:not(.PENDING)').length >= 2")
        assert pg.locator("#cases details.case[open]").count() <= 1           # finished cases close themselves
        first = pg.locator("#cases details.case").first
        first.locator("summary").click()                                       # the user opens a finished case by hand
        assert first.evaluate("d => d.open")
        pg.wait_for_selector("#headline-report", state="attached", timeout=30000)
        assert first.evaluate("d => d.open")                                   # and it stays open
        assert pg.locator("#cases details.case[open]").count() == 1
        pg.click("#expand-all")
        assert pg.locator("#cases details.case[open]").count() == 6
        pg.click("#collapse-all")
        assert pg.locator("#cases details.case[open]").count() == 0
        assert pg.errors == []


def test_matrix_cell_opens_the_matching_evidence(site):
    with browser_page(site) as pg:
        go(pg)
        cid = "P-03-ADV-001__buggy"
        pg.click(f"#matrix-live .cell[data-case='{cid}']")
        assert pg.get_attribute("#tabs button[data-tab='3']", "aria-selected") == "true"
        pg.wait_for_function(f"document.getElementById('ev-{cid}').open")
        assert pg.locator("#report .matrix .cell").count() == 2 * N_SCENARIOS
        assert pg.errors == []


def test_partial_approval_changes_the_denominator_and_html_in_data_is_not_executed(site):
    with browser_page(site) as pg:
        go(pg, keep={"P-05-ADV-001"})
        assert pg.locator("#cases .case").count() == 2
        assert "1 of 1 policy: planted violation detected" in pg.inner_text("#headline")
        pg.click("#open-report")
        assert "<b>x</b>" in pg.inner_text("#report")                           # shown as text...
        assert pg.evaluate("[...document.querySelectorAll('#report b')].every(b => b.textContent !== 'x')")   # ...never parsed
        assert pg.errors == []


def test_api_error_is_shown_not_swallowed(site):
    with browser_page(site) as pg:
        pg.wait_for_function("document.querySelector('#policy').value.length > 0")
        pg.fill("#policy", "Refunds above INR 99 require a smile.")
        pg.click("#analyze")
        pg.wait_for_function("document.querySelector('#analyze-msg').textContent.length > 0")
        assert "no cached output" in pg.inner_text("#analyze-msg")
        assert pg.is_hidden("#analysis")


def test_unsupported_clause_is_shown_as_not_tested(site_unsupported):
    with browser_page(site_unsupported) as pg:
        pg.click("#analyze")
        pg.wait_for_selector("#rules .rule")
        assert pg.locator("#rules .rule").count() == N_RULES - 1
        assert "no verifier yet" in pg.inner_text("#unsupported")
        assert "Discounts cannot exceed 30%" in pg.inner_text("#unsupported")
        assert "Live model output" in pg.inner_text("#src")
        assert pg.errors == []


def test_text_typed_before_the_initial_policy_fetch_is_not_overwritten(site):
    with sync_playwright() as p:
        b = p.chromium.launch(**LAUNCH)
        pg = b.new_page()
        pg.route("**/api/policy", lambda r: (time.sleep(0.5), r.continue_())[1])      # a slow network
        pg.goto(site)
        pg.fill("#policy", "Typed by the user before the page finished loading.")
        pg.wait_for_timeout(1500)
        assert pg.input_value("#policy") == "Typed by the user before the page finished loading."
        b.close()


# ----------------------------------------------------------- layout, a11y ---

def test_phone_layout_never_scrolls_sideways(sites):
    with browser_page(sites["ok"], viewport={"width": 390, "height": 844}) as pg:
        assert no_hscroll(pg)
        pg.wait_for_function("document.querySelector('#policy').value.length > 0")
        pg.click("#analyze")
        pg.wait_for_selector("#rules .rule")
        assert no_hscroll(pg)                                                  # rules and scenarios tables
        go(pg)
        assert no_hscroll(pg)                                                  # live screen with matrix
        pg.click("#open-report")
        assert no_hscroll(pg)                                                  # report
        pg.locator("#report .build details summary").first.click()
        assert no_hscroll(pg)                                                  # expanded evidence with wide tables
        assert pg.errors == []


def test_accessibility_basics(site):
    with browser_page(site) as pg:
        assert pg.get_attribute("html", "lang") == "en"
        assert pg.get_attribute("#tabs", "role") == "tablist"
        assert pg.get_attribute("#headline", "aria-live") == "polite"
        assert pg.locator("link[rel='icon']").count() == 1
        pg.click("#tabs button[data-tab='2']")
        assert pg.get_attribute("#tabs button[data-tab='2']", "aria-selected") == "true"
        assert pg.get_attribute("#tabs button[data-tab='1']", "aria-selected") == "false"
        pg.click("#tabs button[data-tab='1']")                                 # back to the screen with the controls
        go(pg)
        pg.click("#open-report")
        pg.locator("#report .build details summary").first.click()
        assert pg.evaluate("[...document.querySelectorAll('img')].every(i => i.alt && i.alt.length > 0)")
        pg.click("#tabs button[data-tab='1']")
        assert pg.evaluate("[...document.querySelectorAll('.pick')].every(c => c.getAttribute('aria-label'))")


# --------------------------------------------- defects found in visual QA ---

def test_report_button_stays_hidden_until_the_run_finishes(sites):
    """Regression: a .btn display rule once overrode the hidden attribute, so it showed mid-run."""
    with browser_page(sites["slow"]) as pg:
        go(pg, keep={"P-01-ADV-001", "P-05-ADV-001"}, wait=False)             # 4 cases, about 2 s
        pg.wait_for_selector("#cases .case")
        assert pg.is_hidden("#open-report")
        pg.wait_for_selector("#headline-report", state="attached", timeout=30000)
        pg.wait_for_function("!document.querySelector('#open-report').hidden")
        assert pg.is_visible("#open-report")


def test_rules_and_scenarios_are_listed_in_policy_order_and_ids_do_not_wrap(site):
    with browser_page(site, viewport={"width": 1280, "height": 900}) as pg:
        pg.wait_for_function("document.querySelector('#policy').value.length > 0")
        pg.click("#analyze")
        pg.wait_for_selector("#rules .rule")
        assert pg.eval_on_selector_all("#rules .rule td:first-child", "els => els.map(e => e.textContent)") == \
            ["P-01", "P-02", "P-03", "P-04", "P-05"]
        ids = pg.eval_on_selector_all(".pick", "els => els.map(e => e.value)")
        assert ids == sorted(ids) and ids[0] == "P-01-ADV-001" and ids[-1] == "P-05-ADV-001"
        # measure the ID text itself (a cell stretches to its row): one line is about 18 px, a wrapped ID about 36 px
        assert pg.evaluate("[...document.querySelectorAll('#scenarios td.sid b')].every(b => b.getBoundingClientRect().height < 24)")


def test_timestamps_are_readable(site):
    import re
    pattern = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC")
    with browser_page(site) as pg:
        pg.wait_for_function("document.querySelector('#policy').value.length > 0")
        pg.click("#analyze")
        pg.wait_for_selector("#rules .rule")
        pg.click("#approve")
        pg.wait_for_function("!document.querySelector('#run').disabled")
        msg = pg.inner_text("#approve-msg")                                   # screen 1 approval message
        assert pattern.search(msg) and "Z UTC" not in msg
        pg.click("#run")
        pg.wait_for_selector("#headline-report", state="attached", timeout=30000)
        pg.click("#open-report")
        text = pg.inner_text("#report")                                       # report: "Human approval"
        assert pattern.search(text) and "Z UTC" not in text


def test_phone_tables_restack_and_all_three_tabs_fit(site):
    with browser_page(site, viewport={"width": 390, "height": 844}) as pg:
        pg.wait_for_function("document.querySelector('#policy').value.length > 0")
        pg.click("#analyze")
        pg.wait_for_selector("#rules .rule")
        assert pg.evaluate("getComputedStyle(document.querySelector('#scenarios thead')).display") == "none"
        assert pg.evaluate("getComputedStyle(document.querySelector('#scenarios tbody tr')).display") == "block"
        assert pg.evaluate("Math.max(...[...document.querySelectorAll('#tabs button')].map(b => b.getBoundingClientRect().right))") <= 391
        label = pg.evaluate("getComputedStyle(document.querySelector('#scenarios td[data-label=\"Steps\"]'), '::before').content")
        assert label == '"Steps: "'                                             # stacked cells carry a visible label
