"""Costs plugin for provider spend and usage."""

from __future__ import annotations

from loguru import logger

from hermes_nicegui.plugin import Plugin, PluginContext

from .logic import CostProvider, build_providers
from .ui import register_pages


class CostsPlugin(Plugin):
    """Display spend summaries and recent usage for configured providers."""

    name = "costs"
    title = "Costs"
    icon = "payments"
    route = "/costs"

    def __init__(
        self, context: PluginContext, providers: list[CostProvider] | None = None
    ) -> None:
        super().__init__(context)
        self.providers = providers if providers is not None else build_providers(context.settings)

    def register(self) -> None:
        """Register the costs page."""
        register_pages(self)
        logger.info("costs plugin registered")
