"""Mutations run through the `hermes` CLI, profile-scoped.

Reads (sessions/messages, cron jobs, profile discovery) no longer live here
-- they read the daemon's own SQLite databases and JSON files directly (see
`hermes_nicegui.store` and `HermesExecutor.read_sqlite`/`read_json`/
`list_profile_names`), which is faster and doesn't depend on the CLI's text
output shape. This module is left with exactly the operations that need to
go through the CLI: they mutate state the daemon itself owns (message
counts, FTS triggers, cron ticker locks), which is unsafe to do by writing
SQL directly alongside the live daemon process.
"""

from __future__ import annotations

from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.gateway import HermesError

# -- sessions -------------------------------------------------------------


async def rename_session(
    executor: HermesExecutor, profile: str, session_id: str, title: str
) -> None:
    result = await executor.run("sessions", "rename", session_id, title, profile=profile)
    if result.returncode != 0:
        raise HermesError(f"sessions rename -> exit {result.returncode}: {result.stderr[:500]}")


async def delete_session(executor: HermesExecutor, profile: str, session_id: str) -> None:
    result = await executor.run("sessions", "delete", session_id, "--yes", profile=profile)
    if result.returncode != 0:
        raise HermesError(f"sessions delete -> exit {result.returncode}: {result.stderr[:500]}")


# -- cron mutations ---------------------------------------------------------
#
# Listing goes through `hermes_nicegui.store.CronStore` (reads `cron/jobs.json`
# directly) -- these are just the action verbs, which return nothing worth
# parsing. Callers re-read the job (via the store) afterwards to refresh
# displayed state.


async def _run_cron_action(executor: HermesExecutor, profile: str, *args: str) -> None:
    result = await executor.run("cron", *args, profile=profile)
    if result.returncode != 0:
        cmd = " ".join(args)
        raise HermesError(f"cron {cmd} -> exit {result.returncode}: {result.stderr[:500]}")


async def create_job(
    executor: HermesExecutor,
    profile: str,
    *,
    schedule: str,
    prompt: str = "",
    name: str | None = None,
    deliver: str | None = None,
    skills: list[str] | None = None,
    repeat: int | None = None,
) -> None:
    args = ["create", schedule]
    if prompt:
        args.append(prompt)
    if name:
        args += ["--name", name]
    if deliver:
        args += ["--deliver", deliver]
    if repeat is not None:
        args += ["--repeat", str(repeat)]
    for skill in skills or []:
        args += ["--skill", skill]
    await _run_cron_action(executor, profile, *args)


async def edit_job(
    executor: HermesExecutor,
    profile: str,
    job_id: str,
    *,
    name: str | None = None,
    schedule: str | None = None,
    prompt: str | None = None,
    deliver: str | None = None,
    skills: list[str] | None = None,
    repeat: int | None = None,
) -> None:
    """`hermes cron edit <job_id> --field value ...` -- only the flags for
    fields actually present in ``fields`` (the cron detail page's form/
    raw-YAML save) are passed, so an edit never clobbers a field the caller
    didn't mean to touch."""
    args = ["edit", job_id]
    if name is not None:
        args += ["--name", name]
    if schedule is not None:
        args += ["--schedule", schedule]
    if prompt is not None:
        args += ["--prompt", prompt]
    if deliver is not None:
        args += ["--deliver", deliver]
    if repeat is not None:
        args += ["--repeat", str(repeat)]
    if skills is not None:
        for skill in skills:
            args += ["--skill", skill]
        if not skills:
            args += ["--clear-skills"]
    await _run_cron_action(executor, profile, *args)


async def pause_job(executor: HermesExecutor, profile: str, job_id: str) -> None:
    await _run_cron_action(executor, profile, "pause", job_id)


async def resume_job(executor: HermesExecutor, profile: str, job_id: str) -> None:
    await _run_cron_action(executor, profile, "resume", job_id)


async def delete_job(executor: HermesExecutor, profile: str, job_id: str) -> None:
    await _run_cron_action(executor, profile, "remove", job_id)


async def run_job(executor: HermesExecutor, profile: str, job_id: str) -> None:
    await _run_cron_action(executor, profile, "run", job_id)
