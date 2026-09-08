"""Shared offset/limit pagination: one ``Pager`` (page number -> ``offset``/
``limit`` kwargs) and one ``render_pager`` (a numbered ``ui.pagination``
control) reused by every plugin list (sessions, cron, kanban) instead of each
growing its own "load more" button and ad hoc page-tracking dict.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nicegui import ui

DEFAULT_PAGE_SIZE = 30


@dataclass
class Pager:
    """Tracks the current (1-indexed) page for one list. ``offset``/``limit``
    are what store methods actually take; the page number is just what a
    numbered pagination control needs."""

    limit: int = DEFAULT_PAGE_SIZE
    page: int = 1

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit

    def reset(self) -> None:
        self.page = 1


def render_pager(pager: Pager, total: int, on_change: Callable[[], Any]) -> None:
    """A numbered page control for `total` items at `pager.limit` each.

    Renders nothing when everything fits on one page -- most lists, most of
    the time -- rather than a pager stuck at "page 1 of 1".
    """
    max_page = max(1, -(-total // pager.limit))  # ceil(total / limit)
    if max_page <= 1:
        return

    def _change(e: Any) -> None:
        pager.page = e.value or 1
        on_change()

    ui.pagination(1, max_page, value=pager.page, on_change=_change).props(
        "direction-links"
    ).mark("page-control")
