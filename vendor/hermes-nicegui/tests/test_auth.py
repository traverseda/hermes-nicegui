"""Tests for hermes_nicegui.auth: hashing, UserStore, AuthMiddleware, and
the /login page.

AuthMiddleware must be registered before `ui.run()` (see auth.py's own
docstring for why), which happens *inside* the `user` fixture's setup, before
a test body ever runs -- so `test_middleware_*` below don't use that fixture.
They instead replicate `nicegui.testing.user_simulation`'s own internals
(`nicegui_reset_globals` + `prepare_simulation` + `ui.run` + an ASGI test
client) with the one addition those internals don't expose a hook for:
adding our middleware in between. Login-page behavior doesn't depend on the
middleware being installed at all, so it's tested separately via the normal
`context`/`user` fixtures.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx
import pytest
from nicegui import core, ui
from nicegui.testing import User
from nicegui.testing.general import nicegui_reset_globals, prepare_simulation

from hermes_nicegui import web
from hermes_nicegui.auth import (
    AuthMiddleware,
    UserStore,
    hash_password,
    is_public_path,
    verify_password,
)
from hermes_nicegui.plugin import PluginContext

# -- hashing --------------------------------------------------------------


def test_hash_password_round_trips() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)


def test_hash_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_hash_password_uses_a_random_salt() -> None:
    assert hash_password("same password") != hash_password("same password")


def test_verify_password_rejects_garbage_stored_value() -> None:
    assert not verify_password("anything", "not-a-valid-stored-hash")


# -- UserStore --------------------------------------------------------------


def test_user_store_has_no_user_initially(tmp_path) -> None:
    store = UserStore(tmp_path / "users.db")
    assert not store.has_any_user()


def test_user_store_create_and_verify(tmp_path) -> None:
    store = UserStore(tmp_path / "users.db")
    store.create_user("alex", "hunter2hunter2")
    assert store.has_any_user()
    assert store.verify("alex", "hunter2hunter2")
    assert not store.verify("alex", "wrong")
    assert not store.verify("nobody", "hunter2hunter2")


def test_user_store_persists_across_instances(tmp_path) -> None:
    db_path = tmp_path / "users.db"
    UserStore(db_path).create_user("alex", "hunter2hunter2")
    reopened = UserStore(db_path)
    assert reopened.has_any_user()
    assert reopened.verify("alex", "hunter2hunter2")


# -- AuthMiddleware path matching (pure) -------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/login", True),
        ("/_nicegui/1.2.3/static/foo.js", True),
        ("/_nicegui_ws/socket.io", True),
        ("/", False),
        ("/sessions", False),
        ("/files/raw/etc/passwd", False),
        ("/files/archive/anything", False),
    ],
)
def test_is_public_path(path: str, expected: bool) -> None:
    assert is_public_path(path) is expected


# -- AuthMiddleware, full stack ---------------------------------------------


@asynccontextmanager
async def _simulated_app_with_auth_middleware():
    """Mirrors `nicegui.testing.user_simulation`'s own internals, with
    `AuthMiddleware` added before `ui.run()` -- the one thing that context
    manager has no hook for."""
    os.environ["NICEGUI_USER_SIMULATION"] = "true"
    try:
        with nicegui_reset_globals():
            prepare_simulation()
            core.app.add_middleware(AuthMiddleware)
            ui.run(storage_secret="test-secret")

            @ui.page("/protected")
            def protected_page() -> None:
                ui.label("secret content")

            @ui.page("/login")
            def login_page() -> None:
                ui.label("login form")

            async with core.app.router.lifespan_context(core.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(core.app), base_url="http://test"
                ) as client:
                    yield client
    finally:
        os.environ.pop("NICEGUI_USER_SIMULATION", None)


async def test_middleware_redirects_unauthenticated_request() -> None:
    async with _simulated_app_with_auth_middleware() as client:
        resp = await client.get("/protected", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert resp.headers["location"] == "/login"


async def test_middleware_lets_login_page_through() -> None:
    async with _simulated_app_with_auth_middleware() as client:
        resp = await client.get("/login", follow_redirects=False)
        assert resp.status_code == 200


# -- /login page --------------------------------------------------------


async def test_login_page_shows_setup_form_when_no_user(user: User, context: PluginContext) -> None:
    web.build(context, [])
    await user.open("/login")
    await user.should_see("Create the admin account")
    await user.should_see("Confirm password")


async def test_setup_creates_user_and_authenticates(user: User, context: PluginContext) -> None:
    web.build(context, [])
    await user.open("/login")
    await user.should_see("Create the admin account")

    user.find(marker="login-username").type("alex")
    user.find(marker="login-password").type("hunter2hunter2")
    user.find(marker="login-confirm-password").type("hunter2hunter2")
    user.find(marker="login-submit").click()

    await user.should_see("Hermes NiceGUI", retries=10)
    assert context.settings is not None
    store = UserStore(context.settings.data_dir_path / "users.db")
    assert store.verify("alex", "hunter2hunter2")


async def test_login_page_shows_login_form_when_user_exists(
    user: User, context: PluginContext
) -> None:
    assert context.settings is not None
    UserStore(context.settings.data_dir_path / "users.db").create_user("alex", "hunter2hunter2")
    web.build(context, [])
    await user.open("/login")
    await user.should_see("Log in")
    await user.should_not_see("Confirm password")


async def test_login_rejects_wrong_password(user: User, context: PluginContext) -> None:
    assert context.settings is not None
    UserStore(context.settings.data_dir_path / "users.db").create_user("alex", "hunter2hunter2")
    web.build(context, [])
    await user.open("/login")
    await user.should_see("Log in")

    user.find(marker="login-username").type("alex")
    user.find(marker="login-password").type("wrong password")
    user.find(marker="login-submit").click()

    await user.should_see("Invalid username or password", retries=10)


async def test_login_succeeds_with_correct_password(user: User, context: PluginContext) -> None:
    assert context.settings is not None
    UserStore(context.settings.data_dir_path / "users.db").create_user("alex", "hunter2hunter2")
    web.build(context, [])
    await user.open("/login")
    await user.should_see("Log in")

    user.find(marker="login-username").type("alex")
    user.find(marker="login-password").type("hunter2hunter2")
    user.find(marker="login-submit").click()

    await user.should_see("Hermes NiceGUI", retries=10)
