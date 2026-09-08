"""Plugin architecture for hermes-nicegui.

Plugins are discovered through the ``hermes_nicegui.plugins`` entry-point
group. Each plugin exposes a ``Plugin`` subclass as its entry-point value;
the app instantiates it with a :class:`PluginContext` at startup and calls
``register()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from loguru import Logger

    from hermes_nicegui.config import Settings
    from hermes_nicegui.executor import HermesExecutor
    from hermes_nicegui.gateway import HermesClient

ENTRY_POINT_GROUP = "hermes_nicegui.plugins"


@dataclass
class NavItem:
    """A single navigation entry rendered in the app drawer."""

    route: str
    label: str
    icon: str = ""


@dataclass
class PluginContext:
    """Shared services handed to every plugin at startup."""

    settings: Settings
    logger: Logger
    executor: HermesExecutor
    # The gateway's bearer-token client. Nothing uses this anymore --
    # sessions/cron reach Hermes through `executor` (the CLI) instead, and
    # kanban has its own dashboard client -- but it's cheap to keep wired up
    # for now rather than ripping out working, tested code.
    client: HermesClient | None = None

    def log(self) -> Logger:
        return self.logger


class Plugin:
    """Base class for hermes-nicegui plugins.

    Subclasses must set ``name``, ``title``, ``icon`` and implement
    ``register``. ``nav_items`` defaults to a single entry pointing at
    ``route`` (may be overridden).
    """

    name: str = ""
    title: str = ""
    icon: str = ""
    route: str = "/"

    def __init__(self, context: PluginContext) -> None:
        self.context = context
        self.client = context.client
        self.settings = context.settings
        self.logger = context.logger

    def register(self) -> None:
        """Register pages and any other startup work with NiceGUI.

        Called once at app startup inside the ``ui.run`` startup phase.
        """
        raise NotImplementedError

    def nav_items(self) -> list[NavItem]:
        return [NavItem(route=self.route, label=self.title, icon=self.icon)]


def load_plugins(context: PluginContext) -> list[Plugin]:
    """Discover and instantiate plugins from the entry-point group.

    Plugins listed in ``settings.disabled_plugins`` are skipped.
    """
    plugins: list[Plugin] = []
    disabled = set(context.settings.disabled_plugins)
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name in disabled:
            logger.info("plugin disabled: {}", entry_point.name)
            continue
        try:
            cls = entry_point.load()
            plugin = cls(context)
            plugins.append(plugin)
            logger.info("plugin loaded: {} ({})", plugin.name, entry_point.name)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("failed to load plugin {}: {}", entry_point.name, exc)
    return plugins
