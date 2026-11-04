"""Playwright worker. Executes a VALIDATED step plan from a fixed action vocabulary.

Model output never becomes a shell command or arbitrary code: the plan is data,
every step is checked against ALLOWED, navigation is pinned to the target origin.
Writes: trace.json, http_log.json, screenshots/, manifest.json (sha256 of each file).

  python -m worker.run_plan --base-url http://127.0.0.1:9000 --plan plan.json --out evidence/
Inside a hardened container Chromium needs --no-sandbox (the container is the boundary).
"""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROLE_USERS = {"support": "support1", "manager": "manager1", "admin": "admin1", "customer": "alice"}
SPEC = {  # action: (required keys, optional keys)
    "login": ({"role"}, set()), "goto": ({"path"}, set()),
    "fill": ({"testid", "value"}, set()), "click": ({"testid"}, set()),
    "select": ({"testid", "value"}, set()), "expect": ({"testid"}, {"text"}),
}
TESTID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_STEPS = 30


class PlanError(ValueError):
    pass


def validate_plan(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise PlanError(f"plan must be a list of 1-{MAX_STEPS} steps")
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or s.get("action") not in SPEC:
            raise PlanError(f"step {i}: action not in vocabulary {sorted(SPEC)}")
        req, opt = SPEC[s["action"]]
        keys = set(s) - {"action"}
        if not req <= keys or not keys <= req | opt:
            raise PlanError(f"step {i}: keys must be {sorted(req)} (+ optional {sorted(opt)})")
        for k in keys:
            if not isinstance(s[k], str) or len(s[k]) > 200:
                raise PlanError(f"step {i}: '{k}' must be a string of at most 200 chars")
        if "testid" in s and not TESTID.match(s["testid"]):
            raise PlanError(f"step {i}: invalid testid")
        if s["action"] == "login" and s["role"] not in ROLE_USERS:
            raise PlanError(f"step {i}: unknown role")
        if s["action"] == "goto":
            p = s["path"]
            if not p.startswith("/") or p.startswith("//") or "://" in p or "\\" in p:
                raise PlanError(f"step {i}: goto path must be a same-origin absolute path")
    return steps


def do_step(page, base, s):
    a = s["action"]
    if a == "login":
        page.goto(base + "/login")
        page.get_by_test_id("login-username").fill(ROLE_USERS[s["role"]])
        page.get_by_test_id("login-submit").click()
        page.wait_for_load_state()
    elif a == "goto":
        page.goto(base + s["path"])
    elif a == "fill":
        page.get_by_test_id(s["testid"]).fill(s["value"])
    elif a == "click":
        page.get_by_test_id(s["testid"]).click()
        page.wait_for_load_state()
    elif a == "select":
        page.get_by_test_id(s["testid"]).select_option(s["value"])
    elif a == "expect":
        loc = page.get_by_test_id(s["testid"])
        loc.wait_for(state="visible")
        if "text" in s and s["text"] not in loc.inner_text():
            raise AssertionError(f"expected text {s['text']!r} in {s['testid']}")


def run(base_url, steps, out, no_sandbox=False, step_timeout_ms=5000):
    from playwright.sync_api import sync_playwright
    validate_plan(steps)
    out = Path(out)
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    base = base_url.rstrip("/")
    origin = urlparse(base)
    http_log, trace, blocked = [], [], []
    status = "completed"

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"] if no_sandbox else [])
        page = browser.new_context().new_page()
        page.set_default_timeout(step_timeout_ms)

        def same_origin(url):
            u = urlparse(url)
            return (u.scheme, u.netloc) == (origin.scheme, origin.netloc)

        def route(r):
            if same_origin(r.request.url):
                r.continue_()
            else:
                blocked.append(r.request.url)
                r.abort()

        def on_response(resp):
            req = resp.request
            if not same_origin(req.url):
                return
            e = {"method": req.method, "path": urlparse(req.url).path, "status": resp.status,
                 "ts": round(time.time(), 3)}
            if req.method == "POST" and req.post_data:
                e["form"] = {k: v[0] for k, v in parse_qs(req.post_data).items()}
            http_log.append(e)

        page.route("**/*", route)
        page.on("response", on_response)

        for i, s in enumerate(steps, 1):
            ok, err = True, None
            try:
                do_step(page, base, s)
            except Exception as e:  # record and stop; never continue past a failed step
                ok, err = False, str(e).splitlines()[0][:300]
            shot = f"screenshots/{i:02d}_{s['action']}.png"
            try:
                page.screenshot(path=str(out / shot))
            except Exception:
                shot = None
            trace.append({"step": i, **s, "ok": ok, "error": err, "screenshot": shot,
                          "ts": round(time.time(), 3)})
            if not ok:
                status = "step_failed"
                break
        browser.close()

    (out / "trace.json").write_text(json.dumps(trace, indent=2))
    (out / "http_log.json").write_text(json.dumps(http_log, indent=2))
    manifest = {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()
                for f in sorted(out.rglob("*")) if f.is_file() and f.name != "manifest.json"}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return {"status": status, "steps": len(trace), "blocked_requests": blocked}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--plan", required=True, help="JSON list of steps, or an object with 'steps'")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-sandbox", action="store_true")
    a = ap.parse_args()
    plan = json.loads(Path(a.plan).read_text())
    steps = plan["steps"] if isinstance(plan, dict) else plan
    try:
        res = run(a.base_url, steps, a.out, a.no_sandbox)
    except PlanError as e:
        print(json.dumps({"status": "rejected", "error": str(e)}))
        sys.exit(2)
    print(json.dumps(res))
    sys.exit(0 if res["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
