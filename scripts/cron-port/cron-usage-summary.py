#!/usr/bin/env python3
"""Cron token usage summary from ~/.hermes/cron/usage_audit.jsonl.

Aggregates per-job token totals for the last N days (default 14, argv override),
resolves job names from jobs.json, and prints a compact table plus summary lines.
Output is small on purpose: it is injected into an LLM prompt on every cron run.
Stdlib only.

Ported from hermesagent.lan (2026-08-15): the old copy hardcoded
/home/hermes/.hermes/cron; this copy resolves the cron dir from $HERMES_HOME
(or ~/.hermes) so it works on any box. The current cron scheduler writes the
same usage_audit.jsonl path (cron/scheduler.py::_usage_audit_path).
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
CRON_DIR = _HERMES_HOME / "cron"
AUDIT = f"{CRON_DIR}/usage_audit.jsonl"
JOBS = f"{CRON_DIR}/jobs.json"

HEADER = (
    "job_id       name                     runs silent errs   prompt_tok    comp_tok"
    "        total      per_run  last_run              models"
)


def _int(v):
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def load_jobs(path):
    """job_id -> name, defensive about shape (list or dict-with-jobs)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    jobs = {}
    if isinstance(data, dict):
        data = data.get("jobs") or list(data.values())
    if isinstance(data, dict):  # id -> job dict
        data = list(data.values())
    if isinstance(data, list):
        for j in data:
            if isinstance(j, dict) and j.get("id"):
                jobs[str(j["id"])] = j.get("name") or "?"
    return jobs


def main():
    days = 14
    if len(sys.argv) > 1:
        try:
            days = max(1, int(sys.argv[1]))
        except ValueError:
            pass
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    jobs = load_jobs(JOBS)
    agg = {}  # job_id -> stats
    try:
        with open(AUDIT) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                try:
                    dt = datetime.fromisoformat(str(rec.get("ts")).replace("Z", "+00:00"))
                except (TypeError, ValueError):
                    continue
                if dt < cutoff:
                    continue
                jid = rec.get("job_id")
                if not jid:
                    continue
                a = agg.setdefault(jid, {
                    "runs": 0, "silent": 0, "errs": 0,
                    "prompt": 0, "comp": 0, "total": 0, "last": dt, "models": [],
                })
                a["runs"] += 1
                a["silent"] += 1 if rec.get("response_silent") else 0
                a["errs"] += 1 if rec.get("error") else 0
                p, c = _int(rec.get("prompt_tokens")), _int(rec.get("completion_tokens"))
                a["prompt"] += p
                a["comp"] += c
                a["total"] += _int(rec.get("total_tokens")) or (p + c)
                if dt > a["last"]:
                    a["last"] = dt
                m = rec.get("model")
                if m:
                    a["models"].append(m)
    except FileNotFoundError:
        print(f"audit file not found: {AUDIT}", file=sys.stderr)
        return 1

    rows = sorted(agg.items(), key=lambda kv: kv[1]["total"], reverse=True)
    total_tokens = sum(a["total"] for _, a in rows)
    total_runs = sum(a["runs"] for _, a in rows)

    print(f"# Cron token usage — last {days} days (from usage_audit.jsonl)")
    print(HEADER)
    for jid, a in rows:
        name = jobs.get(jid) or "?"
        name = name if len(name) <= 24 else name[:23] + "…"
        last = a["last"].strftime("%Y-%m-%d %H:%M") + " UTC"
        models = ",".join(sorted(set(a["models"])))
        print(f"{jid:<12} {name:<24} {a['runs']:>4} {a['silent']:>6} {a['errs']:>4}"
              f" {a['prompt']:>11,} {a['comp']:>9,} {a['total']:>12,}"
              f" {a['total'] // a['runs']:>11,} {last:<20}  {models}")
    if rows:
        top_jid, top = rows[0]
        share = top["total"] / total_tokens * 100
        print(f"total runs: {total_runs}")
        print(f"total tokens: {total_tokens:,}")
        print(f"top spend: {jobs.get(top_jid) or '?'} ({top_jid})"
              f" {top['total']:,} tokens ({share:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
