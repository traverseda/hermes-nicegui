"""Tests for the cron job list page: rendering, nav, create dialog."""

from __future__ import annotations

from typing import cast

from nicegui import ui
from nicegui.testing import User
import pytest

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.cron import CronPlugin


async def test_cron_list_renders(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Nightly report")
    assert user.find(marker="new-job-button").elements


async def test_cron_nav_item_present(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open("/")
    await user.should_see("Cron Jobs")


async def test_rows_are_clickable(user: User, context: PluginContext) -> None:
    """Rows must carry Quasar's `clickable` prop for real-browser clicks."""
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Nightly report")
    for item in user.find(marker="job-row").elements:
        assert item.props.get("clickable") is True


async def test_click_row_navigates_to_detail(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Nightly report")
    user.find(marker="job-row").click()
    await user.should_see("Details", retries=10)
    await user.should_see("Raw YAML", retries=10)


async def test_script_only_job_has_script_badge(
    user: User, context: PluginContext, fake_hermes_cli
) -> None:
    fake_hermes_cli.insert_job(
        {
            "id": "script-job",
            "name": "Food Log Reset",
            "prompt": "",
            "script": "reset-food-today.sh",
            "no_agent": True,
            "schedule_display": "every 1d",
            "deliver": "local",
            "skills": [],
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "created_at": "2026-08-13T00:00:00+00:00",
            "next_run_at": "2026-08-14T00:00:00+00:00",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
        }
    )
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Food Log Reset")
    assert user.find(marker="script-badge").elements
    assert len(user.find(marker="script-badge").elements) == 1


async def test_script_only_job_detail_shows_mode(
    user: User, context: PluginContext, fake_hermes_cli
) -> None:
    fake_hermes_cli.insert_job(
        {
            "id": "script-job",
            "name": "Food Log Reset",
            "prompt": "",
            "script": "reset-food-today.sh",
            "no_agent": True,
            "schedule_display": "every 1d",
            "deliver": "local",
            "skills": [],
            "repeat": {"times": None, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "created_at": "2026-08-13T00:00:00+00:00",
            "next_run_at": "2026-08-14T00:00:00+00:00",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
        }
    )
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Food Log Reset")
    await user.open("/cron/script-job")
    assert user.find(marker="job-script-line").elements
    await user.should_see("reset-food-today.sh", retries=10)

    await user.open("/cron/aabbccddeeff")
    assert user.find(marker="job-agent-mode-line").elements
    with pytest.raises(AssertionError):
        user.find(marker="job-script-line")


async def test_create_job_dialog_records_cli_call_and_appears_in_list(
    user: User, context: PluginContext, fake_hermes_cli
) -> None:
    """Creating a job has no structured response from `hermes cron create`,
    so this checks both that the CLI call went out with the right args *and*
    that the list's re-read afterwards (`cron/jobs.json`, written by the
    fake CLI the same way the real one would) picks up the new job."""
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Nightly report")

    user.find(marker="new-job-button").click()
    await user.should_see("New cron job", retries=10)
    user.find(marker="new-job-name").type("Log rotation")
    user.find(marker="new-job-schedule").type("every 1h")
    user.find(marker="create-job-confirm").click()

    await user.should_see("Job created", retries=10)
    assert fake_hermes_cli.jobs_actions == [
        ("cron", "create", "every 1h", "--name", "Log rotation")
    ]
    await user.should_see("Log rotation", retries=10)


async def test_list_paginates_with_numbered_pages(
    user: User, context: PluginContext, fake_hermes_cli
) -> None:
    for i in range(35):
        fake_hermes_cli.insert_job(
            {
                "id": f"job-extra-{i}",
                "name": f"Extra job {i}",
                "prompt": "",
                "schedule_display": "every 1h",
                "deliver": "local",
                "skills": [],
                "repeat": {"times": None, "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "created_at": "2026-08-13T00:00:00+00:00",
                "next_run_at": "2026-08-14T00:00:00+00:00",
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
            }
        )
    web.build(context, [CronPlugin(context)])
    await user.open("/cron")
    await user.should_see("Nightly report")
    assert len(user.find(marker="job-row").elements) == 30

    pager = cast(ui.pagination, next(iter(user.find(marker="page-control").elements)))
    pager.set_value(2)
    await user.should_see("Extra job 30", retries=10)
    assert len(user.find(marker="job-row").elements) == 6
    await user.should_not_see("Nightly report", retries=10)
