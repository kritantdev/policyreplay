"""PolicyReplay orchestrator: Day-1 dispatch endpoint.

Runs a fixed command in a hardened, network-isolated container and confirms
cleanup. Argument lists only (never shell=True); model output never reaches here.
"""
import subprocess
import uuid

from fastapi import FastAPI

app = FastAPI(title="PolicyReplay Orchestrator")

HARDENING = [
    "--read-only", "--tmpfs", "/tmp",
    "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
    "--pids-limit", "128", "--memory", "256m", "--cpus", "0.5",
    "--user", "10001",
]


def sh(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def inventory(name, net):
    c = sh(["docker", "ps", "-a", "--filter", f"name={name}", "-q"]).stdout.strip()
    n = sh(["docker", "network", "ls", "--filter", f"name={net}", "-q"]).stdout.strip()
    return {"containers": c, "networks": n}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/dispatch")
def dispatch():
    rid = uuid.uuid4().hex[:8]
    net, name = f"pr-net-{rid}", f"worker-{rid}"
    result = {"run_id": rid, "output": "", "exit_code": None, "error": None}
    sh(["docker", "network", "create", "--internal", net])
    try:
        r = sh(["docker", "run", "--rm", "--name", name, "--network", net,
                *HARDENING, "alpine", "sh", "-c", "echo hello from sandbox"])
        result.update(output=r.stdout, exit_code=r.returncode)
    except subprocess.TimeoutExpired:
        result["error"] = "timeout"
    finally:
        sh(["docker", "rm", "-f", name])
        sh(["docker", "network", "rm", net])
    left = inventory(name, net)
    result["cleanup_confirmed"] = not left["containers"] and not left["networks"]
    return result
