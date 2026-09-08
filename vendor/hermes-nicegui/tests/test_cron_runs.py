"""Tests for cron execution history reads and presentation."""

from __future__ import annotations

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.cron import CronPlugin
from hermes_nicegui.store import CronStore

JOB_ID = "aabbccddeeff"


async def test_store_lists_newest_runs_and_filters_job(context: PluginContext) -> None:
    store = CronStore(context.executor, "")
    runs = await store.list_runs(JOB_ID)
    assert [run.id for run in runs] == ["run-1", "run-2", "run-3"]
    assert all(run.job_id == JOB_ID for run in runs)
    assert len(await store.list_runs(JOB_ID, limit=2)) == 2


async def test_store_returns_empty_for_missing_database(make_executor, tmp_path) -> None:
    store = CronStore(make_executor(hermes_home=str(tmp_path)), "")
    assert await store.list_runs(JOB_ID) == []


async def test_store_get_run_returns_single_row(context: PluginContext) -> None:
    store = CronStore(context.executor, "")
    run = await store.get_run(JOB_ID, "run-1")
    assert run is not None
    assert run.id == "run-1"
    assert run.status == "completed"
    assert run.finished_at == "2026-08-15T11:51:51+00:00"
    assert await store.get_run(JOB_ID, "does-not-exist") is None


async def test_store_find_run_output_matches_nearest_file(
    context: PluginContext, fake_hermes_cli
) -> None:
    """The output files carry only a save-time wall clock, so the match is by
    proximity to the run's own timestamps -- run-1 finished at 11:51:51 and
    the seeded file is 11:51:50 (1s delta); run-3 finished at 09:51:55, hours
    away from the only file, so it must not be attributed to it."""
    fake_hermes_cli.write_run_output(JOB_ID, "2026-08-15_11-51-50.md", "# Run 1 output")
    store = CronStore(context.executor, "")

    run_1 = await store.get_run(JOB_ID, "run-1")
    assert run_1 is not None
    assert await store.find_run_output(JOB_ID, run_1) == (
        "2026-08-15_11-51-50.md",
        "# Run 1 output",
    )

    run_3 = await store.get_run(JOB_ID, "run-3")
    assert run_3 is not None
    assert await store.find_run_output(JOB_ID, run_3) is None


async def test_store_find_run_output_missing_dir_returns_none(context: PluginContext) -> None:
    run_1 = await CronStore(context.executor, "").get_run(JOB_ID, "run-1")
    assert run_1 is not None
    assert await CronStore(context.executor, "").find_run_output(JOB_ID, run_1) is None


def _seed_cron_session(fake_hermes_cli, session_id: str, started_at: str) -> None:
    """Insert a cron-agent session row into the fake state.db (id format
    ``cron_{job_id}_{%Y%m%d_%H%M%S}``, wall-clock run start)."""
    fake_hermes_cli.insert_session(
        {
            "id": session_id,
            "title": "Cron run",
            "source": "cron",
            "model": "deepseek-v4-flash",
            "started_at": started_at,
        }
    )


async def test_store_find_related_session_id_finds_nearest(
    context: PluginContext, fake_hermes_cli
) -> None:
    """The cron agent names its sessions with the run-start wall clock, so the
    resolution is a prefix-range scan on ``cron_{job_id}_`` ids plus a
    ``claimed_at``-proximity pick. run-1 was claimed at 11:51:48 and the
    seeded session started at the same wall-clock minute; run-3 was claimed
    hours earlier, so no session is close enough for it."""
    _seed_cron_session(
        fake_hermes_cli, "cron_aabbccddeeff_20260815_115148", "2026-08-15T11:51:48+00:00"
    )
    store = CronStore(context.executor, "")

    run_1 = await store.get_run(JOB_ID, "run-1")
    assert run_1 is not None
    assert await store.find_related_session_id(JOB_ID, run_1) == (
        "cron_aabbccddeeff_20260815_115148"
    )

    run_3 = await store.get_run(JOB_ID, "run-3")
    assert run_3 is not None
    assert await store.find_related_session_id(JOB_ID, run_3) is None


async def test_runs_tab_shows_seeded_rows(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    await user.should_see("Runs")
    user.find(marker="runs-tab").click()
    await user.should_see("failed")
    await user.should_see("report failed")
    assert user.find(marker="run-row").elements


async def test_runs_tab_empty_state(user: User, context: PluginContext, fake_hermes_cli) -> None:
    fake_hermes_cli.insert_job(
        {
            "id": "no-runs",
            "name": "No runs",
            "schedule_display": "manual",
            "enabled": True,
        }
    )
    web.build(context, [CronPlugin(context)])
    await user.open("/cron/no-runs")
    user.find(marker="runs-tab").click()
    await user.should_see("No runs yet.")


async def test_run_row_click_navigates_to_run_detail(
    user: User, context: PluginContext, fake_hermes_cli
) -> None:
    fake_hermes_cli.write_run_output(JOB_ID, "2026-08-15_11-51-50.md", "# Run 1 output")
    _seed_cron_session(
        fake_hermes_cli, "cron_aabbccddeeff_20260815_115148", "2026-08-15T11:51:48+00:00"
    )
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}")
    user.find(marker="runs-tab").click()
    await user.should_see("failed")
    user.find(marker="run-row").click()
    await user.should_see("Run 1 output", retries=10)
    assert user.find(marker="run-session-link").elements


async def test_run_detail_page_fallbacks_when_no_output_or_session(
    user: User, context: PluginContext
) -> None:
    """run-3 failed and never wrote an output file nor spawned a session, so
    the detail page must fall back to the muted placeholders."""
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}/runs/run-3")
    await user.should_see("No output file saved for this run.", retries=10)
    await user.should_see("No related session.", retries=10)


async def test_run_detail_not_found(user: User, context: PluginContext) -> None:
    web.build(context, [CronPlugin(context)])
    await user.open(f"/cron/{JOB_ID}/runs/does-not-exist")
    await user.should_see("Run not found", retries=10)
