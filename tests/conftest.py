"""Shared pytest fixtures: fake Hermes gateway via httpx.MockTransport, and
a fake `hermes` daemon's on-disk state (`hermes_home`) for the CLI-subprocess
and direct-SQLite/JSON read paths.

There is no ``main.py`` involved in a test (``main_file = ""`` in
``pyproject.toml``): a test builds a :class:`~hermes_nicegui.plugin.PluginContext`
via ``make_context``/``context``, constructs whichever plugin(s) it wants, and
calls ``hermes_nicegui.web.build(context, [...])`` itself. That keeps a test's
dependencies -- and which plugin it's actually exercising -- visible in the
test, instead of implicit in environment variables and entry-point discovery.

Sessions/cron/kanban *reads* (see ``hermes_nicegui.store``) go straight at a
real ``state.db``/``cron/jobs.json``/``kanban.db`` under a temp ``hermes_home``
directory -- a local-mode ``HermesExecutor`` pointed at it exercises the
*real* ``read_sqlite``/``read_json``, no fake subprocess involved. Only
*mutations* (rename/delete a session, cron actions, kanban writes) are faked
at a transport boundary (``FakeHermesCli.run_fn`` / ``FakeKanban``'s REST
handler) -- and each of those writes through to the same on-disk files a
subsequent read would see, mirroring how the real daemon keeps both in sync.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from loguru import logger

from hermes_nicegui.config import Settings
from hermes_nicegui.executor import CompletedResult, HermesExecutor
from hermes_nicegui.gateway import HermesClient
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.kanban.gateway import KanbanClient


class _AsyncSSEStream(httpx.AsyncByteStream):
    """Delayed SSE body compatible with HTTPX's async transport interface."""

    def __init__(self, events: list[tuple[str, dict]], delay: float) -> None:
        self.events = events
        self.delay = delay

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for name, data in self.events:
            yield f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()
            if self.delay > 0:
                await asyncio.sleep(self.delay)


class FakeHermes:
    """In-memory stand-in for the Hermes API server (session/job REST +
    chat streaming), plus a mirror of the same ``hermes_home/state.db``
    ``FakeHermesCli`` owns.

    In production, a gateway write (create a session, send a chat turn)
    and a direct SQLite read both ultimately look at the *same* daemon
    state -- see the kanban fake's identical comment below. Session
    *reads* in this app go through ``web.current_store()`` (direct SQLite),
    while *sending* a chat turn still goes through this gateway client, so
    a UI test that creates a session or sends a message and then expects
    the (re-read) page to show it depends on that being true here too.
    """

    def __init__(self, hermes_home: Path) -> None:
        self.hermes_home = hermes_home
        self.sessions: list[dict] = []
        self.messages: dict[str, list[dict]] = {}
        self.stream_events: list[tuple[str, dict]] = []
        self.jobs: list[dict] = []
        # Per-test knobs: a >0 stream_delay dribbles SSE events out slowly so
        # tests can exercise mid-stream controls (stop, queue); stop_calls
        # records every run id a stop was requested for.
        self.stream_delay: float = 0.0
        self.stop_calls: list[str] = []
        self.reset()

    def reset(self) -> None:
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
            (
                "tool.started",
                {"tool_name": "terminal", "preview": "ls", "args": {"command": "ls"}},
            ),
            ("tool.completed", {"tool_name": "terminal"}),
            ("assistant.delta", {"delta": "Hello "}),
            ("assistant.delta", {"delta": "world"}),
            ("assistant.completed", {"content": "Hello world"}),
            ("run.completed", {"completed": True, "usage": {}}),
            ("done", {}),
        ]

    # -- handler -----------------------------------------------------------

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
                "source": "api_server",
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
        if method == "GET" and path == "/api/jobs":
            return self._json({"jobs": self.jobs})
        if method == "POST" and path == "/api/jobs":
            body = json.loads(request.content) if request.content else {}
            job = {
                "id": "112233445566",
                "name": body.get("name", ""),
                "prompt": body.get("prompt", ""),
                "schedule_display": body.get("schedule", ""),
                "deliver": body.get("deliver") or "local",
                "skills": body.get("skills") or [],
                "repeat": {"times": body.get("repeat"), "completed": 0},
                "enabled": True,
                "state": "scheduled",
                "created_at": "2026-08-13T00:00:00+00:00",
                "next_run_at": "2026-08-14T00:00:00+00:00",
                "last_run_at": None,
                "last_status": None,
                "last_error": None,
            }
            self.jobs.append(job)
            return self._json({"job": job})
        if path.startswith("/api/jobs/"):
            parts = path.split("/")
            job_id = parts[3]
            job = next((j for j in self.jobs if j["id"] == job_id), None)
            action = parts[4] if len(parts) > 4 else None
            if method == "GET" and action is None:
                if not job:
                    return self._json({"error": "Job not found"}, status=404)
                return self._json({"job": job})
            if method == "PATCH" and action is None:
                if not job:
                    return self._json({"error": "Job not found"}, status=404)
                body = json.loads(request.content) if request.content else {}
                if "schedule" in body:
                    body["schedule_display"] = body.pop("schedule")
                if "repeat" in body:
                    body["repeat"] = {
                        "times": body["repeat"],
                        "completed": job["repeat"]["completed"],
                    }
                job.update(body)
                return self._json({"job": job})
            if method == "DELETE" and action is None:
                if not job:
                    return self._json({"error": "Job not found"}, status=404)
                self.jobs.remove(job)
                return self._json({"ok": True})
            if method == "POST" and action == "pause":
                if not job:
                    return self._json({"error": "Job not found"}, status=404)
                job["enabled"] = False
                job["state"] = "paused"
                return self._json({"job": job})
            if method == "POST" and action == "resume":
                if not job:
                    return self._json({"error": "Job not found"}, status=404)
                job["enabled"] = True
                job["state"] = "scheduled"
                return self._json({"job": job})
            if method == "POST" and action == "run":
                if not job:
                    return self._json({"error": "Job not found"}, status=404)
                job["last_run_at"] = "2026-08-13T12:00:00+00:00"
                job["last_status"] = "success"
                return self._json({"job": job})
        return self._json({"error": {"message": f"unhandled {method} {path}"}}, status=404)

    def _record_turn(self, session_id: str, input_text: str) -> None:
        """Append a user + assistant message, mimicking what a real Hermes
        server does when a ``/chat/stream`` turn completes -- needed so a
        follow-up ``GET .../messages`` (the reload after a live chat send)
        reflects the turn instead of silently dropping it.
        """
        final_content = next(
            (
                data.get("content", "")
                for name, data in self.stream_events
                if name == "assistant.completed"
            ),
            "",
        )
        history = self.messages.setdefault(session_id, [])
        next_id = max((m["id"] for m in history), default=0) + 1
        history.append(
            {
                "id": next_id,
                "session_id": session_id,
                "role": "user",
                "content": input_text,
                "timestamp": 1786620020.0,
            }
        )
        history.append(
            {
                "id": next_id + 1,
                "session_id": session_id,
                "role": "assistant",
                "content": final_content,
                "timestamp": 1786620021.0,
            }
        )
        for session in self.sessions:
            if session["id"] == session_id:
                session["message_count"] = session.get("message_count", 0) + 2
                break

    def _state_db_path(self) -> Path:
        return self.hermes_home / "state.db"

    def _mirror_new_session(self, session: dict) -> None:
        """Insert a gateway-created session into ``state.db`` too, so an
        immediate follow-up direct-SQLite read (opening the detail page
        after "New session") finds it -- see the class docstring.

        A no-op when ``state.db`` doesn't exist yet: gateway-only tests
        (``test_gateway.py``) never bring up a ``FakeHermesCli`` to create
        it, and have nothing that reads it back either.
        """
        if not self._state_db_path().exists():
            return
        con = sqlite3.connect(self._state_db_path())
        con.execute(
            f"INSERT INTO sessions ({','.join(_SESSION_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_SESSION_COLUMNS))})",
            [
                session["id"],
                session.get("title"),
                session.get("source"),
                session.get("model"),
                None,
                None,
                None,
                0,
                0,
                0,
                0,
                None,
                0,
                0,
                None,
                session.get("last_activity_description"),
                session.get("last_activity_provenance"),
            ],
        )
        con.commit()
        con.close()

    def _mirror_turn(self, session_id: str, input_text: str) -> None:
        """Insert the same user+assistant messages ``_record_turn`` appends
        in-memory into ``state.db`` too -- see the class docstring and
        ``_mirror_new_session``'s note on the no-``state.db``-yet case."""
        if not self._state_db_path().exists():
            return
        final_content = next(
            (
                data.get("content", "")
                for name, data in self.stream_events
                if name == "assistant.completed"
            ),
            "",
        )
        con = sqlite3.connect(self._state_db_path())
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0] + 1
        con.executemany(
            f"INSERT INTO messages ({','.join(_MESSAGE_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_MESSAGE_COLUMNS))})",
            [
                (
                    next_id,
                    session_id,
                    "user",
                    input_text,
                    None,
                    None,
                    None,
                    1786620020.0,
                    None,
                    None,
                ),
                (
                    next_id + 1,
                    session_id,
                    "assistant",
                    final_content,
                    None,
                    None,
                    None,
                    1786620021.0,
                    None,
                    None,
                ),
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

    def _sse(self, events: list[tuple[str, dict]]) -> httpx.Response:
        body = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
        return httpx.Response(
            200,
            content=body.encode(),
            headers={"Content-Type": "text/event-stream"},
        )

    def _sse_stream(self, events: list[tuple[str, dict]], delay: float) -> httpx.Response:
        """A streaming SSE response that dribbles events out with ``delay``
        seconds between frames, so tests can interact mid-stream."""

        return httpx.Response(
            200,
            stream=_AsyncSSEStream(events, delay),
            headers={"Content-Type": "text/event-stream"},
        )


@pytest.fixture
def hermes(hermes_home: Path) -> FakeHermes:
    return FakeHermes(hermes_home)


# -- fake daemon on-disk state (state.db / cron/jobs.json) -------------------
#
# `hermes_home` is a fresh temp directory per test; a local-mode
# `HermesExecutor` pointed at it (see `make_executor` below) reads it through
# the *real* `read_sqlite`/`read_json` -- there is nothing to fake for reads.
# `FakeHermesCli.run_fn` fakes only the CLI-subprocess *mutation* boundary,
# and each mutation writes through to the same files so a follow-up read
# sees the effect, the same way the real `hermes` CLI mutating `state.db`/
# `cron/jobs.json` in place would.


def _split_argv(argv: list[str]) -> tuple[str, list[str]]:
    """Undo `HermesExecutor.argv`: (profile, real command args), dropping
    the binary name and an optional `-p <profile>` prefix."""
    rest = argv[1:]
    profile = ""
    if rest[:1] == ["-p"]:
        profile, rest = rest[1], rest[2:]
    return profile, rest


def _parse_flags(args: list[str]) -> dict[str, list[str]]:
    """`["--name", "X", "--skill", "a", "--skill", "b", "--clear-skills"]` ->
    `{"--name": ["X"], "--skill": ["a", "b"], "--clear-skills": []}` --
    mirrors argparse's own repeatable-flag/boolean-flag handling closely
    enough for the small `hermes cron create`/`edit` flag sets this fakes."""
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(args):
        if not args[i].startswith("--"):
            i += 1
            continue
        key = args[i]
        values = flags.setdefault(key, [])
        if i + 1 < len(args) and not args[i + 1].startswith("--"):
            values.append(args[i + 1])
            i += 2
        else:
            i += 1
    return flags


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
    finish_reason TEXT, reasoning TEXT
);
"""

_SESSION_COLUMNS = [
    "id",
    "title",
    "source",
    "model",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "input_tokens",
    "output_tokens",
    "estimated_cost_usd",
    "pinned",
    "archived",
    "last_activity_at",
    "last_activity_description",
    "last_activity_provenance",
]
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

_EXECUTIONS_SCHEMA = """
CREATE TABLE executions (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    source TEXT NOT NULL,
    process_id TEXT NOT NULL,
    pid INTEGER NOT NULL,
    process_started_at INTEGER,
    status TEXT NOT NULL CHECK(status IN
      ('claimed','running','completed','failed','unknown')),
    claimed_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
)
"""
_EXECUTION_COLUMNS = [
    "id",
    "job_id",
    "source",
    "process_id",
    "pid",
    "process_started_at",
    "status",
    "claimed_at",
    "started_at",
    "finished_at",
    "error",
]


class FakeHermesCli:
    """Owns the fake daemon's `state.db` and `cron/jobs.json` under
    `hermes_home`, and fakes the `hermes` CLI's *mutation* verbs
    (`run_fn`) against them. Reads never go through this class at all --
    they hit the files directly via a real `HermesExecutor.read_sqlite`/
    `read_json`.
    """

    def __init__(self, hermes_home: Path) -> None:
        self.hermes_home = hermes_home
        self.jobs_actions: list[tuple[str, ...]] = []
        self.reset()

    def reset(self) -> None:
        self.jobs_actions = []
        self.kanban_actions: list[tuple[str, ...]] = []
        self.kanban_specify_fail = False
        self._init_state_db()
        self._init_jobs_json()
        self._init_executions_db()
        (self.hermes_home / "profiles" / "ha").mkdir(parents=True, exist_ok=True)

    # -- state.db ------------------------------------------------------------

    def _state_db_path(self) -> Path:
        return self.hermes_home / "state.db"

    def _init_state_db(self) -> None:
        path = self._state_db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        con = sqlite3.connect(path)
        con.executescript(_SESSIONS_SCHEMA)
        con.executemany(
            f"INSERT INTO sessions ({','.join(_SESSION_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_SESSION_COLUMNS))})",
            [
                (
                    "sess-1",
                    "First session",
                    "webui",
                    "deepseek-v4-flash",
                    None,
                    None,
                    None,
                    3,
                    1,
                    100,
                    50,
                    0.001,
                    0,
                    0,
                    1786620319.0,
                    "sequential tool running (30s): terminal",
                    None,
                ),
                (
                    "sess-2",
                    "Cron run",
                    "cron",
                    "deepseek-v4-flash",
                    None,
                    None,
                    None,
                    0,
                    0,
                    0,
                    0,
                    None,
                    0,
                    0,
                    1786610000.0,
                    None,
                    None,
                ),
            ],
        )
        con.executemany(
            f"INSERT INTO messages ({','.join(_MESSAGE_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_MESSAGE_COLUMNS))})",
            [
                (
                    1,
                    "sess-1",
                    "user",
                    "How do I access your API?",
                    None,
                    None,
                    None,
                    1786620000.0,
                    None,
                    None,
                ),
                (
                    2,
                    "sess-1",
                    "assistant",
                    "Here is the answer.",
                    None,
                    None,
                    None,
                    1786620010.0,
                    None,
                    None,
                ),
                (
                    3,
                    "sess-1",
                    "assistant",
                    "Used a tool to check.",
                    None,
                    json.dumps(
                        [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
                            }
                        ]
                    ),
                    None,
                    1786620015.0,
                    None,
                    "I needed to list files.",
                ),
            ],
        )
        con.commit()
        con.close()

    def insert_session(self, row: dict) -> None:
        """Add one more session row directly -- for tests exercising the
        list page's pagination against a bigger dataset than the two
        default fixture sessions."""
        con = sqlite3.connect(self._state_db_path())
        values = [row.get(col) for col in _SESSION_COLUMNS]
        con.execute(
            f"INSERT INTO sessions ({','.join(_SESSION_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_SESSION_COLUMNS))})",
            values,
        )
        con.commit()
        con.close()

    def dump_sessions(self) -> list[dict]:
        con = sqlite3.connect(self._state_db_path())
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("SELECT * FROM sessions ORDER BY rowid").fetchall()]
        con.close()
        return rows

    def _sessions_rename(self, session_id: str, title: str) -> None:
        con = sqlite3.connect(self._state_db_path())
        con.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        con.commit()
        con.close()

    def _sessions_delete(self, session_id: str) -> None:
        con = sqlite3.connect(self._state_db_path())
        con.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        con.commit()
        con.close()

    # -- cron/jobs.json --------------------------------------------------------

    def _jobs_path(self) -> Path:
        path = self.hermes_home / "cron" / "jobs.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _init_jobs_json(self) -> None:
        self._save_jobs(
            [
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
        )

    def _load_jobs(self) -> list[dict]:
        path = self._jobs_path()
        if not path.exists():
            return []
        return json.loads(path.read_text())["jobs"]

    def _save_jobs(self, jobs: list[dict]) -> None:
        self._jobs_path().write_text(json.dumps({"jobs": jobs, "updated_at": None}))

    def insert_job(self, job: dict) -> None:
        """Add one more cron job directly -- for tests exercising the job
        list page's pagination against a bigger set than the one default
        fixture job."""
        jobs = self._load_jobs()
        jobs.append(job)
        self._save_jobs(jobs)

    def _executions_db_path(self) -> Path:
        path = self.hermes_home / "cron" / "executions.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _init_executions_db(self) -> None:
        path = self._executions_db_path()
        if path.exists():
            path.unlink()
        con = sqlite3.connect(path)
        con.executescript(_EXECUTIONS_SCHEMA)
        con.executemany(
            f"INSERT INTO executions ({','.join(_EXECUTION_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_EXECUTION_COLUMNS))})",
            [
                (
                    "run-1",
                    "aabbccddeeff",
                    "builtin",
                    "proc-1",
                    101,
                    1786791100,
                    "completed",
                    "2026-08-15T11:51:48.259151+00:00",
                    "2026-08-15T11:51:49+00:00",
                    "2026-08-15T11:51:51+00:00",
                    "",
                ),
                (
                    "run-2",
                    "aabbccddeeff",
                    "builtin",
                    "proc-2",
                    102,
                    1786791000,
                    "completed",
                    "2026-08-15T10:51:48+00:00",
                    "2026-08-15T10:51:49+00:00",
                    "2026-08-15T10:52:01+00:00",
                    None,
                ),
                (
                    "run-3",
                    "aabbccddeeff",
                    "builtin",
                    "proc-3",
                    103,
                    1786790900,
                    "failed",
                    "2026-08-15T09:51:48+00:00",
                    "2026-08-15T09:51:49+00:00",
                    "2026-08-15T09:51:55+00:00",
                    "report failed",
                ),
                (
                    "run-other",
                    "other-job",
                    "direct",
                    "proc-4",
                    104,
                    1786790800,
                    "completed",
                    "2026-08-15T08:51:48+00:00",
                    "2026-08-15T08:51:49+00:00",
                    "2026-08-15T08:51:50+00:00",
                    None,
                ),
            ],
        )
        con.commit()
        con.close()

    def insert_run(self, row: dict) -> None:
        """Add an execution row directly for cron run tests."""
        con = sqlite3.connect(self._executions_db_path())
        values = [row.get(col) for col in _EXECUTION_COLUMNS]
        con.execute(
            f"INSERT INTO executions ({','.join(_EXECUTION_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_EXECUTION_COLUMNS))})",
            values,
        )
        con.commit()
        con.close()

    # -- run_fn ----------------------------------------------------------------

    async def run_fn(self, argv: list[str]) -> CompletedResult:
        _profile, args = _split_argv(argv)
        if args[:2] == ["sessions", "rename"]:
            self._sessions_rename(args[2], args[3])
            return CompletedResult(0, "", "")
        if args[:2] == ["sessions", "delete"]:
            self._sessions_delete(args[2])
            return CompletedResult(0, "", "")
        if args[:1] == ["cron"]:
            self.jobs_actions.append(tuple(args))
            self._apply_cron_action(args)
            return CompletedResult(0, "", "")
        if args[:2] == ["kanban", "specify"]:
            # Mirror of `hermes kanban specify <id> --json` (the auto-specify
            # path in plugins/kanban/ui.py): writes the fleshed-out body +
            # triage -> todo promotion into the same kanban.db the board
            # reads back, and returns the CLI's --json outcome line.
            self.kanban_actions.append(tuple(args))
            return CompletedResult(0, self._apply_kanban_specify(args), "")
        return CompletedResult(1, "", f"unhandled: {args}")

    def _apply_kanban_specify(self, args: list[str]) -> str:
        task_id = args[2]
        ok = not self.kanban_specify_fail
        if ok:
            db = self.hermes_home / "kanban.db"
            if db.exists():
                con = sqlite3.connect(db)
                con.execute(
                    "UPDATE tasks SET body = ?, status = 'todo' WHERE id = ?",
                    ("**Goal**\nFake auto-spec body.", task_id),
                )
                con.commit()
                con.close()
        return json.dumps(
            {
                "task_id": task_id,
                "ok": ok,
                "reason": "specified" if ok else "LLM error: simulated failure",
                "new_title": None,
            }
        )

    def _apply_cron_action(self, args: list[str]) -> None:
        action = args[1]
        rest = args[2:]
        jobs = self._load_jobs()
        if action == "create":
            jobs.append(self._new_job_from_create_args(rest))
            self._save_jobs(jobs)
            return
        job_id = rest[0]
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if job is None:
            return
        if action == "pause":
            job["enabled"], job["state"] = False, "paused"
        elif action == "resume":
            job["enabled"], job["state"] = True, "scheduled"
        elif action == "remove":
            jobs.remove(job)
        elif action == "run":
            job["last_status"] = "success"
        elif action == "edit":
            self._apply_edit_flags(job, rest[1:])
        self._save_jobs(jobs)

    def _new_job_from_create_args(self, rest: list[str]) -> dict:
        schedule = rest[0]
        idx = 1
        prompt = ""
        if idx < len(rest) and not rest[idx].startswith("--"):
            prompt = rest[idx]
            idx += 1
        flags = _parse_flags(rest[idx:])
        repeat = int(flags["--repeat"][0]) if flags.get("--repeat") else None
        return {
            "id": "112233445566",
            "name": (flags.get("--name") or [""])[0],
            "prompt": prompt,
            "schedule_display": schedule,
            "deliver": (flags.get("--deliver") or [None])[0],
            "skills": flags.get("--skill", []),
            "repeat": {"times": repeat, "completed": 0},
            "enabled": True,
            "state": "scheduled",
            "created_at": "2026-08-13T00:00:00+00:00",
            "next_run_at": "2026-08-14T00:00:00+00:00",
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
        }

    def _apply_edit_flags(self, job: dict, flag_args: list[str]) -> None:
        flags = _parse_flags(flag_args)
        if "--name" in flags:
            job["name"] = flags["--name"][0]
        if "--schedule" in flags:
            job["schedule_display"] = flags["--schedule"][0]
        if "--prompt" in flags:
            job["prompt"] = flags["--prompt"][0]
        if "--deliver" in flags:
            job["deliver"] = flags["--deliver"][0]
        if "--repeat" in flags:
            job["repeat"] = {
                "times": int(flags["--repeat"][0]),
                "completed": job.get("repeat", {}).get("completed", 0),
            }
        if "--skill" in flags:
            job["skills"] = flags["--skill"]
        if "--clear-skills" in flags:
            job["skills"] = []


@pytest.fixture
def hermes_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("hermes-home")


@pytest.fixture
def fake_hermes_cli(hermes_home: Path) -> FakeHermesCli:
    return FakeHermesCli(hermes_home)


@pytest.fixture
def make_executor(
    fake_hermes_cli: FakeHermesCli, hermes_home: Path
) -> Callable[..., HermesExecutor]:
    def _make(**kwargs: Any) -> HermesExecutor:
        kwargs.setdefault("hermes_bin", "hermes")
        kwargs.setdefault("hermes_home", str(hermes_home))
        return HermesExecutor(run_fn=fake_hermes_cli.run_fn, **kwargs)

    return _make


@pytest.fixture
def executor(make_executor: Callable[..., HermesExecutor]) -> HermesExecutor:
    return make_executor()


@pytest.fixture
def make_context(
    hermes: FakeHermes, executor: HermesExecutor, tmp_path_factory: pytest.TempPathFactory
) -> Callable[..., PluginContext]:
    """Factory for a :class:`PluginContext` wired to fake backends.

    A factory fixture (rather than a fixed ``context`` value) so tests that
    need a specific setting -- ``test_dark.py`` wants ``ui_dark=True``, say --
    can ask for it directly instead of reaching for env vars or markers.
    """

    def _make(**settings_kwargs: Any) -> PluginContext:
        settings = Settings(
            gateway_url="http://hermes.test",
            api_token="test-token",
            default_model="deepseek-v4-flash",
            # `web.build()` unconditionally opens/creates `data_dir/users.db`
            # (see `AppState.user_store`) -- without this, every test run
            # would read/write the real default `~/.local/share/hermes-nicegui`,
            # leaking state (e.g. "has an admin account already") across runs.
            # A fixture-factory temp dir, not the test's own `tmp_path`: some
            # tests (e.g. files-plugin ones) point `files_root` at `tmp_path`
            # itself, and a `data_dir` nested inside it would show up as a
            # stray extra entry in that plugin's directory listing.
            data_dir=str(tmp_path_factory.mktemp("hermes-nicegui-data")),
            **settings_kwargs,
        )
        client = HermesClient(
            settings.gateway_url,
            settings.api_token,
            transport=httpx.MockTransport(hermes.handle),
        )
        return PluginContext(client=client, settings=settings, logger=logger, executor=executor)

    return _make


@pytest.fixture
def context(make_context: Callable[..., PluginContext]) -> PluginContext:
    """The default fake context, for tests that don't need custom settings."""
    return make_context()


# -- fake kanban dashboard (writes) + kanban.db (reads) -----------------------
#
# Kanban board *reads* go through `web.current_store().kanban` -- direct
# SQLite against `hermes_home/kanban.db` (a real file, like sessions/cron's
# state.db/jobs.json above). Kanban *writes* still go through `KanbanClient`
# (dashboard cookie-auth REST -- kanban's CLI has no generic field-setter,
# see `plugins/kanban/ui.py`'s module docstring), faked here the same way as
# before. What's new: each write handler mirrors its change into the same
# `kanban.db` file the read path sees, since a real dashboard write and a
# real direct SQLite read are both ultimately looking at the *same* on-disk
# database in production -- a UI test that creates a task via the dialog and
# then expects the (re-read) board to show it depends on that being true here
# too.

_KANBAN_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT,
    priority INTEGER, tenant TEXT, created_at INTEGER, started_at INTEGER,
    completed_at INTEGER, consecutive_failures INTEGER DEFAULT 0,
    last_failure_error TEXT, current_run_id TEXT, session_id TEXT
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT, created_at INTEGER
);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, started_at INTEGER,
    ended_at INTEGER, outcome TEXT, summary TEXT
);
"""
_TASK_COLUMNS = [
    "id",
    "title",
    "body",
    "assignee",
    "status",
    "priority",
    "tenant",
    "created_at",
    "started_at",
    "completed_at",
    "consecutive_failures",
    "last_failure_error",
    "current_run_id",
    "session_id",
]


class FakeKanban:
    """In-memory stand-in for the Hermes dashboard's kanban plugin API
    (writes), backed by a real `kanban.db` (reads) -- see the module
    comment above.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.tasks: list[dict] = []
        self.comments: dict[str, list[dict]] = {}
        self.dispatch_calls = 0
        self.reset()

    def reset(self) -> None:
        self.tasks = [
            {
                "id": "t_1",
                "title": "Fix flaky test",
                "body": "test_foo is flaky",
                "assignee": "default",
                "status": "ready",
                "priority": 2,
                "created_at": 1786620000,
                "started_at": None,
                "completed_at": None,
                "consecutive_failures": 0,
                "last_failure_error": None,
                "current_run_id": "run_1",
                "session_id": "sess-99",
                "latest_summary": None,
                "comment_count": 1,
            }
        ]
        self.comments = {
            "t_1": [
                {"id": 1, "body": "Looking into it", "author": "alex", "created_at": 1786620100},
            ]
        }
        self.dispatch_calls = 0
        self._init_db()

    def _init_db(self) -> None:
        if self.db_path.exists():
            self.db_path.unlink()
        con = sqlite3.connect(self.db_path)
        con.executescript(_KANBAN_SCHEMA)
        for task in self.tasks:
            self._sql_insert_task(con, task)
        for task_id, comments in self.comments.items():
            for comment in comments:
                con.execute(
                    "INSERT INTO task_comments (id, task_id, author, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        comment["id"],
                        task_id,
                        comment["author"],
                        comment["body"],
                        comment["created_at"],
                    ),
                )
        con.execute(
            "INSERT INTO task_runs (task_id, status, started_at, outcome, summary) "
            "VALUES ('t_1', 'completed', 1786620050, 'completed', NULL)"
        )
        con.commit()
        con.close()

    def _sql_insert_task(self, con: sqlite3.Connection, task: dict) -> None:
        values = [task.get(col) for col in _TASK_COLUMNS]
        con.execute(
            f"INSERT OR REPLACE INTO tasks ({','.join(_TASK_COLUMNS)}) "
            f"VALUES ({','.join('?' * len(_TASK_COLUMNS))})",
            values,
        )

    def insert_task(self, task: dict) -> None:
        """Add one more task directly to `kanban.db` -- for tests exercising
        the board page's pagination against a bigger set than the one
        default fixture task."""
        con = sqlite3.connect(self.db_path)
        self._sql_insert_task(con, task)
        con.commit()
        con.close()

    def _sql_update_task(self, task_id: str, fields: dict) -> None:
        cols = [c for c in fields if c in _TASK_COLUMNS]
        if not cols:
            return
        con = sqlite3.connect(self.db_path)
        con.execute(
            f"UPDATE tasks SET {','.join(f'{c} = ?' for c in cols)} WHERE id = ?",
            [fields[c] for c in cols] + [task_id],
        )
        con.commit()
        con.close()

    def _sql_delete_task(self, task_id: str) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        con.commit()
        con.close()

    def _sql_insert_comment(self, task_id: str, comment: dict) -> None:
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT INTO task_comments (id, task_id, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (comment["id"], task_id, comment["author"], comment["body"], comment["created_at"]),
        )
        con.commit()
        con.close()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        path = request.url.path
        if method == "POST" and path == "/auth/password-login":
            return self._json({"ok": True, "next": "/"})
        if method == "GET" and path == "/api/plugins/kanban/board":
            columns: dict[str, list[dict]] = {
                name: []
                for name in [
                    "triage",
                    "todo",
                    "scheduled",
                    "ready",
                    "running",
                    "blocked",
                    "review",
                    "done",
                ]
            }
            for task in self.tasks:
                columns.setdefault(task["status"], []).append(task)
            return self._json(
                {
                    "columns": [{"name": name, "tasks": ts} for name, ts in columns.items()],
                    "assignees": ["default"],
                    "tenants": [],
                    "latest_event_id": 0,
                    "now": 1786668908,
                }
            )
        if method == "POST" and path == "/api/plugins/kanban/tasks":
            body = json.loads(request.content) if request.content else {}
            new_id = f"t_{len(self.tasks) + 1}"
            task = {
                "id": new_id,
                "title": body.get("title", ""),
                "body": body.get("body", ""),
                "assignee": body.get("assignee") or "default",
                "status": "ready",
                "priority": body.get("priority", 2),
                "created_at": 1786668908,
                "started_at": None,
                "completed_at": None,
                "consecutive_failures": 0,
                "last_failure_error": None,
                "current_run_id": None,
                "latest_summary": None,
                "comment_count": 0,
            }
            self.tasks.append(task)
            self.comments[new_id] = []
            con = sqlite3.connect(self.db_path)
            self._sql_insert_task(con, task)
            con.commit()
            con.close()
            return self._json({"task": task})
        if method == "POST" and path == "/api/plugins/kanban/dispatch":
            self.dispatch_calls += 1
            return self._json({"ok": True})
        if path.startswith("/api/plugins/kanban/tasks/"):
            parts = path.split("/")
            task_id = parts[5]
            action = parts[6] if len(parts) > 6 else None
            task = next((t for t in self.tasks if t["id"] == task_id), None)
            if method == "GET" and action is None:
                if not task:
                    return self._json({"error": "not found"}, status=404)
                return self._json(
                    {
                        "task": task,
                        "comments": self.comments.get(task_id, []),
                        "events": [],
                        "attachments": [],
                        "links": {"parents": [], "children": []},
                        "child_results": [],
                        "runs": [
                            {
                                "id": "run_1",
                                "status": "completed",
                                "started_at": 1786620050,
                            }
                        ],
                    }
                )
            if method == "PATCH" and action is None:
                if not task:
                    return self._json({"error": "not found"}, status=404)
                body = json.loads(request.content) if request.content else {}
                task.update(body)
                self._sql_update_task(task_id, body)
                return self._json({"task": task})
            if method == "DELETE" and action is None:
                if not task:
                    return self._json({"error": "not found"}, status=404)
                self.tasks.remove(task)
                self._sql_delete_task(task_id)
                return self._json({"deleted": True, "task_id": task_id})
            if method == "POST" and action == "comments":
                body = json.loads(request.content) if request.content else {}
                comment = {
                    "id": len(self.comments.get(task_id, [])) + 1,
                    "body": body.get("body", ""),
                    "author": "test",
                    "created_at": 1786668908,
                }
                self.comments.setdefault(task_id, []).append(comment)
                self._sql_insert_comment(task_id, comment)
                if task:
                    task["comment_count"] = task.get("comment_count", 0) + 1
                return self._json({"comment": comment})
        return self._json({"error": {"message": f"unhandled {method} {path}"}}, status=404)

    def _json(self, payload: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=payload, headers={"Content-Type": "application/json"})


@pytest.fixture
def fake_kanban(hermes_home: Path) -> FakeKanban:
    return FakeKanban(hermes_home / "kanban.db")


@pytest.fixture
def kanban_client(fake_kanban: FakeKanban) -> KanbanClient:
    return KanbanClient(
        "http://kanban.test",
        "admin",
        "test-password",
        transport=httpx.MockTransport(fake_kanban.handle),
    )
