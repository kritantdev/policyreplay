"""Docker sandbox runner. Same pipeline as local_e2e.py, but app and browser run in hardened,
network-isolated containers and the verifier runs on the HOST against copied snapshots.

  python orchestrator/sandbox_run.py build            # build both images
  python orchestrator/sandbox_run.py probe            # Chromium + containment probe (run this first)
  python orchestrator/sandbox_run.py run [--case NAME]

No API keys are passed to any container. Argument lists only; no shell. No docker.sock mount.
Host mounts: the read-only plan file and the run's evidence directory only.
"""
import argparse
import ast
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
try:
    from orchestrator.local_e2e import CASES  # noqa: E402
except ImportError:
    from local_e2e import CASES  # noqa: E402
from verifier.make_fixtures import case_files  # noqa: E402
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
BASE_IMAGE_PUBLIC = {'GPG_KEY'}  # public GPG key ID shipped in the official python image, not a credential
r['env_var_names'] = sorted(os.environ)
r['secret_like_env_vars'] = [k for k in os.environ if k not in BASE_IMAGE_PUBLIC
                             and any(w in k.upper() for w in ('KEY', 'TOKEN', 'SECRET'))]
r['vultr_vars'] = [k for k in os.environ if k.upper().startswith('VULTR')]
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


def probe_ok(returncode, stdout):
    """The probe must PROVE each claim, not just launch Chromium."""
    try:
        d = ast.literal_eval(stdout.strip().splitlines()[-1])
    except (ValueError, SyntaxError, IndexError):
        return False
    blocked = lambda k: str(d.get(k, "")).startswith("blocked")  # noqa: E731
    return (returncode == 0 and d.get("chromium") == "ok" and blocked("rootfs_write")
            and blocked("internet") and blocked("metadata")
            and not d.get("secret_like_env_vars") and not d.get("vultr_vars"))


STRESS_MEM = "b = []\nwhile True:\n    b.append(bytearray(10_000_000))"
STRESS_FORK = """
import os, time
n = 0
try:
    while True:
        if os.fork() == 0:
            time.sleep(30); os._exit(0)
        n += 1
except OSError as e:
    print(f'fork blocked after {n} children: {e}')
"""


def stress():
    """v3 containment demos: memory hog is killed (exit 137), fork loop is capped by --pids-limit."""
    out = {}
    mem = f"pr-mem-{uuid.uuid4().hex[:6]}"
    docker(["run", "--name", mem, "--network", "none", *harden("128m", "64", "0.5"),
            "--memory-swap", "128m", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
            "--entrypoint", "python", WK_IMG, "-c", STRESS_MEM], timeout=120)
    st = docker(["inspect", "-f", "{{.State.ExitCode}} {{.State.OOMKilled}}", mem]).stdout.split()
    out["memory_hog"] = {"exit_code": st[0] if st else "?", "oom_killed": st[1] if len(st) > 1 else "?",
                         "limit": "128m"}
    fork = f"pr-fork-{uuid.uuid4().hex[:6]}"
    r = docker(["run", "--name", fork, "--network", "none", *harden("256m", "32", "0.5"),
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m", "--entrypoint", "python", WK_IMG,
                "-c", STRESS_FORK], timeout=120)
    out["fork_loop"] = {"output": r.stdout.strip(), "pids_limit": 32}
    docker(["rm", "-f", mem, fork])
    left = [n for n in inventory()["containers"] if n in (mem, fork)]
    out["cleanup_confirmed"] = not left
    print(json.dumps(out, indent=2))
    ok = (out["memory_hog"]["exit_code"] == "137" and out["memory_hog"]["oom_killed"] == "true"
          and "fork blocked" in out["fork_loop"]["output"] and out["cleanup_confirmed"])
    print("STRESS", "OK: memory hog killed (137), fork loop capped, nothing left behind" if ok else "FAILED")
    sys.exit(0 if ok else 1)


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
    ok = probe_ok(r.returncode, r.stdout)
    print("PROBE", "OK: Chromium runs in the hardened container" if ok else "FAILED")
    sys.exit(0 if ok else 1)


def run_scenario(rule, sc, profile, out, on_event=None):
    emit = (lambda t, **kw: on_event({"type": t, **kw})) if on_event else (lambda t, **kw: None)
    rid = uuid.uuid4().hex[:8]
    net, app, wk = f"pr-net-{rid}", f"app-{rid}", f"worker-{rid}"
    out = Path(out)
    shutil.rmtree(out, ignore_errors=True)
    if out.exists():
        # The worker writes as uid 10001, so the host user cannot always delete a previous run's folder.
        # Never crash on that: keep the old evidence untouched and write this run to a fresh folder.
        out = out.with_name(f"{out.name}-{rid}")
        emit("stage", text=f"previous run folder could not be deleted; writing to {out.name}")
    ev = out / "evidence"
    ev.mkdir(parents=True)
    try:
        ev.chmod(0o777)  # container user 10001 must be able to write here
    except OSError:
        pass
    before = inventory()
    (out / "inventory_before.json").write_text(json.dumps(before, indent=2))
    summary = {"run_id": rid, "containment": {}, "error": None, "worker_exit": None, "evidence_dir": str(out)}
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
        emit("stage", text="isolated network and target app container ready")
        summary["containment"]["network_internal"] = docker(
            ["network", "inspect", net, "--format", "{{.Internal}}"]).stdout.strip()
        summary["containment"]["app"] = inspect_summary(app)
        snapshot_db(app, out / "pre.db")
        emit("stage", text="pre-run database snapshot copied out")

        (out / "rule.json").write_text(json.dumps(rule, indent=2))
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
        emit("stage", text="browser agent executing in the hardened sandbox")
        try:
            w = docker(["start", "-a", wk], timeout=180)
            summary["worker_exit"] = w.returncode
            (out / "worker_output.txt").write_text((w.stdout or "") + (w.stderr or ""))
        except subprocess.TimeoutExpired:
            docker(["kill", wk])
            summary["worker_exit"] = "timeout"
        snapshot_db(app, out / "post.db")
        emit("stage", text="post-run database snapshot copied out")
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
    emit("stage", text="containers and network destroyed; cleanup " + ("confirmed" if not left else "FAILED"))

    log = ev / "http_log.json"
    if log.exists():
        shutil.copy(log, out / "http_log.json")
    res = verify(out / "rule.json", out / "scenario.json", out / "pre.db", out / "post.db",
                 out / "http_log.json") if (out / "rule.json").exists() else {
        "verdict": "INCONCLUSIVE", "reasons": ["run failed before evidence was collected"]}
    res["sandbox"] = summary
    (out / "verdict.json").write_text(json.dumps(res, indent=2))
    return res


def run_case(c, outdir):
    rule, sc = case_files(c["policy"], c["name"], c["params"])
    return run_scenario(rule, sc, c["profile"], Path(outdir) / c["name"])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["build", "probe", "stress", "run"])
    ap.add_argument("--case")
    a = ap.parse_args()
    if a.cmd == "build":
        build()
    elif a.cmd == "probe":
        probe()
    elif a.cmd == "stress":
        stress()
    else:
        for c in CASES:
            if a.case and a.case != c["name"]:
                continue
            r = run_case(c, ROOT / "runs" / "sandbox")
            s = r["sandbox"]
            ok = "OK" if r["verdict"] == c["expected"] else "MISMATCH"
            print(f"{c['name']:15} {r['verdict']:12} {ok:8} cleanup={s['cleanup_confirmed']} "
                  f"worker_exit={s['worker_exit']} {s['error'] or r['reasons'][0]}")
