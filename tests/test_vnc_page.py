"""UI tests for the VNC plugin: page rendering (noVNC assets absent in tests),
the authenticated /vnc/token POST, and the /vnc/ws websocket's token gate."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest
from loguru import logger
from nicegui import core, ui
from nicegui.testing import User
from nicegui.testing.general import nicegui_reset_globals, prepare_simulation
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from hermes_nicegui import web
from hermes_nicegui.auth import AuthMiddleware
from hermes_nicegui.config import Settings
from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.vnc import VncPlugin
from hermes_nicegui.plugins.vnc.logic import register_routes


def _make_vnc_plugin(tmp_path: Path, *, vnc_port: int = 59999) -> VncPlugin:
    settings = Settings(vnc_password_file=str(tmp_path / "passwd"), vnc_state_dir=str(tmp_path), vnc_port=vnc_port)
    return VncPlugin(PluginContext(settings=settings, logger=logger, executor=HermesExecutor()))


def _make_novnc_dir(tmp_path: Path) -> Path:
    """Create a minimal noVNC directory structure for happy-path tests."""
    core_dir = tmp_path / "core"
    core_dir.mkdir(parents=True)
    (core_dir / "rfb.js").write_text("")  # minimal file
    return tmp_path


# -- page rendering ------------------------------------------------------------


async def test_vnc_nav_item_present(user: User, tmp_path, make_context) -> None:
    context = make_context(vnc_state_dir=str(tmp_path), vnc_novnc_dir="")
    web.build(context, [VncPlugin(context)])
    await user.open("/")
    await user.should_see("VNC")


async def test_vnc_page_renders(user: User, tmp_path, make_context) -> None:
    novnc_dir = _make_novnc_dir(tmp_path / "novnc")
    context = make_context(vnc_state_dir=str(tmp_path), vnc_novnc_dir=str(novnc_dir), vnc_port=59999)
    web.build(context, [VncPlugin(context)])
    await user.open("/vnc")
    await user.should_see("VNC viewer")
    await user.should_see("display offline")
    assert user.find(marker="vnc-connect-button").elements
    assert user.find(marker="vnc-canvas").elements  # canvas present when assets OK


async def test_vnc_page_renders_without_novnc_assets(user: User, tmp_path, make_context) -> None:
    context = make_context(vnc_state_dir=str(tmp_path), vnc_novnc_dir="")
    web.build(context, [VncPlugin(context)])
    await user.open("/vnc")
    await user.should_see("noVNC client assets missing")


# -- /vnc/token endpoint -------------------------------------------------------


@asynccontextmanager
async def _simulated_app_with_vnc_routes(tmp_path: Path, *, with_auth_middleware: bool = False):
    """Mirrors ``tests/test_auth.py``'s ``_simulated_app_with_auth_middleware``,
    with the vnc routes registered instead of a dummy protected page."""
    os.environ["NICEGUI_USER_SIMULATION"] = "true"
    try:
        with nicegui_reset_globals():
            prepare_simulation()
            if with_auth_middleware:
                core.app.add_middleware(AuthMiddleware)
            ui.run(storage_secret="test-secret")
            register_routes(_make_vnc_plugin(tmp_path))
            async with core.app.router.lifespan_context(core.app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(core.app), base_url="http://test"
                ) as client:
                    yield client
    finally:
        os.environ.pop("NICEGUI_USER_SIMULATION", None)


async def test_vnc_token_returns_token_and_password(tmp_path) -> None:
    (tmp_path / "passwd").write_text("hunter2\n")
    async with _simulated_app_with_vnc_routes(tmp_path) as client:
        first = await client.post("/vnc/token")
        assert first.status_code == 200
        body = first.json()
        assert body["token"]
        assert body["password"] == "hunter2"
        second = await client.post("/vnc/token")
        assert second.status_code == 200
        assert second.json()["token"] != body["token"]


async def test_vnc_token_503_without_password_file(tmp_path) -> None:
    async with _simulated_app_with_vnc_routes(tmp_path) as client:
        resp = await client.post("/vnc/token")
        assert resp.status_code == 503


async def test_vnc_token_requires_auth(tmp_path) -> None:
    (tmp_path / "passwd").write_text("hunter2\n")
    async with _simulated_app_with_vnc_routes(tmp_path, with_auth_middleware=True) as client:
        resp = await client.post("/vnc/token", follow_redirects=False)
        assert resp.status_code in (302, 303, 307)
        assert resp.headers["location"] == "/login"


# -- /vnc/ws websocket gate ----------------------------------------------------


@contextmanager
def _testclient_with_vnc_routes(tmp_path: Path):
    os.environ["NICEGUI_USER_SIMULATION"] = "true"
    try:
        with nicegui_reset_globals():
            prepare_simulation()
            ui.run(storage_secret="test-secret")
            register_routes(_make_vnc_plugin(tmp_path))
            with TestClient(core.app) as client:
                yield client
    finally:
        os.environ.pop("NICEGUI_USER_SIMULATION", None)


def test_vnc_ws_rejects_bad_token(tmp_path) -> None:
    (tmp_path / "passwd").write_text("hunter2\n")
    with _testclient_with_vnc_routes(tmp_path) as client:
        with client.websocket_connect("/vnc/ws") as ws:
            ws.send_text(json.dumps({"token": "bogus"}))
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 1008


def test_vnc_ws_rejects_non_json_first_message(tmp_path) -> None:
    (tmp_path / "passwd").write_text("hunter2\n")
    with _testclient_with_vnc_routes(tmp_path) as client:
        with client.websocket_connect("/vnc/ws") as ws:
            ws.send_text("not json")
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_text()
            assert exc_info.value.code == 1008
