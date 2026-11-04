"""Local end-to-end run WITHOUT Docker: app -> pre snapshot -> Playwright worker -> post snapshot
-> host-side verifier. Proves the pipeline logic; the Docker/sandbox topology comes with the VM.

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
from verifier.make_fixtures import RULE, RULE_P05, scenario_dict, scenario_dict_p05, snapshot  # noqa: E402
from verifier.verify import verify  # noqa: E402


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


CASES = [("P-01", "buggy_15000", "buggy", 15000), ("P-01", "correct_15000", "correct", 15000),
         ("P-01", "correct_10000", "correct", 10000),
         ("P-05", "buggy_p05", "buggy", None), ("P-05", "correct_p05", "correct", None)]


def run(policy, name, profile, amount, outdir, no_sandbox=False):
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
        sc = scenario_dict(name, amount) if policy == "P-01" else scenario_dict_p05(name)
        (out / "rule.json").write_text(json.dumps(RULE if policy == "P-01" else RULE_P05, indent=2))
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
    for policy, name, profile, amount in CASES:
        r = run(policy, name, profile, amount, ROOT / "runs" / "e2e", a.no_sandbox)
        print(f"{name:15} {r['verdict']:12} {r['reasons'][0]}")
