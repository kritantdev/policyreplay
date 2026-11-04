"""Docker sandbox runner. Same pipeline as local_e2e.py, but app and browser run in hardened,
network-isolated containers and the verifier runs on the HOST against copied snapshots.

  python orchestrator/sandbox_run.py build            # build both images
  python orchestrator/sandbox_run.py probe            # Chromium + containment probe (run this first)
  python orchestrator/sandbox_run.py run [--case NAME]

No API keys are passed to any container. Argument lists only; no shell. No docker.sock mount.
Host mounts: the read-only plan file and the run's evidence directory only.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_e2e import CASES  # noqa: E402
from verifier.make_fixtures import RULE, RULE_P05, scenario_dict, scenario_dict_p05  # noqa: E402
from verifier.verify import verify  # noqa: E402

APP_IMG, WK_IMG = "pr-target-app", "pr-worker"
SNAP_PY = ("import sqlite3,sys;c=sqlite3.connect('file:/data/app.db?mode=ro',uri=True);"
           "sys.stdout.buffer.write(c.serialize())")
READY_PY = "import urllib.request;urllib.request.urlopen('http://127.0.0.1:9000/health',timeout=1)"
PROBE_PY = r"""
import os, socket
r = {}
try:
    open('/x', 'w'); r['rootfs_write'] = 'ALLOWED (bad)'
except OSError as e:
    r['rootfs_write'] = 'blocked: ' + str(e.strerror)
for label, host in (('internet', '1.1.1.1'), ('metadata', '169.254.169.254')):
    try:
        socket.create_connection((host, 80), timeout=3); r[label] = 'ALLOWED (bad)'
    except OSError as e:
        r[label] = 'blocked: ' + str(e)
r['secret_like_env_vars'] = [k for k in os.environ if any(w in k.upper() for w in ('KEY', 'TOKEN', 'SECRET'))]
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(args=['--no-sandbox']); pg = b.new_page()
    pg.set_content('<h1>ok</h1>'); r['chromium'] = pg.inner_text('h1'); b.close()
print(r)
"""


def docker(args, timeout=60, text=True):
    return subprocess.run(["docker", *args], capture_output=True, text=text, timeout=timeout)


def harden(mem, pids, cpus):
    return ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", pids, "--memory", mem, "--cpus", cpus, "--user", "10001"]


def inspect_summary(name):
    try:
        j = json.loads(docker(["inspect", name]).stdout)[0]
        h = j["HostConfig"]
        return {"ReadonlyRootfs": h.get("ReadonlyRootfs"), "CapDrop": h.get("CapDrop"),
                "SecurityOpt": h.get("SecurityOpt"), "PidsLimit": h.get("PidsLimit"),
                "MemoryBytes": h.get("Memory"), "NanoCpus": h.get("NanoCpus"),
                "NetworkMode": h.get("NetworkMode"), "User": j["Config"].get("User")}
    except Exception:
        return {"error": "inspect unavailable"}


def inventory():
    return {"containers": docker(["ps", "-a", "--format", "{{.Names}}"]).stdout.split(),
            "networks": docker(["network", "ls", "--format", "{{.Name}}"]).stdout.split()}


def snapshot_db(app, dst):
    r = docker(["exec", app, "python", "-c", SNAP_PY], text=False)
    if r.returncode != 0:
        raise RuntimeError(f"snapshot failed: {r.stderr.decode(errors='replace')[:200]}")
    Path(dst).write_bytes(r.stdout)


def build():
    for args in (["build", "-t", APP_IMG, "target_app"],
                 ["build", "-t", WK_IMG, "-f", "worker/Dockerfile", "."]):
        if subprocess.run(["docker", *args], cwd=ROOT).returncode != 0:
            sys.exit("build failed")


def probe():
    net = f"pr-probe-{uuid.uuid4().hex[:6]}"
    docker(["network", "create", "--internal", net])
    try:
        r = docker(["run", "--rm", "--network", net, *harden("1g", "256", "1"),
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--shm-size", "256m",
                    "-e", "HOME=/tmp", "--entrypoint", "python", WK_IMG, "-c", PROBE_PY], timeout=120)
    finally:
        docker(["network", "rm", net])
    print(r.stdout or "", r.stderr or "")
    ok = r.returncode == 0 and "'chromium': 'ok'" in r.stdout
    print("PROBE", "OK: Chromium runs in the hardened container" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def run_case(policy, name, profile, amount, outdir):
    rid = uuid.uuid4().hex[:8]
    net, app, wk = f"pr-net-{rid}", f"app-{rid}", f"worker-{rid}"
    out = Path(outdir) / name
    shutil.rmtree(out, ignore_errors=True)
    ev = out / "evidence"
    ev.mkdir(parents=True)
    try:
        ev.chmod(0o777)  # container user 10001 must be able to write here
    except OSError:
        pass
    before = inventory()
    (out / "inventory_before.json").write_text(json.dumps(before, indent=2))
    summary = {"run_id": rid, "containment": {}, "error": None, "worker_exit": None}
    try:
        docker(["network", "create", "--internal", net])
        r = docker(["run", "-d", "--name", app, "--network", net, *harden("256m", "128", "0.5"),
                    "--tmpfs", "/data:rw,noexec,nosuid,size=64m,uid=10001,gid=10001",
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                    "-e", f"BUG_PROFILE={profile}", APP_IMG])
        if r.returncode != 0:
            raise RuntimeError("app container failed to start: " + r.stderr[:300])
        for _ in range(60):
            if docker(["exec", app, "python", "-c", READY_PY]).returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("target app did not become ready")
        summary["containment"]["network_internal"] = docker(
            ["network", "inspect", net, "--format", "{{.Internal}}"]).stdout.strip()
        summary["containment"]["app"] = inspect_summary(app)
        snapshot_db(app, out / "pre.db")

        sc = scenario_dict(name, amount) if policy == "P-01" else scenario_dict_p05(name)
        (out / "rule.json").write_text(json.dumps(RULE if policy == "P-01" else RULE_P05, indent=2))
        (out / "scenario.json").write_text(json.dumps(sc, indent=2))
        (out / "plan.json").write_text(json.dumps(sc["steps"]))

        c = docker(["create", "--name", wk, "--network", net, *harden("1g", "256", "1"),
                    "--tmpfs", "/tmp:rw,noexec,nosuid,size=256m", "--shm-size", "256m",
                    "-v", f"{(out / 'plan.json').resolve()}:/plan/plan.json:ro",
                    "-v", f"{ev.resolve()}:/out", WK_IMG,
                    "--base-url", f"http://{app}:9000", "--plan", "/plan/plan.json",
                    "--out", "/out", "--no-sandbox"])
        if c.returncode != 0:
            raise RuntimeError("worker create failed: " + c.stderr[:300])
        summary["containment"]["worker"] = inspect_summary(wk)
        try:
            w = docker(["start", "-a", wk], timeout=180)
            summary["worker_exit"] = w.returncode
            (out / "worker_output.txt").write_text((w.stdout or "") + (w.stderr or ""))
        except subprocess.TimeoutExpired:
            docker(["kill", wk])
            summary["worker_exit"] = "timeout"
        snapshot_db(app, out / "post.db")
    except Exception as e:
        summary["error"] = str(e)
    finally:
        docker(["rm", "-f", app, wk])
        docker(["network", "rm", net])
    after = inventory()
    (out / "inventory_after.json").write_text(json.dumps(after, indent=2))
    left = [n for n in after["containers"] + after["networks"] if rid in n]
    summary["cleanup_confirmed"] = not left
    summary["leftovers"] = left

    log = ev / "http_log.json"
    if log.exists():
        shutil.copy(log, out / "http_log.json")
    res = verify(out / "rule.json", out / "scenario.json", out / "pre.db", out / "post.db",
                 out / "http_log.json") if (out / "rule.json").exists() else {
        "verdict": "INCONCLUSIVE", "reasons": ["run failed before evidence was collected"]}
    res["sandbox"] = summary
    (out / "verdict.json").write_text(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "probe", "run"])
    ap.add_argument("--case")
    a = ap.parse_args()
    if a.cmd == "build":
        build()
    elif a.cmd == "probe":
        probe()
    else:
        for policy, name, profile, amount in CASES:
            if a.case and a.case != name:
                continue
            r = run_case(policy, name, profile, amount, ROOT / "runs" / "sandbox")
            s = r["sandbox"]
            print(f"{name:15} {r['verdict']:12} cleanup={s['cleanup_confirmed']} "
                  f"worker_exit={s['worker_exit']} {s['error'] or r['reasons'][0]}")
