"""Summarise every API run on this machine: the v3 reliability gate plus containment evidence.

  python scripts/gate_report.py            # reads runs/api
  python scripts/gate_report.py | tee docs/evidence/gate_report.txt

Prints no secrets: it only reads verdict.json files (verdicts, docker-inspect flags, cleanup).
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
base = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "runs" / "api"

runs = {}
for f in base.glob("*/*/verdict.json"):
    runs.setdefault(f.parent.parent.name, []).append(f)

rows, latest_sb = [], None
for rid, files in runs.items():
    planted_policies = set()
    fp = inc = 0
    cleanup = []
    for f in files:
        d = json.loads(f.read_text())
        profile = f.parent.name.rsplit("__", 1)[-1]
        v = d.get("verdict")
        if v == "VIOLATION" and profile == "buggy":
            planted_policies.add(d.get("rule_id"))
        fp += v == "VIOLATION" and profile == "correct"
        inc += v == "INCONCLUSIVE"
        sb = d.get("sandbox")
        cleanup.append(None if not sb else bool(sb.get("cleanup_confirmed")))
        if sb and (latest_sb is None or f.stat().st_mtime > latest_sb[0]):
            latest_sb = (f.stat().st_mtime, rid, f.parent.name, sb)
    planted = len(planted_policies)
    mode = "local" if all(c is None for c in cleanup) else "docker"
    clean = fp == 0 and inc == 0 and mode == "docker" and all(cleanup)
    rows.append((max(f.stat().st_mtime for f in files), rid, len(files), planted, fp, inc, mode,
                 all(c for c in cleanup) if mode == "docker" else None, clean))
rows.sort()

print(f"{'finished (local time)':21} {'run':13} {'cases':5} {'planted*':7} {'false+':6} {'inconcl':7} {'mode':6} {'cleanup':7} clean")
for ts, rid, n, planted, fp, inc, mode, cl, clean in rows:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)):21} {rid:13} {n:<5} {planted:<7} {fp:<6} {inc:<7} "
          f"{mode:6} {str(cl):7} {'YES' if clean else 'no'}")
streak = 0
for r in reversed(rows):
    if not r[-1]:
        break
    streak += 1
print("(* planted = distinct policies with a detected violation on the buggy build)")
print(f"\nconsecutive clean docker runs at the end: {streak}   (v3 gate: 3)")

if latest_sb:
    _, rid, case, sb = latest_sb
    print(f"\ncontainment evidence from run {rid}, case {case}:")
    print(json.dumps({k: sb.get(k) for k in ("run_id", "containment", "worker_exit", "cleanup_confirmed", "leftovers")},
                     indent=2))
else:
    print("\nno docker-mode runs found")
