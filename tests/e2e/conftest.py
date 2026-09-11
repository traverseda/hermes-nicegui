"""Playwright E2E test fixtures for hermes-nicegui sessions plugin."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import pytest
from playwright.async_api import async_playwright

# Use a writable temp dir — /tmp on this host is a read-only tmpfs.
_TMPDIR = Path("/var/lib/hermes/.hermes/tmp")
_TMPDIR.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TMPDIR", str(_TMPDIR))
os.environ.setdefault("TMP", str(_TMPDIR))
os.environ.setdefault("TEMP", str(_TMPDIR))
os.environ.setdefault("TEMPDIR", str(_TMPDIR))

# The Chrome binary for NixOS — Playwright's bundled Chromium fails on NixOS.
CHROME_EXEC = "/var/lib/hermes/.local/bin/google-chrome"


def _make_hermes_home(tmp_dir: Path) -> Path:
    """Create a minimal state.db with test sessions and messages."""
    hermes_home = tmp_dir / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)

    import sqlite3

    path = hermes_home / "state.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, title TEXT, source TEXT, model TEXT,
    started_at REAL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL, pinned INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,
    last_activity_at REAL, last_activity_description TEXT, last_activity_provenance TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,
    finish_reason TEXT, reasoning TEXT,
    active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0
);
"""
    )
    con.executemany(
        "INSERT INTO sessions (id, title, source, model, started_at, ended_at, end_reason, "
        "message_count, tool_call_count, input_tokens, output_tokens, estimated_cost_usd, "
        "pinned, archived, last_activity_at, last_activity_description, last_activity_provenance) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 0, 0, 0, NULL, 0, 0, ?, ?, NULL)",
        [
            ("sess-1", "First session", "webui", "deepseek-v4-flash", 3,
             1786620319.0, "sequential tool running (30s): terminal"),
            ("sess-2", "Cron run", "cron", "deepseek-v4-flash", 0,
             1786610000.0, None),
        ],
    )
    con.executemany(
        "INSERT INTO messages (id, session_id, role, content, tool_call_id, "
        "tool_calls, tool_name, timestamp, finish_reason, reasoning) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL)",
        [
            (1, "sess-1", "user", "How do I access your API?", 1786620000.0),
            (2, "sess-1", "assistant", "Here is the answer.", 1786620010.0),
            (3, "sess-1", "assistant", "Used a tool to check.", 1786620015.0),
        ],
    )
    con.commit()
    con.close()
    return hermes_home


@pytest.fixture
def e2e_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Temporary directory for E2E test data (state.db, users.db, etc.)."""
    return tmp_path_factory.mktemp("e2e-data")


@pytest.fixture(autouse=True)
async def launch_browser():
    """Launch a Playwright browser for the test session.

    Uses system Chrome (required on NixOS) rather than Playwright's bundled
    Chromium which fails due to stub-ld restrictions.
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path=CHROME_EXEC,
            headless=True,
        )
        yield browser
        await browser.close()


@pytest.fixture
async def context_page(launch_browser):
    """Page fixture: creates a new context + page for each test.

    Each test gets an isolated context (cookies, storage) so tests
    don't interfere with each other.
    """
    context = await launch_browser.new_context()
    try:
        context.set_default_timeout(10000)
        page = await context.new_page()
        yield page
    finally:
        await context.close()


@pytest.fixture
def chrome_executable() -> str:
    """Return the path to the system Chrome binary."""
    return CHROME_EXEC
