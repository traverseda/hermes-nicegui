"""Tests for the cron job detail/edit page."""

from __future__ import annotations

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.cron import CronPlugin

JOB_ID = "aabbccddeeff"


async def test_detail_page_loads_directly(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Nightly report")


async def test_detail_page_has_actions(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Pause")
    await user.should_see("Run now")
    await user.should_see("Delete")


async def test_detail_page_has_yaml_tab(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Raw YAML")
    assert user.find(marker="save-yaml-button").elements


async def test_pause_resume_toggles(user: User, context: PluginContext, fake_hermes_cli) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Pause")
    user.find(marker="pause-resume-button").click()
    await user.should_see("Resume", retries=10)
    assert fake_hermes_cli.jobs_actions == [("cron", "pause", JOB_ID)]


async def test_delete_navigates_to_list(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Nightly report")
    user.find(marker="delete-job-button").click()
    await user.should_see("Scheduled jobs", retries=10)
    await user.should_not_see("Nightly report", retries=10)


async def test_save_details_updates_name(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Nightly report")

    name_input = user.find(marker="job-name-input")
    name_input.clear()
    name_input.type("Renamed job")
    user.find(marker="save-details-button").click()

    await user.should_see("Renamed job", retries=10)
