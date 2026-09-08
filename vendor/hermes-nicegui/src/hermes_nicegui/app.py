"""Process entrypoint: reads Settings from the environment and runs NiceGUI.

Everything about *what pages exist* lives in :mod:`hermes_nicegui.web`. This
module's only job is assembling the real ``PluginContext`` (real ``Settings``
from the environment, a real ``HermesClient``) and handing it to
``web.build``. Plugins should never import from here -- ``hermes_nicegui.web``
is the public surface they and the test suite depend on instead.
"""

from __future__ import annotations

from loguru import logger
from nicegui import app, ui

from hermes_nicegui import web
from hermes_nicegui.auth import AuthMiddleware, load_or_create_secret
from hermes_nicegui.config import Settings
from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.gateway import HermesClient
from hermes_nicegui.logging import setup_logging
from hermes_nicegui.plugin import PluginContext


async def _bootstrap(settings: Settings) -> None:
    """Build the real plugin context and register pages.

    Registered as a NiceGUI startup handler so that plugin ``@ui.page``
    decorators run inside the ``on_startup`` phase (required in script mode).
    Async because listing profiles runs the ``hermes`` CLI as a subprocess.
    """
    setup_logging(settings.log_level)
    client = HermesClient(**settings.client_kwargs())
    executor = HermesExecutor(
        mode=settings.exec_mode,
        hermes_bin=settings.cli_bin,
        ssh_target=settings.ssh_target,
        ssh_options=settings.ssh_options_list,
        hermes_home=settings.hermes_home,
        remote_python_bin=settings.ssh_python_bin,
        dashboard_url=settings.kanban_url,
        dashboard_username=settings.kanban_username,
        dashboard_password=settings.kanban_password,
    )
    try:
        profiles = await executor.list_profile_names()
    except Exception:
        logger.exception("failed to list Hermes profiles; profile switcher will be empty")
        profiles = []
    context = PluginContext(client=client, settings=settings, logger=logger, executor=executor)
    web.build(context, profiles=profiles)


def main() -> None:
    """Console entry point (``hermes-nicegui``)."""
    settings = Settings()
    # Must happen *before* `ui.run()`: it adds Starlette's own
    # `SessionMiddleware` as a side effect of `storage_secret=...`, and
    # `Starlette.add_middleware` prepends -- the last-added middleware ends
    # up outermost/first-to-run. Added here, `SessionMiddleware` ends up
    # outermost and attaches `request.session` before `AuthMiddleware`
    # runs. Added after `ui.run()` (e.g. from inside `_bootstrap`, which
    # only runs once the app has already started) would raise
    # `RuntimeError: Cannot add middleware after an application has started`.
    if settings.auth_enabled:
        app.add_middleware(AuthMiddleware)
    app.on_startup(lambda: _bootstrap(settings))
    ui.run(
        title="Hermes",
        host=settings.ui_host,
        port=settings.ui_port,
        storage_secret=load_or_create_secret(settings.data_dir_path / "storage_secret"),
        reload=settings.ui_reload,
        show=False,
    )
