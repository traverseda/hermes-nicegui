"""Tests for hermes_cli.py: the CLI-subprocess *mutation* wrappers, against
a fake HermesExecutor (see tests/conftest.py::FakeHermesCli).

Reads (`list_sessions`, `get_session_detail`, `list_profiles`) no longer
live in `hermes_cli` -- see `tests/test_executor.py` (`read_sqlite`/
`read_json`/`list_profile_names`) and `tests/test_store.py` (the
`SessionsStore`/`CronStore`/`KanbanStore` facade) for their coverage now.
"""

from __future__ import annotations

from hermes_nicegui import hermes_cli
from hermes_nicegui.executor import HermesExecutor


async def test_rename_session(executor: HermesExecutor, fake_hermes_cli) -> None:
    await hermes_cli.rename_session(executor, "", "sess-1", "New title")
    sessions = fake_hermes_cli.dump_sessions()
    assert next(s for s in sessions if s["id"] == "sess-1")["title"] == "New title"


async def test_delete_session(executor: HermesExecutor, fake_hermes_cli) -> None:
    await hermes_cli.delete_session(executor, "", "sess-1")
    assert [s["id"] for s in fake_hermes_cli.dump_sessions()] == ["sess-2"]


async def test_cron_actions_recorded(executor: HermesExecutor, fake_hermes_cli) -> None:
    await hermes_cli.create_job(executor, "ha", schedule="every 5m", prompt="check")
    await hermes_cli.pause_job(executor, "ha", "job-1")
    await hermes_cli.resume_job(executor, "ha", "job-1")
    await hermes_cli.delete_job(executor, "ha", "job-1")
    await hermes_cli.run_job(executor, "ha", "job-1")
    assert fake_hermes_cli.jobs_actions == [
        ("cron", "create", "every 5m", "check"),
        ("cron", "pause", "job-1"),
        ("cron", "resume", "job-1"),
        ("cron", "remove", "job-1"),
        ("cron", "run", "job-1"),
    ]


async def test_edit_job_only_passes_given_fields(
    executor: HermesExecutor, fake_hermes_cli
) -> None:
    """Only the flags for fields actually passed should reach the CLI --
    the cron detail page's form/YAML save always fills every field, but a
    caller passing `None` for one means "leave it alone", not "clear it"."""
    await hermes_cli.edit_job(executor, "", "aabbccddeeff", name="Renamed", schedule=None)
    assert fake_hermes_cli.jobs_actions == [
        ("cron", "edit", "aabbccddeeff", "--name", "Renamed")
    ]


async def test_edit_job_clears_skills_when_given_empty_list(
    executor: HermesExecutor, fake_hermes_cli
) -> None:
    await hermes_cli.edit_job(executor, "", "aabbccddeeff", skills=[])
    assert fake_hermes_cli.jobs_actions == [
        ("cron", "edit", "aabbccddeeff", "--clear-skills")
    ]
