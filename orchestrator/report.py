"""Evidence report assembly (read-only view over the files a run produced)."""
import json
from pathlib import Path

LIMITATIONS = [
    "This tests the listed scenarios only. A PASS means the tested attempt was blocked; it is not a "
    "compliance certification of the application.",
    "Evidence for P-01, P-02, P-03, P-04 and P-05 includes audit rows maintained by the application under test; "
    "each verdict also requires the browser-observed HTTP status to agree with them.",
    "Chromium's own sandbox is disabled (--no-sandbox); the container is the isolation boundary. Docker is "
    "not a complete boundary against kernel-level exploits: the threat model is an untrusted target app "
    "and browser, not a kernel-exploit attacker.",
    "Rules and scenarios marked 'replayed model output' were cached, not produced live by the model.",
]


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def case_report(job, case, runs_dir):
    cid = case["case_id"]
    d = Path(runs_dir) / job.id / cid
    res = job.results.get(cid)
    rule, sc = case["rule"], case["scenario"]
    rep = {
        "case_id": cid, "test_id": case["test_id"], "profile": case["profile"],
        "policy_clause": rule["source_clause"], "rule": rule, "scenario": sc,
        "verdict": res["verdict"] if res else None,
        "reasons": res["reasons"] if res else [],
        "checks": res.get("checks", {}) if res else {},
        "observed": res.get("observed", {}) if res else {},
        "artifact_sha256": res.get("artifact_sha256", {}) if res else {},
        "manifest_sha256": _json(d / "evidence" / "manifest.json") or {},
        "trace": [], "environment": None,
    }
    for s in _json(d / "evidence" / "trace.json") or []:
        shot = s.get("screenshot")
        rep["trace"].append({**s, "screenshot": (
            f"/api/runs/{job.id}/cases/{cid}/file/evidence/{shot}" if shot else None)})
    sb = (res or {}).get("sandbox")
    rep["environment"] = ({"mode": "docker sandbox", **sb} if sb else
                          {"mode": job.mode_label, "note": "no container lifecycle evidence in this mode"})
    return rep


def run_report(job, runs_dir):
    return {
        "run_id": job.id, "status": job.status, "execution_mode": job.mode_label,
        "model_output": job.sources, "human_approved_at": job.approved_at,
        "summary": job.summary, "error": job.error,
        "cases": [case_report(job, c, runs_dir) for c in job.cases],
        "limitations": LIMITATIONS,
    }
