"""Measure which Vultr model actually works for PolicyReplay (run on the VM).

  set -a; source /etc/policyreplay.env; set +a
  python scripts/model_bench.py                       # default candidates
  python scripts/model_bench.py glm-5.3-flash qwen3.8-27b

TEXT: runs the real policy engine twice per model. A run counts only if the model's output passes ALL
      validation (real source=model), not merely "returned JSON".
VISION: asks the model about two real screenshots with known ground truth (buggy vs correct build).
The key is never printed. Each model costs a handful of small calls; check Billing afterwards.
"""
import base64
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.llm import LLMError, extract_json_text, http_transport  # noqa: E402
from orchestrator.policy_engine import analyze  # noqa: E402

DEFAULT = ["deepseek-v4.1-flash-1017", "glm-5.3-flash", "qwen3.8-flash-next",
           "minimax-m3.1-flash", "mimo-v2.6-flash"]
POLICY = (ROOT / "policies" / "sample_policy.txt").read_text()
SHOTS = {"buggy": ROOT / "runs/sandbox/buggy_15000/evidence/screenshots/05_click.png",
         "correct": ROOT / "runs/sandbox/correct_15000/evidence/screenshots/05_click.png"}
EXPECT = {"buggy": "completed", "correct": "rejected"}
VISION_PROMPT = ("This is a screenshot of an order page in a web shop, taken right after someone "
                 "submitted a refund request. Return ONLY JSON: "
                 '{"outcome": "completed" | "rejected" | "unclear", "visible_message": "<the status message '
                 'text shown on the page, or empty string>"}. outcome is "completed" if the page says the '
                 'refund was issued, "rejected" if it shows a message refusing or blocking it, otherwise "unclear".')


def bench_text(model, runs=2):
    ok, secs, why = 0, [], None
    for _ in range(runs):
        t0 = time.time()
        out = analyze(POLICY, transport=lambda m, _name, mid=model: http_transport(m, mid, timeout=120))
        secs.append(time.time() - t0)
        if out["source"] == {"rules": "model", "scenarios": "model"}:
            ok += 1
        elif why is None:
            why = next((v for v in out["fallback_reason"].values() if v), "unknown")
    return ok, runs, sum(secs) / len(secs), why


def bench_vision(model):
    if not all(p.exists() for p in SHOTS.values()):
        return None, "run `python orchestrator/sandbox_run.py run` first (screenshots missing)"
    good, why = 0, None
    for kind, path in SHOTS.items():
        b64 = base64.b64encode(path.read_bytes()).decode()
        msg = [{"role": "user", "content": [
            {"type": "text", "text": VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}]}]
        try:
            d = json.loads(extract_json_text(http_transport(msg, model, timeout=120)))
            if d.get("outcome") == EXPECT[kind]:
                good += 1
            elif why is None:
                why = f"{kind}: got {d.get('outcome')!r} ({str(d.get('visible_message'))[:60]!r})"
        except (LLMError, ValueError) as e:
            why = why or f"{kind}: {str(e)[:120]}"
    return good, why


if __name__ == "__main__":
    vision_only = "--vision-only" in sys.argv
    models = [a for a in sys.argv[1:] if not a.startswith("--")] or DEFAULT
    print(f"{'model':34} {'text ok':8} {'avg s':6} {'vision':7} notes")
    for m in models:
        try:
            ok, runs, avg, twhy = (0, 0, 0.0, None) if vision_only else bench_text(m)
            vg, vwhy = bench_vision(m)
        except Exception as e:     # keep going; never print anything that could contain a key
            print(f"{m:34} ERROR {type(e).__name__}")
            continue
        vis = "n/a" if vg is None else f"{vg}/2"
        note = "; ".join(x for x in (twhy, vwhy) if x)[:160]
        txt = "skipped" if vision_only else f"{ok}/{runs}"
        print(f"{m:34} {txt:8} {avg:5.1f}s {vis:7} {note}")
