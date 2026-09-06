#!/usr/bin/env python3
"""Import legacy Hermes cron jobs (hermesagent.lan) into the local cron store.

Ports the old crontab manager's state (``~/.hermes/cron/jobs.json`` from the
legacy agent on hermesagent.lan) into the current Hermes Agent's local cron
store, preserving schedules, prompts, scripts, skills and enabled flags while
adapting records to this environment:

  * runtime fields are RESET (``next_run_at``, ``last_run_at``,
    ``fire_claim``, ``last_status``, ...) so nothing fires retroactively on
    import; the scheduler recomputes the next occurrence from ``schedule`` on
    its next tick (the store's ``_normalize_job_record`` / self-heal path).
  * delivery targets that do not exist in this environment are REMAPPED. The
    default map sends every ``discord:*`` / bare ``discord`` target to
    ``local`` because the current box has no Discord platform configured.
    Override with ``--deliver-map`` (comma-separated ``old=new`` pairs; the
    special key ``discord:*`` matches any ``discord:<id>`` target).
  * the stale ``origin`` block is dropped (it describes the legacy chat the
    job was created from).
  * idempotent: job ids already present in the target store are skipped
    (``--replace`` to overwrite instead).
  * the store is written under the same fcntl ``.jobs.lock`` the scheduler
    uses, atomically (tmp file + os.replace), mode 0600.

The target store defaults to ``~/.hermes/cron`` (override with ``--cron-dir``
or ``$HERMES_HOME``). The old agent's jobs.json is fully compatible with the
current scheduler — ``hermes_cli/cron.py`` is byte-identical between the two
installations — so this tool is a faithful field-level migration, not a
rewrite.

Stdlib only. Usage::

    python3 import-old-cron.py --source old-jobs.json --dry-run
    python3 import-old-cron.py --source old-jobs.json --backup
"""

import argparse
import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path

# Fields that describe the job definition and are preserved verbatim.
_DEFINITION_FIELDS = (
    "id", "name", "prompt", "skills", "skill", "model", "provider",
    "base_url", "script", "no_agent", "context_from", "schedule",
    "schedule_display", "repeat", "enabled", "deliver", "workdir",
    "enabled_toolsets", "created_at",
)

# Runtime/derived fields that must NOT carry over (stale on the new box).
# ``state`` is forced to "scheduled" for ENABLED jobs; disabled (paused)
# source records keep their pause markers so they land paused, not firing.
_RESET_FIELDS = {
    "state": "scheduled",
    "next_run_at": None,
    "last_run_at": None,
    "last_status": None,
    "last_error": None,
    "last_delivery_error": None,
    "fire_claim": None,
    "paused_at": None,
    "paused_reason": None,
    "origin": None,
}

# Pause markers preserved when the source record is disabled (enabled=false).
_PAUSE_FIELDS = ("state", "paused_at", "paused_reason")

_LOCK_TIMEOUT_SECONDS = 30.0


class DeliverMap:
    """Parse + apply ``old=new`` deliver remaps.

    Special key ``discord:*`` matches any ``discord:<anything>`` target.
    """

    def __init__(self, pairs_text: str):
        self._exact = {}
        self._wildcard = {}
        for pair in (pairs_text or "").split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            old, new = pair.split("=", 1)
            old, new = old.strip(), new.strip()
            if old.endswith(":*"):
                self._wildcard[old[:-2]] = new
            else:
                self._exact[old] = new

    def apply(self, deliver):
        if deliver is None:
            return deliver
        if deliver in self._exact:
            return self._exact[deliver]
        base = deliver.split(":", 1)[0]
        if base in self._wildcard:
            return self._wildcard[base]
        return deliver


DEFAULT_DELIVER_MAP = "discord:*=local,discord=local"


def _jobs_lock_file(cron_dir: Path) -> Path:
    return cron_dir / ".jobs.lock"


def acquire_jobs_lock(cron_dir: Path):
    """Blocking fcntl lock with timeout, mirroring cron/jobs.py semantics."""
    lock_path = _jobs_lock_file(cron_dir)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except (BlockingIOError, OSError):
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(
                    f"could not acquire {lock_path} within "
                    f"{_LOCK_TIMEOUT_SECONDS:.0f}s (scheduler busy?)"
                )
            time.sleep(0.2)


def load_source(path: Path):
    """Return the list of job records from a legacy jobs.json.

    Accepts the shapes the old manager produced: ``{"jobs": [...]}``,
    ``{"updated_at": ..., "jobs": [...]}``, a bare list, or a dict of
    id -> job.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("jobs", list(data.values()))
    if isinstance(data, dict):  # id -> job dict
        data = list(data.values())
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a list of jobs, got {type(data).__name__}")
    return data


def load_target_jobs(cron_dir: Path):
    jobs_file = cron_dir / "jobs.json"
    if not jobs_file.exists():
        return []
    try:
        with open(jobs_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("jobs", [])
    return data if isinstance(data, list) else []


def adapt_job(job: dict, deliver_map: DeliverMap, replace_ok: bool = True) -> dict:
    """Transform one legacy record into a current-store record."""
    job_id = str(job.get("id") or "").strip()
    if not job_id:
        raise ValueError("job record has no id")
    schedule = job.get("schedule")
    if not isinstance(schedule, dict) or not schedule.get("kind"):
        raise ValueError(f"job {job_id}: missing/invalid schedule")

    adapted = {k: job.get(k) for k in _DEFINITION_FIELDS}
    adapted["id"] = job_id
    adapted["deliver"] = deliver_map.apply(job.get("deliver"))
    adapted.update(_RESET_FIELDS)
    # Disabled source records carry their pause markers (land paused, never
    # firing); enabled jobs are always forced back to "scheduled" so a stale
    # "error"/"paused" state from the old box does not carry over.
    if not adapted.get("enabled", True):
        for field in _PAUSE_FIELDS:
            if job.get(field) is not None:
                adapted[field] = job.get(field)
        if adapted.get("state") is None:
            adapted["state"] = "paused"
    # Keep repeat limits, drop completed-run counters (fresh start on new box).
    repeat = job.get("repeat") or {}
    if isinstance(repeat, dict):
        adapted["repeat"] = {"times": repeat.get("times"), "completed": 0}
    return adapted


def write_store(cron_dir: Path, jobs: list, backup: bool = True) -> Path:
    cron_dir.mkdir(parents=True, exist_ok=True)
    jobs_file = cron_dir / "jobs.json"
    payload = {"jobs": jobs, "updated_at": time.strftime(
        "%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"}

    if backup and jobs_file.exists():
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        jobs_file.rename(jobs_file.with_name(f"jobs.json.bak-{stamp}"))

    fd = acquire_jobs_lock(cron_dir)
    try:
        fd_tmp, tmp_path = tempfile.mkstemp(
            dir=str(cron_dir), prefix=".jobs.json.", suffix=".tmp")
        try:
            with os.fdopen(fd_tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, jobs_file)
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    return jobs_file


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Import legacy Hermes cron jobs into the local cron store")
    parser.add_argument("--source", required=True, help="legacy jobs.json path")
    parser.add_argument("--cron-dir", default=None,
                        help="target cron dir (default: $HERMES_HOME/cron or ~/.hermes/cron)")
    parser.add_argument("--deliver-map", default=DEFAULT_DELIVER_MAP,
                        help="comma-separated old=new deliver remaps "
                             "(default: %(default)s)")
    parser.add_argument("--jobs", default=None,
                        help="comma-separated job id filter (import only these)")
    parser.add_argument("--replace", action="store_true",
                        help="overwrite jobs whose id already exists in target")
    parser.add_argument("--no-backup", action="store_true",
                        help="do not keep a .bak of the pre-import jobs.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported without writing")
    args = parser.parse_args(argv)

    if args.cron_dir:
        cron_dir = Path(args.cron_dir).expanduser()
    else:
        hermes_home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        cron_dir = Path(hermes_home) / "cron"

    source_path = Path(args.source).expanduser()
    source_jobs = load_source(source_path)
    deliver_map = DeliverMap(args.deliver_map)
    wanted = None
    if args.jobs:
        wanted = {j.strip() for j in args.jobs.split(",") if j.strip()}

    existing = {str(j.get("id")) for j in load_target_jobs(cron_dir)}

    to_import, skipped, errors = [], [], []
    for job in source_jobs:
        job_id = str(job.get("id") or "").strip()
        if wanted is not None and job_id not in wanted:
            continue
        try:
            adapted = adapt_job(job, deliver_map)
        except ValueError as exc:
            errors.append(f"  ! {job_id or '<no-id>'}: {exc}")
            continue
        if job_id in existing and not args.replace:
            skipped.append(job_id)
            continue
        to_import.append(adapted)

    print(f"source:        {source_path} ({len(source_jobs)} jobs)")
    print(f"target store:  {cron_dir}/jobs.json")
    print(f"deliver map:   {args.deliver_map}")
    print(f"filter:        {wanted or 'all'}")
    print(f"existing ids:  {len(existing)}")
    print(f"to import:     {len(to_import)}")
    print(f"skipped (dup): {len(skipped)} -> {skipped}")
    for line in errors:
        print(line)

    if args.dry_run:
        print("DRY RUN — no files written")
        for job in to_import:
            print(f"  would import {job['id']} {job['name']!r} "
                  f"schedule={job['schedule'].get('display') or job['schedule'].get('expr') or job['schedule'].get('run_at')} "
                  f"deliver={job['deliver']} script={job.get('script') or '-'} "
                  f"skills={job.get('skills') or []}")
        return 0

    if not to_import:
        print("nothing to import")
        return 0

    target_jobs = load_target_jobs(cron_dir)
    by_id = {str(j.get("id")): j for j in target_jobs}
    for job in to_import:
        by_id[job["id"]] = job
    write_store(cron_dir, list(by_id.values()), backup=not args.no_backup)
    print(f"wrote {len(to_import)} jobs to {cron_dir}/jobs.json "
          f"(store now has {len(by_id)} jobs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
