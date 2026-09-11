#!/usr/bin/env python3
"""Standalone test server for hermes-nicegui E2E tests.

Starts the NiceGUI app with FakeHermes as the gateway transport,
writes users.db to set up auth, then runs the server on a given port.

Usage:
    python tests/e2e/serve.py [--port PORT] [--data-dir DIR]

The server creates an admin account on first start and serves on PORT.
Data (state.db, users.db) goes to DATA_DIR (default: /tmp/e2e-test-state).
"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# Make hermes_nicegui importable from the src/ directory
# serve.py is at tests/e2e/serve.py → parent is tests/ → parent is project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from loguru import logger
from hermes_nicegui import web
from hermes_nicegui.auth import UserStore
from hermes_nicegui.config import Settings
from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.gateway import HermesClient
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin

# Import test fixtures directly (not from the conftest module to avoid
# pytest-only dependencies leaking into the server process).
import httpx


from collections.abc import AsyncIterator
import asyncio


class _AsyncSSEStream(httpx.AsyncByteStream):
    """Delayed SSE body compatible with HTTPX's async transport interface."""

    def __init__(self, events: list, delay: float) -> None:
        self.events = events
        self.delay = delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for name, data in self.events:
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            if self.delay > 0:
                await asyncio.sleep(self.delay)


class FakeHermes:
    """In-memory stand-in for the Hermes API server.

    This mirrors the same FakeHermes used by tests/conftest.py so the
    E2E tests exercise the same SSE event vocabulary the production UI
    consumes.
    """

    def __init__(self, hermes_home: Path) -> None:
        self.hermes_home = hermes_home
        self.sessions: list = []
        self.messages: dict = {}
        self.stream_events: list = []
        self.jobs: list = []
        self.stream_delay: float = 0.0
        self.stop_calls: list = []
        self.stop_session_calls: list = []
        self.reset()

    def reset(self) -> None:
        self.stop_session_calls = []
        self.sessions = [
            {
                "id": "sess-1",
                "title": "First session",
                "source": "webui",
                "model": "deepseek-v4-flash",
                "message_count": 3,
                "tool_call_count": 1,
                "input_tokens": 100,
                "output_tokens": 50,
                "estimated_cost_usd": 0.001,
                "last_active": 1786620319.0,
                "preview": "hello",
            },
            {
                "id": "sess-2",
                "title": "Cron run",
                "source": "cron",
                "model": "deepseek-v4-flash",
                "message_count": 9,
                "tool_call_count": 4,
                "input_tokens": 900,
                "output_tokens": 300,
                "estimated_cost_usd": 0.01,
                "last_active": 1786610000.0,
                "preview": "nightly check",
            },
        ]
        self.messages = {
            "sess-1": [
                {
                    "id": 1,
                    "session_id": "sess-1",
                    "role": "user",
                    "content": "How do I access your API?",
                    "timestamp": 1786620000.0,
                },
                {
                    "id": 2,
                    "session_id": "sess-1",
                    "role": "assistant",
                    "content": "Here is the answer.",
                    "timestamp": 1786620010.0,
                },
                {
                    "id": 3,
                    "session_id": "sess-1",
                    "role": "assistant",
                    "content": "Used a tool to check.",
                    "timestamp": 1786620015.0,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"command": "ls"}',
                            },
                        }
                    ],
                    "reasoning": "I needed to list files.",
                },
            ]
        }
        self.jobs = [
            {
                "id": "aabbccddeeff",
                "name": "Nightly report",
                "prompt": "Summarize yesterday's activity",
                "schedule_display": "0 3 * * *",
                "deliver": "local",
                "skills": [],
                "repeat": {"times": None, "completed": 5},
                "enabled": True,
                "state": "scheduled",
                "created_at": "2026-08-01T00:00:00+00:00",
                "next_run_at": "2026-08-14T03:00:00+00:00",
                "last_run_at": "2026-08-13T03:00:00+00:00",
                "last_status": "success",
                "last_error": None,
            }
        ]
        self.stream_events = [
            ("run.started", {"session_id": "sess-1", "run_id": "run_1"}),
            ("message.started", {"message": {"id": "msg_1", "role": "assistant"}}),
            ("tool.progress", {"tool_name": "_thinking", "delta": "thinking…"}),
            ("assistant.delta", {"delta": "Hello "}),
            ("assistant.delta", {"delta": "world"}),
            ("assistant.completed", {"content": "Hello world"}),
            ("run.completed", {"completed": True, "usage": {}}),
            ("done", {}),
        ]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        if method == "GET" and path == "/api/sessions":
            return self._json({"object": "list", "data": self.sessions, "has_more": False})
        if method == "POST" and path == "/api/sessions":
            body = json.loads(request.content) if request.content else {}
            new_id = "sess-new"
            session = {
                "id": new_id,
                "title": body.get("title"),
                "source": body.get("source") or "api_server",
                "model": "hermes-agent",
                "message_count": 0,
                "tool_call_count": 0,
                "estimated_cost_usd": None,
                "last_active": None,
                "preview": None,
            }
            self.sessions.append(session)
            self._mirror_new_session(session)
            return self._json({"object": "hermes.session", "session": session})
        if method == "GET" and path.startswith("/api/sessions/") and path.count("/") == 3:
            sid = path.split("/")[3]
            session = next((s for s in self.sessions if s["id"] == sid), None)
            if not session:
                return self._json({"error": "Session not found"}, status=404)
            return self._json({"object": "hermes.session", "session": session})
        if method == "GET" and path.startswith("/api/sessions/") and path.endswith("/messages"):
            sid = path.split("/")[3]
            return self._json({"object": "list", "data": self.messages.get(sid, [])})
        if method == "POST" and path == "/api/sessions/sess-1/model":
            return self._json({"model_lock": "accepted"})
        if method == "POST" and path == "/api/sessions/sess-1/fork":
            new_id = "sess-fork"
            session = {"id": new_id, "title": "forked", "source": "webui"}
            self.sessions.append(session)
            return self._json({"object": "hermes.session", "session": session})
        if method == "DELETE" and path == "/api/sessions/sess-1":
            return self._json({"deleted": True})
        if method == "POST" and path.endswith("/chat/stream"):
            sid = path.split("/")[3]
            body = json.loads(request.content) if request.content else {}
            self._record_turn(sid, body.get("input", ""))
            self._mirror_turn(sid, body.get("input", ""))
            if self.stream_delay > 0:
                return self._sse_stream(self.stream_events, self.stream_delay)
            return self._sse(self.stream_events)
        if method == "POST" and path.startswith("/v1/runs/") and path.endswith("/stop"):
            run_id = path.split("/")[3]
            self.stop_calls.append(run_id)
            self.stream_delay = 0.0
            return self._json({"run_id": run_id, "status": "stopping"})
        if method == "POST" and path.startswith("/api/sessions/") and path.endswith("/stop"):
            sid = path.split("/")[3]
            self.stop_session_calls.append(sid)
            for session in self.sessions:
                if session["id"] == sid:
                    session["last_activity_description"] = ""
                    session["last_activity_provenance"] = "unknown"
            if self._state_db_path().exists():
                con = sqlite3.connect(self._state_db_path())
                con.execute(
                    "UPDATE sessions SET last_activity_description = '', "
                    "last_activity_provenance = 'unknown' WHERE id = ?",
                    (sid,),
                )
                con.commit()
                con.close()
            return self._json({"session_id": sid, "status": "stopping"})
        if method == "GET" and path == "/api/jobs":
            return self._json({"jobs": self.jobs})
        return self._json({"error": f"unhandled {method} {path}"}, status=404)

    def _record_turn(self, session_id: str, input_text: str) -> None:
        final_content = next(
            (data.get("content", "") for name, data in self.stream_events if name == "assistant.completed"),
            "",
        )
        history = self.messages.setdefault(session_id, [])
        next_id = max((m["id"] for m in history), default=0) + 1
        history.append({
            "id": next_id,
            "session_id": session_id,
            "role": "user",
            "content": input_text,
            "timestamp": 1786620020.0,
        })
        history.append({
            "id": next_id + 1,
            "session_id": session_id,
            "role": "assistant",
            "content": final_content,
            "timestamp": 1786620021.0,
        })
        for session in self.sessions:
            if session["id"] == session_id:
                session["message_count"] = session.get("message_count", 0) + 2
                break

    def _state_db_path(self) -> Path:
        return self.hermes_home / "state.db"

    def _mirror_new_session(self, session: dict) -> None:
        if not self._state_db_path().exists():
            return
        con = sqlite3.connect(self._state_db_path())
        con.execute(
            "INSERT INTO sessions (id, title, source, model, "
            "started_at, ended_at, end_reason, message_count, tool_call_count, "
            "input_tokens, output_tokens, estimated_cost_usd, pinned, archived, "
            "last_activity_at, last_activity_description, last_activity_provenance) "
            "VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0, 0, 0, 0, NULL, 0, 0, NULL, ?, 'unknown')",
            (
                session["id"],
                session.get("title"),
                session.get("source"),
                session.get("model"),
                session.get("last_activity_description"),
            ),
        )
        con.commit()
        con.close()

    def _mirror_turn(self, session_id: str, input_text: str) -> None:
        if not self._state_db_path().exists():
            return
        final_content = next(
            (data.get("content", "") for name, data in self.stream_events if name == "assistant.completed"),
            "",
        )
        con = sqlite3.connect(self._state_db_path())
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0] + 1
        con.executemany(
            "INSERT INTO messages (id, session_id, role, content, "
            "tool_call_id, tool_calls, tool_name, timestamp, finish_reason, reasoning) "
            "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL)",
            [
                (next_id, session_id, "user", input_text, 1786620020.0),
                (next_id + 1, session_id, "assistant", final_content, 1786620021.0),
            ],
        )
        con.execute(
            "UPDATE sessions SET message_count = message_count + 2, "
            "last_activity_at = ? WHERE id = ?",
            (1786620021.0, session_id),
        )
        con.commit()
        con.close()

    def _json(self, payload: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=payload, headers={"Content-Type": "application/json"})

    def _sse(self, events: list) -> httpx.Response:
        body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    def _sse_stream(self, events: list, delay: float) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_AsyncSSEStream(events, delay),
            headers={"Content-Type": "text/event-stream"},
        )


# ---- state.db helpers (same schema as tests/conftest.py) ----

_SESSIONS_SCHEMA = """
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

_MESSAGE_COLUMNS = [
    "id",
    "session_id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "finish_reason",
    "reasoning",
]


def setup_auth(data_dir: Path) -> None:
    """Create users.db with an admin account so login works."""
    data_dir.mkdir(parents=True, exist_ok=True)
    users_db = data_dir / "users.db"
    store = UserStore(users_db)
    store.create_user("admin", "test1234")


def setup_state_db(hermes_home: Path) -> None:
    """Initialize state.db with test sessions and messages."""
    hermes_home.mkdir(parents=True, exist_ok=True)
    path = hermes_home / "state.db"
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript(_SESSIONS_SCHEMA)
    con.executemany(
        "INSERT INTO sessions (id, title, source, model, "
        "started_at, ended_at, end_reason, message_count, tool_call_count, "
        "input_tokens, output_tokens, estimated_cost_usd, pinned, archived, "
        "last_activity_at, last_activity_description, last_activity_provenance) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, 0, 0, 0, NULL, 0, 0, ?, ?, NULL)",
        [
            ("sess-1", "First session", "webui", "deepseek-v4-flash", 3,
             1786620319.0, "sequential tool running (30s): terminal"),
            ("sess-2", "Cron run", "cron", "deepseek-v4-flash", 0,
             1786610000.0, None),
        ],
    )
    con.executemany(
        "INSERT INTO messages (id, session_id, role, content, "
        "tool_call_id, tool_calls, tool_name, timestamp, finish_reason, reasoning) "
        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL, NULL)",
        [
            (1, "sess-1", "user", "How do I access your API?", 1786620000.0),
            (2, "sess-1", "assistant", "Here is the answer.", 1786620010.0),
            (3, "sess-1", "assistant", "Used a tool to check.", 1786620015.0),
        ],
    )
    con.commit()
    con.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E test server for hermes-nicegui")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--data-dir", type=str, default="/tmp/e2e-test-state")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    hermes_home = data_dir / "hermes"

    # Set up auth + state
    setup_auth(data_dir)
    setup_state_db(hermes_home)

    # Create FakeHermes
    hermes = FakeHermes(hermes_home)

    settings = Settings(
        gateway_url="http://127.0.0.1:9999",
        data_dir=str(data_dir),
        hermes_home=str(hermes_home),
        auth_enabled=True,
        ui_reload=False,
        ui_port=args.port,
    )

    async def _dummy_run_fn(argv: list[str]):
        """Dummy run_fn so HermesExecutor doesn't crash."""
        from hermes_nicegui.executor import CompletedResult
        return CompletedResult(0, "", "")

    executor = HermesExecutor(
        mode="local",
        hermes_bin="hermes",
        hermes_home=str(hermes_home),
        run_fn=_dummy_run_fn,
    )

    client = HermesClient(
        settings.gateway_url,
        settings.api_token,
        transport=httpx.MockTransport(hermes.handle),
    )

    context = PluginContext(client=client, settings=settings, logger=logger, executor=executor)
    web.build(context, plugins=[SessionsPlugin(context)])

    import os
    # NiceGUI requires NICEGUI_SCREEN_TEST_PORT to exist (reads it with
    # os.environ['…'], not .get()).  When the parent pytest process sets
    # it to '' we get int('') → ValueError.  When we remove it entirely
    # we get KeyError.  The fix: overwrite it with a real port before
    # ui.run().
    _nicegui_port = os.environ.get("NICEGUI_SCREEN_TEST_PORT", "")
    if not _nicegui_port:
        # No port set → run in normal mode.  Set it to the actual port
        # number so NiceGUI knows which port to listen on (it reads this
        # env var with os.environ['…'], not .get()).
        os.environ["NICEGUI_SCREEN_TEST_PORT"] = str(args.port)
    if "NICEGUI_USER_SIMULATION" in os.environ:
        del os.environ["NICEGUI_USER_SIMULATION"]

    from nicegui import ui
    ui.run(
        title="Hermes E2E",
        host="127.0.0.1",
        port=args.port,
        storage_secret="e2e-test-secret-key-12345",
        show=False,
        reload=False,
    )


def _dummy_run_fn():
    """Dummy run_fn so HermesExecutor doesn't crash."""
    from hermes_nicegui.executor import CompletedResult
    return CompletedResult(0, "", "")


if __name__ == "__main__":
    main()
