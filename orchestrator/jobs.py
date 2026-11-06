"""Run jobs: one at a time, with an ordered event log that the SSE endpoint streams."""
import json
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

MAX_JOBS = 20


class Job:
    def __init__(self, analysis_id, cases, mode_label, sources, approved_at):
        self.id = uuid.uuid4().hex[:12]
        self.analysis_id, self.cases = analysis_id, cases      # cases: [{case_id,test_id,profile,rule,scenario}]
        self.mode_label, self.sources, self.approved_at = mode_label, sources, approved_at
        self.status, self.summary, self.error = "queued", None, None
        self.events, self.results = [], {}
        self.lock = threading.Lock()
        self.created = time.time()

    def emit(self, type_, **data):
        with self.lock:
            self.events.append({"i": len(self.events), "ts": round(time.time(), 3), "type": type_, **data})

    def snapshot_events(self, after=0):
        with self.lock:
            return list(self.events[after:]), self.status in ("finished", "failed")


class JobManager:
    def __init__(self):
        self.jobs = OrderedDict()
        self._slot = threading.Semaphore(1)      # one concurrent run, ever
        self._lock = threading.Lock()

    def start(self, job, runner, runs_dir):
        if not self._slot.acquire(blocking=False):
            return False
        with self._lock:
            self.jobs[job.id] = job
            while len(self.jobs) > MAX_JOBS:
                self.jobs.popitem(last=False)
        threading.Thread(target=self._run, args=(job, runner, runs_dir), daemon=True).start()
        return True

    def get(self, job_id):
        with self._lock:
            return self.jobs.get(job_id)

    def _run(self, job, runner, runs_dir):
        try:
            execute(job, runner, runs_dir)
        finally:
            self._slot.release()


def _step_detail(s):
    return s.get("role") or s.get("path") or s.get("testid") or ""


def execute(job, runner, runs_dir):
    try:
        job.status = "running"
        job.emit("run_started", total=len(job.cases), mode=job.mode_label)
        for c in job.cases:
            cid = c["case_id"]
            out = Path(runs_dir) / job.id / cid
            job.emit("case_started", case_id=cid, test_id=c["test_id"], profile=c["profile"],
                     rule_id=c["rule"]["rule_id"])

            def on_event(ev, cid=cid):
                job.emit(ev["type"], case_id=cid, **{k: v for k, v in ev.items() if k != "type"})

            try:
                res = runner(c["rule"], c["scenario"], c["profile"], out, on_event)
            except Exception as e:      # never leak internals; an unproven case is INCONCLUSIVE
                res = {"verdict": "INCONCLUSIVE", "reasons": [f"runner error: {type(e).__name__}"],
                       "checks": {}, "observed": {}, "artifact_sha256": {}}
            trace_file = out / "evidence" / "trace.json"
            if trace_file.exists():
                for s in json.loads(trace_file.read_text()):
                    shot = s.get("screenshot")
                    job.emit("step", case_id=cid, n=s["step"], action=s["action"], detail=_step_detail(s),
                             ok=s["ok"], error=s.get("error"),
                             screenshot=(f"/api/runs/{job.id}/cases/{cid}/file/evidence/{shot}" if shot else None))
            job.results[cid] = res
            job.emit("verdict", case_id=cid, verdict=res["verdict"], reasons=res["reasons"])
            sb = res.get("sandbox")
            if sb:
                job.emit("cleanup", case_id=cid, confirmed=sb.get("cleanup_confirmed"))
            job.emit("case_finished", case_id=cid)
        job.summary = summarize(job)
        job.status = "finished"
        job.emit("run_finished", summary=job.summary)
    except Exception as e:
        job.status, job.error = "failed", type(e).__name__
        job.emit("run_failed", error=type(e).__name__)


def summarize(job):
    prof = {c["case_id"]: c["profile"] for c in job.cases}
    verdicts = {cid: r["verdict"] for cid, r in job.results.items()}
    sandboxes = [r["sandbox"] for r in job.results.values() if r.get("sandbox")]
    return {
        "cases": len(job.cases),
        "planted_violations_detected": sum(v == "VIOLATION" for cid, v in verdicts.items() if prof[cid] == "buggy"),
        "false_positives_on_correct_build": sum(v == "VIOLATION" for cid, v in verdicts.items() if prof[cid] == "correct"),
        "inconclusive": sum(v == "INCONCLUSIVE" for v in verdicts.values()),
        "cleanup_confirmed": (all(s.get("cleanup_confirmed") for s in sandboxes) if sandboxes else None),
    }
