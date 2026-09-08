"""The web UI shell: shared frame, nav state, and page registration.

This is the boundary plugins are allowed to depend on. ``hermes_nicegui.app``
owns *process* bootstrap (reading ``Settings()`` from the environment,
building the real ``HermesClient``, calling ``ui.run``); this module owns
*page* bootstrap and knows nothing about where its ``PluginContext`` came
from. That split is what lets :func:`build` be called directly from a test
with a fake context and no environment variables involved, instead of going
through the whole process entrypoint.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING

from loguru import logger as _logger
from nicegui import app, ui

from hermes_nicegui.auth import ReadStateStore, UserStore
from hermes_nicegui.plugin import NavItem, Plugin, PluginContext, load_plugins
from hermes_nicegui.store import HermesStore, build_store

if TYPE_CHECKING:
    from collections.abc import Iterator

    from hermes_nicegui.config import Settings
    from hermes_nicegui.executor import HermesExecutor
    from hermes_nicegui.gateway import HermesClient

_PROFILE_KEY = "profile"


def current_profile() -> str:
    """The active Hermes profile for this browser session.

    Backed by ``app.storage.user`` (a signed cookie + server-side dict,
    keyed by the browser's session cookie -- the same storage
    ``AuthMiddleware``/the login flow already use) rather than
    ``app.storage.client``. ``client`` storage is scoped to the current
    *connection*, not the tab: NiceGUI's own docs say outright that it "is
    discarded when the connection to the client is lost through a page
    reload or a navigation" -- and since every page here is a distinct
    ``@ui.page`` route, an ordinary click from one page to another *is* a
    navigation. Using it here meant the selected profile silently reverted
    to the default on every single page change, confirmed live (not caught
    by the test suite, which doesn't reproduce a real browser's navigation
    behavior). ``app.storage.tab`` would persist correctly but isn't
    readable this early either -- it needs the client's websocket already
    connected, and this is read synchronously in ``frame()``'s header,
    before most pages await that connection.

    The trade-off: the active profile is now per browser *session*
    (shared across tabs, like ``app.storage.user["authenticated"]`` already
    is), not per tab. Empty string means the default profile, matching the
    convention `hermes_cli`/the dashboard both already use.
    """
    try:
        return app.storage.user.get(_PROFILE_KEY, "")
    except RuntimeError:  # pragma: no cover - storage disabled in some test setups
        return ""


def set_current_profile(name: str) -> None:
    try:
        app.storage.user[_PROFILE_KEY] = name
    except RuntimeError:  # pragma: no cover - storage disabled in some test setups
        pass


def current_username() -> str:
    """The username logged in for this browser session, or ''."""
    try:
        return app.storage.user.get("username", "")
    except RuntimeError:  # pragma: no cover - storage disabled in some test setups
        return ""


def current_executor() -> HermesExecutor:
    """``state.executor``, narrowed to non-``None``.

    It's only ``None`` before ``build()`` has run once, which never happens
    while a page is actually being served -- this just gives callers a
    non-Optional type instead of repeating the same assertion everywhere.
    """
    assert state.executor is not None, "web.build() must run before pages are served"
    return state.executor


def current_store() -> HermesStore:
    """``sessions``/``cron``/``kanban`` reads and mutations for whichever
    profile is active in this browser tab -- the one place plugin code
    reaches for "the hermes daemon and profiles" instead of separately
    pulling ``current_executor()`` and ``current_profile()`` at every call
    site (see ``hermes_nicegui.store``).
    """
    return build_store(current_executor(), current_profile())


class AppState:
    """Holds the assembled plugin set and nav items for the running app.

    A single process-wide instance (``state`` below), on purpose: nav items
    and settings are shared across every user session, not per-user data, so
    there is nothing to gain by threading them through every page function.
    :func:`build` resets it in place, which is what makes calling it again
    from a second test safe.
    """

    def __init__(self) -> None:
        self.settings: Settings | None = None
        self.client: HermesClient | None = None
        self.executor: HermesExecutor | None = None
        self.user_store: UserStore | None = None
        self.read_state_store: ReadStateStore | None = None
        self.profiles: list[str] = []
        self.plugins: list[Plugin] = []
        self.nav_items: list[NavItem] = []
        self.logger = _logger


state = AppState()


def build(
    context: PluginContext,
    plugins: list[Plugin] | None = None,
    profiles: list[str] | None = None,
) -> AppState:
    """Register the home page and every plugin's pages against ``state``.

    ``plugins`` are already-constructed ``Plugin`` instances. Pass an
    explicit list to register exactly the plugins a test cares about; omit
    it (the production path, see ``hermes_nicegui.app``) to discover the
    installed set through the ``hermes_nicegui.plugins`` entry-point group.

    ``profiles`` populates the profile switcher. It's a plain list rather
    than something ``build`` fetches itself, since fetching it means running
    ``hermes profile list`` (async, a subprocess) and ``build`` stays sync
    so every existing test -- which calls it directly, synchronously -- is
    unaffected; the real app fetches it in ``app.py``'s (async) startup
    handler and passes it in.

    Safe to call more than once in the same process: state is reset before
    each build, which is what the test suite relies on -- NiceGUI's ``user``
    test fixture resets the route table between tests, and each test calls
    ``build`` fresh with the plugin set it wants.
    """
    state.settings = context.settings
    state.client = context.client
    state.executor = context.executor
    # Constructed unconditionally (cheap -- just opens/creates a small SQLite
    # file) so `/login` works the same whether or not `AuthMiddleware` is
    # installed; the middleware is only ever added in `app.py::main`, never
    # here, so tests calling `build()` directly are never gated by it.
    state.user_store = UserStore(context.settings.data_dir_path / "users.db")
    state.read_state_store = ReadStateStore(context.settings.data_dir_path / "users.db")
    state.profiles = profiles or []
    state.logger = context.logger
    state.plugins = plugins if plugins is not None else load_plugins(context)
    state.nav_items = []

    for plugin in state.plugins:
        try:
            plugin.register()
        except Exception:
            state.logger.exception("plugin {} failed to register", plugin.name)
            continue
        for item in plugin.nav_items():
            if item.route not in {nav.route for nav in state.nav_items}:
                state.nav_items.append(item)

    _register_home()
    _register_login_page()

    state.logger.info(
        "hermes-nicegui ready: {} plugins, {} nav items",
        len(state.plugins),
        len(state.nav_items),
    )
    return state


def _apply_theme() -> None:
    """Brand colors + dark mode (follows ``state.settings.ui_dark``).

    Shared between ``frame()`` and the login page -- the login page renders
    before any plugin page (and outside ``frame()`` entirely, since it has
    no nav drawer/profile switcher), but should still match the rest of the
    app's theme rather than defaulting to light mode regardless of
    ``HERMES_UI_DARK``.
    """
    ui.colors(primary="#4F46E5", secondary="#0EA5E9")
    if state.settings and state.settings.ui_dark:
        ui.dark_mode().enable()
    else:
        ui.dark_mode().disable()


async def _new_session() -> None:
    """Create a session via the gateway client and open it (top-bar button)."""
    client = state.client
    if client is None:
        ui.notify("Gateway client unavailable", type="negative")
        return
    try:
        session = await client.create_session()
    except Exception as exc:  # noqa: BLE001 - surface any gateway failure in the UI
        ui.notify(f"Failed to start session: {exc}", type="negative")
        return
    ui.navigate.to(f"/sessions/{session.id}")


@contextmanager
def frame(*, active: str = "") -> Iterator[None]:
    """Shared page frame: header + left nav drawer + content column.

    Every plugin page wraps its content in ``with frame(...):`` so the shell
    stays consistent across modules. ``active`` highlights the current route.
    Dark mode follows ``state.settings.ui_dark``.
    """
    _apply_theme()
    with ui.header():
        with ui.row().classes("items-center"):
            # `ui.button`'s `color` param sets Quasar's own `color` prop only
            # for its whitelisted Quasar/Tailwind color names; "white" isn't
            # one, so it falls back to an inline `background-color: white`
            # style instead -- a solid white circle behind the icon (`round`),
            # not the flat white icon a `flat` button implies. A `text-white`
            # class sets the icon's color directly with no background at all.
            # The default `color` is 'primary', which on this already
            # primary-colored header made the icon blend into invisibility.
            ui.button(icon="menu", on_click=lambda: drawer.toggle()).props(
                "flat round dense"
            ).classes("text-white")
            ui.icon("device_hub")
            ui.label("Hermes")
            ui.space()
            ui.button(icon="add", on_click=_new_session).props(
                "flat round dense"
            ).classes("text-white").tooltip("New session").mark(
                "topbar-new-session-button"
            )
            if state.profiles:
                ui.select(
                    state.profiles,
                    value=current_profile() or "default",
                    on_change=lambda e: (set_current_profile(e.value), ui.navigate.reload()),
                ).props("dense outlined dark options-dense").classes("text-white w-40").tooltip(
                    "Active Hermes profile"
                ).mark("profile-select")
            if state.settings and state.settings.auth_enabled:
                username = app.storage.user.get("username")
                if username:
                    ui.label(username).classes("text-white text-xs self-center q-mx-sm")
                ui.button(
                    icon="logout",
                    on_click=lambda: (app.storage.user.clear(), ui.navigate.to("/login")),
                ).props("flat round dense").classes("text-white").tooltip("Log out").mark(
                    "logout-button"
                )

    # `value` deliberately left unset: NiceGUI opens the drawer above the
    # 1024px breakpoint and collapses it to a toggleable overlay below it.
    # Forcing `value=True` (the old code) defeated that and squeezed every
    # page's content into a sliver next to a permanently-open drawer on
    # phone-width screens.
    #
    # `fixed=True` pins the drawer to the viewport as its own scroll
    # region, independent of the page content -- with the default
    # `fixed=False` the drawer sits in normal document flow and scrolls
    # away with the page on a long transcript.
    drawer = ui.left_drawer(fixed=True)
    with drawer:
        for item in state.nav_items:
            # A flat, full-width button rather than a ``ui.list``/``ui.item``:
            # the drawer's own default CSS is a flex column with
            # `align-items: flex-start` (see `.nicegui-drawer` in NiceGUI's
            # stylesheet), so a plain item shrinks to its label's width
            # instead of filling the drawer -- most of the row then looks
            # like empty drawer background but isn't actually clickable.
            # `w-full` + `justify-start` makes the whole row both look and
            # behave like the nav entry it is.
            ui.button(
                item.label,
                icon=item.icon,
                color="primary" if item.route == active else None,
                on_click=partial(ui.navigate.to, item.route),
            ).props("flat no-caps align=left").classes("w-full justify-start")

    with ui.column().classes("w-full"):
        yield


def _register_home() -> None:
    """Register the root landing page with quick links to plugins."""

    @ui.page("/", title="Hermes")
    def home_page() -> None:
        with frame():
            ui.label("Hermes NiceGUI")
            ui.label("Modular web UI for the Hermes Agent gateway.")
            with ui.row():
                for item in state.nav_items:
                    with ui.link(target=item.route).classes("no-underline"), ui.card():
                        with ui.row().classes("items-center gap-2"):
                            if item.icon:
                                ui.icon(item.icon)
                            ui.label(item.label)


def _register_login_page() -> None:
    """Single ``/login`` route: a first-run "create admin account" form when
    no user exists yet (``UserStore.has_any_user``), a plain login form
    otherwise. Deliberately no auth check of its own -- gating is
    ``AuthMiddleware``'s job (``hermes_nicegui.auth``), not this page's; it
    just needs to render and work regardless of whether that middleware is
    installed, which is what keeps it testable via ``web.build()`` alone.
    """

    @ui.page("/login", title="Hermes — Log in")
    async def login_page() -> None:
        store = state.user_store
        assert store is not None
        _apply_theme()
        first_run = not await asyncio.to_thread(store.has_any_user)

        with ui.card().classes("absolute-center w-full max-w-sm"):
            ui.label("Hermes").classes("text-lg font-bold")
            ui.label("Create the admin account" if first_run else "Log in").classes(
                "text-sm opacity-70 q-mb-sm"
            )
            username_input = (
                ui.input("Username")
                .props("outlined dense autofocus")
                .classes("w-full")
                .mark("login-username")
            )
            password_input = (
                ui.input("Password", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full")
                .mark("login-password")
            )
            confirm_input = (
                ui.input("Confirm password", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full")
                .mark("login-confirm-password")
                if first_run
                else None
            )
            error_label = ui.label("").classes("text-negative text-xs")

            async def submit() -> None:
                error_label.set_text("")
                new_username = (username_input.value or "").strip()
                password = password_input.value or ""
                if not new_username or not password:
                    error_label.set_text("Username and password are required")
                    return
                if first_run:
                    if len(password) < 8:
                        error_label.set_text("Password must be at least 8 characters")
                        return
                    if password != (confirm_input.value if confirm_input else ""):
                        error_label.set_text("Passwords do not match")
                        return
                    await asyncio.to_thread(store.create_user, new_username, password)
                    app.storage.user.update({"authenticated": True, "username": new_username})
                    ui.navigate.to("/")
                    return
                ok = await asyncio.to_thread(store.verify, new_username, password)
                if not ok:
                    error_label.set_text("Invalid username or password")
                    return
                app.storage.user.update({"authenticated": True, "username": new_username})
                ui.navigate.to("/")

            password_input.on("keydown.enter", submit)
            if confirm_input is not None:
                confirm_input.on("keydown.enter", submit)
            ui.button("Create account" if first_run else "Log in", on_click=submit).classes(
                "w-full"
            ).mark("login-submit")
