"""PolicyReplay orchestrator API (FastAPI). Holds the inference key; never runs inside a sandbox.

Public-demo safety (v3 section 7): fixed target app only (no URL parameter exists), policy text length
cap, per-IP rate limits, ONE concurrent run, daily model-call budget (falls back to labeled replay),
no key or credential is ever returned. The unauthenticated /dispatch prototype has been removed.

  uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000     (from the repo root)
  PR_RUN_MODE=docker  -> hardened Docker sandbox (default: local, development only)
"""
import json
import os
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from orchestrator import llm, report as reportmod  # noqa: E402
from orchestrator.jobs import Job, JobManager  # noqa: E402
from orchestrator.llm import LLMError  # noqa: E402
from orchestrator.policy_engine import analyze  # noqa: E402

SAMPLE_POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()
MAX_POLICY_CHARS = 4000
PROFILES = ("buggy", "correct")
SHOT = re.compile(r"^evidence/screenshots/[A-Za-z0-9_.-]+\.png$")
FILES = {"verdict.json", "scenario.json", "rule.json", "http_log.json",
         "evidence/trace.json", "evidence/manifest.json"}
DEFAULT_LIMITS = {"analyze": (20, 3600), "run": (10, 3600)}


class RateLimiter:
    def __init__(self, limits):
        self.limits, self.hits, self.lock = limits, defaultdict(deque), threading.Lock()

    def check(self, kind, ip):
        n, window = self.limits[kind]
        now = time.time()
        with self.lock:
            q = self.hits[(kind, ip)]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= n:
                raise HTTPException(429, "rate limit reached; try again later")
            q.append(now)


class Budget:
    """Daily cap on model calls. When exhausted, requests fall back to labeled replayed output."""

    def __init__(self, per_day):
        self.per_day, self.day, self.used, self.lock = per_day, date.today(), 0, threading.Lock()

    def take(self):
        with self.lock:
            if self.day != date.today():
                self.day, self.used = date.today(), 0
            if self.used >= self.per_day:
                return False
            self.used += 1
            return True


class AnalyzeBody(BaseModel):
    policy_text: str | None = Field(default=None, max_length=MAX_POLICY_CHARS)
    replay: bool = False


class ApproveBody(BaseModel):
    test_ids: list[str] | None = None


class RunBody(BaseModel):
    analysis_id: str
    test_ids: list[str] | None = None


def default_runner():
    if os.environ.get("PR_RUN_MODE", "local") == "docker":
        from orchestrator import sandbox_run
        return sandbox_run.run_scenario, "docker sandbox"
    from orchestrator import local_e2e
    ns = os.environ.get("PR_NO_SANDBOX") == "1"
    return (lambda rule, sc, profile, out, on_event: local_e2e.run_scenario(
        rule, sc, profile, out, ns, on_event)), "local (development only, no containment)"


def create_api(runs_dir=None, runner=None, mode_label=None, transport=None, limits=None, daily_budget=None):
    runs_dir = Path(runs_dir or ROOT / "runs" / "api")
    runs_dir.mkdir(parents=True, exist_ok=True)
    if runner is None:
        runner, mode_label = default_runner()
    mode_label = mode_label or "custom runner"
    budget = Budget(daily_budget if daily_budget is not None else int(os.environ.get("PR_DAILY_MODEL_CALLS", "100")))
    limiter = RateLimiter({**DEFAULT_LIMITS, **(limits or {})})
    jobs, analyses, alock = JobManager(), OrderedDict(), threading.Lock()
    trust_proxy = os.environ.get("PR_TRUST_PROXY") == "1"

    base_transport = transport or (llm.http_transport if llm.configured() else None)

    def counted_transport(messages, model):
        if not budget.take():
            raise LLMError("daily model-call budget reached")
        return base_transport(messages, model)

    app = FastAPI(title="PolicyReplay Orchestrator")

    def ip_of(request):
        if trust_proxy and request.headers.get("x-forwarded-for"):
            return request.headers["x-forwarded-for"].split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def get_analysis(aid):
        with alock:
            a = analyses.get(aid)
        if not a:
            raise HTTPException(404, "analysis not found or expired")
        return a

    @app.get("/api/health")
    def health():
        return {"ok": True, "mode": mode_label, "model_configured": base_transport is not None}

    @app.get("/api/policy")
    def policy():
        return {"policy_text": SAMPLE_POLICY, "max_chars": MAX_POLICY_CHARS}

    @app.post("/api/analyze")
    def analyze_policy(body: AnalyzeBody, request: Request):
        limiter.check("analyze", ip_of(request))
        text = (body.policy_text or SAMPLE_POLICY).strip()
        if not text:
            raise HTTPException(400, "policy text is empty")
        try:
            out = analyze(text, transport=counted_transport if base_transport else None, replay=body.replay)
        except LLMError as e:
            raise HTTPException(422, str(e)) from None
        aid = uuid.uuid4().hex[:12]
        scenarios = [{**s.model_dump(), "approved": False} for s in out["scenarios"]]
        rec = {"id": aid, "rules": [r.model_dump() for r in out["rules"]], "unsupported": out["unsupported"],
               "scenarios": scenarios, "source": out["source"], "approved_at": None}
        with alock:
            analyses[aid] = rec
            while len(analyses) > 50:
                analyses.popitem(last=False)
        return {"analysis_id": aid, "source": rec["source"], "rules": rec["rules"],
                "unsupported": rec["unsupported"], "scenarios": scenarios}

    @app.post("/api/analyses/{aid}/approve")
    def approve(aid: str, body: ApproveBody):
        a = get_analysis(aid)
        ids = {s["test_id"] for s in a["scenarios"]}
        chosen = set(body.test_ids) if body.test_ids is not None else ids
        if not chosen or not chosen <= ids:
            raise HTTPException(400, "test_ids must be a non-empty subset of this analysis")
        with alock:
            for s in a["scenarios"]:
                s["approved"] = s["test_id"] in chosen
            a["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {"approved": sorted(chosen), "approved_at": a["approved_at"]}

    @app.post("/api/runs", status_code=202)
    def start_run(body: RunBody, request: Request):
        a = get_analysis(body.analysis_id)
        approved = [s for s in a["scenarios"] if s["approved"]]
        if body.test_ids is not None:
            approved = [s for s in approved if s["test_id"] in set(body.test_ids)]
        if not approved:
            raise HTTPException(400, "no approved scenarios: approve scenarios before running")
        limiter.check("run", ip_of(request))
        rules = {r["rule_id"]: r for r in a["rules"]}
        cases = []
        for s in approved:
            sc = {k: v for k, v in s.items() if k != "approved"}
            for prof in PROFILES:
                cases.append({"case_id": f"{s['test_id']}__{prof}", "test_id": s["test_id"], "profile": prof,
                              "rule": rules[s["rule_id"]], "scenario": sc})
        job = Job(a["id"], cases, mode_label, a["source"], a["approved_at"])
        if not jobs.start(job, runner, runs_dir):
            raise HTTPException(409, "a run is already in progress")
        return {"run_id": job.id, "cases": len(cases)}

    def get_job(run_id):
        job = jobs.get(run_id)
        if not job:
            raise HTTPException(404, "run not found")
        return job

    @app.get("/api/runs/{run_id}")
    def run_status(run_id: str):
        job = get_job(run_id)
        done = sum(1 for e in job.events if e["type"] == "case_finished")
        return {"run_id": job.id, "status": job.status, "progress": {"done": done, "total": len(job.cases)},
                "summary": job.summary, "error": job.error,
                "verdicts": {cid: r["verdict"] for cid, r in job.results.items()}}

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, request: Request, after: int = 0):
        job = get_job(run_id)
        last = request.headers.get("last-event-id")
        if last and last.isdigit():
            after = int(last) + 1

        def gen():
            i, deadline = max(after, 0), time.time() + 600
            while time.time() < deadline:
                batch, done = job.snapshot_events(i)
                for ev in batch:
                    yield f"id: {ev['i']}\nevent: {ev['type']}\ndata: {json.dumps(ev)}\n\n"
                    i = ev["i"] + 1
                if done and not batch:
                    return
                if not batch:
                    yield ": keep-alive\n\n"
                time.sleep(0.25)
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}/report")
    def run_report(run_id: str):
        return reportmod.run_report(get_job(run_id), runs_dir)

    @app.get("/api/runs/{run_id}/cases/{case_id}/file/{path:path}")
    def case_file(run_id: str, case_id: str, path: str):
        job = get_job(run_id)
        if case_id not in {c["case_id"] for c in job.cases} or not (path in FILES or SHOT.match(path)):
            raise HTTPException(404, "not found")
        base = (runs_dir / job.id / case_id).resolve()
        f = (base / path).resolve()
        if base not in f.parents or not f.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(f)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(ROOT / "dashboard" / "index.html")

    return app


app = create_api()
