"""Local end-to-end run WITHOUT Docker: app -> pre snapshot -> Playwright worker -> post snapshot
-> host-side verifier. Proves the pipeline logic; the Docker/sandbox topology is sandbox_run.py.

  python orchestrator/local_e2e.py [--no-sandbox]
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verifier.make_fixtures import case_files, snapshot  # noqa: E402
from verifier.verify import verify  # noqa: E402


def case(policy, name, profile, expected, **params):
    return {"policy": policy, "name": name, "profile": profile, "expected": expected, "params": params}


CASES = [
    case("P-01", "buggy_15000", "buggy", "VIOLATION", amount=15000),
    case("P-01", "correct_15000", "correct", "PASS", amount=15000),
    case("P-01", "correct_10000", "correct", "PASS", amount=10000),
    case("P-05", "buggy_p05", "buggy", "VIOLATION"),
    case("P-05", "correct_p05", "correct", "PASS"),
    case("P-02", "buggy_p02", "buggy", "VIOLATION", actor="support", target=1, new_role="admin"),
    case("P-02", "correct_p02", "correct", "PASS", actor="support", target=1, new_role="admin"),
    case("P-02", "admin_ctl_p02", "correct", "PASS", actor="admin", target=4, new_role="manager"),
]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(c, outdir, no_sandbox=False):
    name, profile = c["name"], c["profile"]
    out = Path(outdir) / name
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    port, live = free_port(), out / "live.db"
    env = dict(os.environ, BUG_PROFILE=profile, DB_PATH=str(live), PR_AUTOSTART="1")
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "app:app", "--port", str(port),
                            "--app-dir", str(ROOT / "target_app"), "--log-level", "warning"], env=env)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("target app did not start")
        snapshot(live, out / "pre.db")
        rule, sc = case_files(c["policy"], name, c["params"])
        (out / "rule.json").write_text(json.dumps(rule, indent=2))
        (out / "scenario.json").write_text(json.dumps(sc, indent=2))
        (out / "plan.json").write_text(json.dumps(sc["steps"]))
        cmd = [sys.executable, "-m", "worker.run_plan", "--base-url", f"http://127.0.0.1:{port}",
               "--plan", str(out / "plan.json"), "--out", str(out / "evidence")]
        if no_sandbox:
            cmd.append("--no-sandbox")
        w = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        snapshot(live, out / "post.db")
    finally:
        srv.terminate()
        srv.wait(timeout=10)
    shutil.copy(out / "evidence" / "http_log.json", out / "http_log.json")
    res = verify(out / "rule.json", out / "scenario.json", out / "pre.db", out / "post.db",
                 out / "http_log.json")
    res["worker"] = {"exit_code": w.returncode, "stdout": w.stdout.strip()[-300:]}
    (out / "verdict.json").write_text(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sandbox", action="store_true",
                    default=os.environ.get("PR_NO_SANDBOX") == "1")
    a = ap.parse_args()
    bad = 0
    for c in CASES:
        r = run(c, ROOT / "runs" / "e2e", a.no_sandbox)
        ok = r["verdict"] == c["expected"]
        bad += not ok
        print(f"{c['name']:15} {r['verdict']:12} {'OK' if ok else 'MISMATCH (expected ' + c['expected'] + ')'}  {r['reasons'][0]}")
    sys.exit(1 if bad else 0)
