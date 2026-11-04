"""Host-side deterministic verifier. Runs OUTSIDE the sandbox on copied snapshots.

CLI:
  python -m verifier.verify --rule rule.json --scenario scenario.json \
      --pre pre.db --post post.db --log http_log.json
"""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from .registry import Result, approval_required_over_threshold, owner_only_access
from .schemas import Rule, Scenario

REGISTRY = {"approval_required_over_threshold": approval_required_over_threshold,
            "owner_only_access": owner_only_access}


def sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def open_ro(path):
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    con.execute("SELECT name FROM sqlite_master LIMIT 1")  # fails fast on a corrupt file
    return con


def verify(rule_path, scenario_path, pre_path, post_path, log_path):
    rule = Rule.model_validate_json(Path(rule_path).read_text())          # raises if invalid
    scenario = Scenario.model_validate_json(Path(scenario_path).read_text())
    if (rule.rule_id, rule.invariant_type) != (scenario.rule_id, scenario.invariant_type):
        raise ValueError("rule and scenario disagree on rule_id or invariant_type")

    artifacts = {Path(p).name: sha256(p) for p in (pre_path, post_path, log_path)}
    try:
        http_log = json.loads(Path(log_path).read_text())
        pre, post = open_ro(pre_path), open_ro(post_path)
    except (OSError, ValueError, sqlite3.Error) as e:
        res = Result("INCONCLUSIVE", [f"evidence unreadable: {e}"])
    else:
        res = REGISTRY[rule.invariant_type](rule, scenario, pre, post, http_log)

    return {"test_id": scenario.test_id, "rule_id": rule.rule_id,
            "source_clause": rule.source_clause, "invariant_type": rule.invariant_type,
            "verdict": res.verdict, "reasons": res.reasons, "checks": res.checks,
            "observed": res.observed, "artifact_sha256": artifacts}


def main():
    ap = argparse.ArgumentParser()
    for a in ("rule", "scenario", "pre", "post", "log"):
        ap.add_argument(f"--{a}", required=True)
    a = ap.parse_args()
    try:
        out = verify(a.rule, a.scenario, a.pre, a.post, a.log)
    except Exception as e:  # schema/invariant rejection is an error, not a verdict
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
