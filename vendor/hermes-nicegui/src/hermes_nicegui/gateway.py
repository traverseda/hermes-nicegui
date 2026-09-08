"""Async client for the Hermes Agent API server.

Thin wrapper around :class:`httpx.AsyncClient`. All methods are async; SSE
streams are consumed incrementally and never block the event loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from loguru import logger


class HermesError(Exception):
    """Raised when the gateway returns a non-2xx response."""


#: Injectable default transport used when tests need to fake the gateway
#: without touching network. Tests set this to ``httpx.MockTransport``.
DEFAULT_TRANSPORT: httpx.AsyncBaseTransport | None = None


@dataclass
class StreamEvent:
    """A single parsed SSE event from a Hermes stream endpoint."""

    event: str
    data: dict[str, Any]


@dataclass
class Session:
    """A Hermes session (from ``GET /api/sessions``)."""

    id: str
    title: str | None = None
    source: str | None = None
    model: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    end_reason: str | None = None
    message_count: int = 0
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float | None = None
    pinned: bool = False
    archived: bool = False
    last_active: float | None = None
    preview: str | None = None
    last_activity_description: str | None = None
    last_activity_provenance: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Session:
        return cls(
            id=data["id"],
            title=data.get("title"),
            source=data.get("source"),
            model=data.get("model"),
            started_at=data.get("started_at"),
            ended_at=data.get("ended_at"),
            end_reason=data.get("end_reason"),
            message_count=data.get("message_count", 0),
            tool_call_count=data.get("tool_call_count", 0),
            input_tokens=data.get("input_tokens", 0),
            output_tokens=data.get("output_tokens", 0),
            estimated_cost_usd=data.get("estimated_cost_usd"),
            pinned=data.get("pinned", False),
            archived=data.get("archived", False),
            last_active=data.get("last_active"),
            preview=data.get("preview"),
            last_activity_description=data.get("last_activity_description"),
            last_activity_provenance=data.get("last_activity_provenance"),
        )


@dataclass
class Job:
    """A cron job (from ``GET /api/jobs``), as returned by the gateway's
    ``api_server`` platform adapter (``gateway/platforms/api_server.py`` in
    the Hermes Agent repo). ``raw`` keeps the full record so a UI can offer
    lossless raw-YAML editing on top of the fields modelled here.
    """

    id: str
    name: str
    schedule_display: str
    prompt: str | None = None
    script: str | None = None
    no_agent: bool = False
    deliver: str | None = None
    skills: list[str] = field(default_factory=list)
    repeat_times: int | None = None
    repeat_completed: int = 0
    enabled: bool = True
    state: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Job:
        repeat = data.get("repeat") or {}
        return cls(
            id=data["id"],
            name=data.get("name") or "",
            schedule_display=data.get("schedule_display") or "",
            prompt=data.get("prompt"),
            script=data.get("script"),
            no_agent=data.get("no_agent", False),
            deliver=data.get("deliver"),
            skills=data.get("skills") or [],
            repeat_times=repeat.get("times"),
            repeat_completed=repeat.get("completed", 0),
            enabled=data.get("enabled", True),
            state=data.get("state"),
            next_run_at=data.get("next_run_at"),
            last_run_at=data.get("last_run_at"),
            last_status=data.get("last_status"),
            last_error=data.get("last_error"),
            created_at=data.get("created_at"),
            raw=data,
        )


@dataclass
class CronRun:
    """A single cron job execution attempt from ``cron/executions.db``."""

    id: str
    job_id: str
    source: str
    process_id: str
    pid: int
    process_started_at: int | None = None
    status: str = "unknown"
    claimed_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CronRun:
        """Build a run from a daemon execution row."""
        return cls(
            id=data["id"],
            job_id=data["job_id"],
            source=data.get("source") or "",
            process_id=data.get("process_id") or "",
            pid=data.get("pid", 0),
            process_started_at=data.get("process_started_at"),
            status=data.get("status") or "unknown",
            claimed_at=data.get("claimed_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            error=data.get("error"),
        )


@dataclass
class Task:
    """A kanban task. ``raw`` keeps the full record for anything not
    modelled here. Shared between the dashboard REST client
    (``HermesExecutor``'s dashboard-REST methods, still used for kanban
    *writes*) and direct SQLite reads (``hermes_nicegui.store.KanbanStore``)
    -- lives in core ``gateway.py`` (alongside ``Job``, cron's own
    dataclass) rather than under ``plugins/kanban/`` so both can use it
    without a plugin-to-plugin or core-to-plugin import.
    """

    id: str
    title: str
    status: str
    body: str | None = None
    assignee: str | None = None
    priority: int | None = None
    tenant: str | None = None
    created_at: float | None = None
    started_at: float | None = None
    completed_at: float | None = None
    consecutive_failures: int = 0
    last_failure_error: str | None = None
    current_run_id: str | None = None
    session_id: str | None = None
    latest_summary: str | None = None
    comment_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Task:
        return cls(
            id=data["id"],
            title=data.get("title") or "",
            status=data.get("status") or "",
            body=data.get("body"),
            assignee=data.get("assignee"),
            priority=data.get("priority"),
            tenant=data.get("tenant"),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            consecutive_failures=data.get("consecutive_failures", 0),
            last_failure_error=data.get("last_failure_error"),
            current_run_id=data.get("current_run_id"),
            session_id=data.get("session_id"),
            latest_summary=data.get("latest_summary"),
            comment_count=data.get("comment_count", 0),
            raw=data,
        )


@dataclass
class BoardColumn:
    name: str
    tasks: list[Task] = field(default_factory=list)


@dataclass
class Board:
    columns: list[BoardColumn] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    tenants: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Board:
        return cls(
            columns=[
                BoardColumn(
                    name=column["name"],
                    tasks=[Task.from_json(t) for t in column.get("tasks", [])],
                )
                for column in data.get("columns", [])
            ],
            assignees=data.get("assignees") or [],
            tenants=data.get("tenants") or [],
        )


@dataclass
class Comment:
    id: Any
    body: str
    author: str | None = None
    created_at: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Comment:
        return cls(
            id=data.get("id"),
            body=data.get("body") or "",
            author=data.get("author") or data.get("created_by"),
            created_at=data.get("created_at"),
            raw=data,
        )


@dataclass
class TaskDetail:
    task: Task
    comments: list[Comment] = field(default_factory=list)
    # Left as raw dicts (not a modelled dataclass) -- the run/event schema
    # varies by outcome (running/done/blocked/crashed/...), so this stays
    # defensive rather than guessing a fixed field set.
    runs: list[dict[str, Any]] = field(default_factory=list)
    # The dispatcher's kanban-tagged worker session that actually did (or is
    # doing) the work for this task -- distinct from `task.session_id`, which
    # is the session that *created* the task (and is None for tasks created
    # from the CLI or the dashboard). Resolved by `KanbanStore` from the
    # assignee's `state.db`; None while the task has never been claimed (no
    # worker session exists yet).
    worker_session_id: str | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TaskDetail:
        return cls(
            task=Task.from_json(data["task"]),
            comments=[Comment.from_json(c) for c in data.get("comments", [])],
            runs=data.get("runs") or [],
        )


@dataclass
class Message:
    """A message in a session transcript."""

    id: int
    session_id: str
    role: str
    content: str | None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    timestamp: float | None = None
    finish_reason: str | None = None
    reasoning: str | None = None
    active: bool = True
    compacted: bool = False

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Message:
        return cls(
            id=data["id"],
            session_id=data["session_id"],
            role=data["role"],
            content=data.get("content"),
            tool_calls=data.get("tool_calls"),
            tool_call_id=data.get("tool_call_id"),
            tool_name=data.get("tool_name"),
            timestamp=data.get("timestamp"),
            finish_reason=data.get("finish_reason"),
            reasoning=data.get("reasoning"),
            active=bool(data.get("active", 1)),
            compacted=bool(data.get("compacted", 0)),
        )


class StreamEventSink(Protocol):
    """Callback interface for consuming :class:`StreamEvent` objects."""

    async def __call__(self, event: StreamEvent) -> None: ...


@dataclass
class _SSEBuffer:
    """Accumulates SSE ``event:`` / ``data:`` lines into events."""

    event_name: str = ""
    data_lines: list[str] = field(default_factory=list)

    def push(self, line: str) -> StreamEvent | None:
        """Feed one raw SSE line; returns a complete event when a blank line arrives."""
        line = line.rstrip("\r")
        if line == "":
            return self.flush()
        if line.startswith(":"):
            return None  # comment
        if line.startswith("event:"):
            self.event_name = line[len("event:") :].strip()
        elif line.startswith("data:"):
            self.data_lines.append(line[len("data:") :].lstrip())
        return None

    def flush(self) -> StreamEvent | None:
        if not self.event_name:
            self.reset()
            return None
        payload = "\n".join(self.data_lines)
        data: dict[str, Any] = {}
        if payload:
            import json

            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                data = {"raw": payload}
        evt = StreamEvent(event=self.event_name, data=data)
        self.reset()
        return evt

    def reset(self) -> None:
        self.event_name = ""
        self.data_lines = []


class HermesClient:
    """Async client for the Hermes API server.

    ``transport`` is injectable so tests can swap in ``httpx.MockTransport``.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 120.0,
        default_model: str = "",
        default_provider: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.default_model = default_model
        self.default_provider = default_provider
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            transport=transport or DEFAULT_TRANSPORT,
            timeout=httpx.Timeout(timeout, connect=5.0),
        )
        logger.debug("HermesClient -> {}", self.base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict[str, Any]:
        resp = await self._client.request(method, path, json=json, params=params)
        if resp.status_code >= 400:
            body = resp.text[:500]
            raise HermesError(f"{method} {path} -> {resp.status_code}: {body}")
        return resp.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/health")

    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/capabilities")

    async def list_models(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/v1/models")
        return data.get("data", [])

    async def model_options(self, refresh: bool = False) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/api/model/options", params={"refresh": "1"} if refresh else None
        )
        return data.get("providers", [])

    async def list_skills(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/v1/skills")
        return data.get("data", [])

    async def list_toolsets(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/v1/toolsets")
        return data.get("data", [])

    # -- sessions ---------------------------------------------------------

    async def list_sessions(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        source: str | None = None,
        include_children: bool = False,
    ) -> tuple[list[Session], bool]:
        """Return ``(sessions, has_more)`` for the given page."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "include_children": include_children,
        }
        if source:
            params["source"] = source
        data = await self._request("GET", "/api/sessions", params=params)
        sessions = [Session.from_json(s) for s in data.get("data", [])]
        return sessions, data.get("has_more", False)

    async def create_session(self, title: str | None = None) -> Session:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        # Always send an explicit model. The gateway otherwise defaults the
        # session to the placeholder "hermes-agent" model name, which its
        # router rejects with HTTP 400 on the first chat turn. The UI's
        # default model (HERMES_DEFAULT_MODEL) is a routable provider model.
        if self.default_model:
            body["model"] = self.default_model
        if self.default_provider:
            body["provider"] = self.default_provider
        # Tag sessions created from the web UI so the gateway accepts and
        # preserves the webui source.
        body["source"] = "webui"
        data = await self._request("POST", "/api/sessions", json=body)
        inner = data.get("session") or data
        return Session.from_json(inner)

    async def get_session(self, session_id: str) -> Session:
        data = await self._request("GET", f"/api/sessions/{session_id}")
        inner = data.get("session") or data
        return Session.from_json(inner)

    async def update_session(
        self, session_id: str, *, title: str | None = None, end_reason: str | None = None
    ) -> Session:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if end_reason is not None:
            body["end_reason"] = end_reason
        data = await self._request("PATCH", f"/api/sessions/{session_id}", json=body)
        inner = data.get("session") or data
        return Session.from_json(inner)

    async def delete_session(self, session_id: str) -> bool:
        await self._request("DELETE", f"/api/sessions/{session_id}")
        return True

    async def fork_session(self, session_id: str, *, title: str | None = None) -> Session:
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        data = await self._request("POST", f"/api/sessions/{session_id}/fork", json=body)
        inner = data.get("session") or data
        return Session.from_json(inner)

    async def set_session_model(
        self, session_id: str, *, model: str, provider: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"model": model}
        if provider:
            body["provider"] = provider
        return await self._request("POST", f"/api/sessions/{session_id}/model", json=body)

    async def session_messages(
        self, session_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[Message]:
        data = await self._request(
            "GET",
            f"/api/sessions/{session_id}/messages",
            params={"limit": limit, "offset": offset},
        )
        return [Message.from_json(m) for m in data.get("data", [])]

    # -- cron jobs ----------------------------------------------------------

    async def list_jobs(self, *, include_disabled: bool = True) -> list[Job]:
        data = await self._request(
            "GET", "/api/jobs", params={"include_disabled": include_disabled}
        )
        return [Job.from_json(j) for j in data.get("jobs", [])]

    async def create_job(
        self,
        *,
        name: str,
        schedule: str,
        prompt: str = "",
        deliver: str | None = None,
        skills: list[str] | None = None,
        repeat: int | None = None,
    ) -> Job:
        body: dict[str, Any] = {"name": name, "schedule": schedule, "prompt": prompt}
        if deliver:
            body["deliver"] = deliver
        if skills:
            body["skills"] = skills
        if repeat is not None:
            body["repeat"] = repeat
        data = await self._request("POST", "/api/jobs", json=body)
        return Job.from_json(data["job"])

    async def get_job(self, job_id: str) -> Job:
        data = await self._request("GET", f"/api/jobs/{job_id}")
        return Job.from_json(data["job"])

    async def update_job(self, job_id: str, fields: dict[str, Any]) -> Job:
        """PATCH a job. ``fields`` may hold any subset of the job's editable
        keys -- the gateway silently drops anything it doesn't recognize."""
        data = await self._request("PATCH", f"/api/jobs/{job_id}", json=fields)
        return Job.from_json(data["job"])

    async def delete_job(self, job_id: str) -> bool:
        await self._request("DELETE", f"/api/jobs/{job_id}")
        return True

    async def pause_job(self, job_id: str) -> Job:
        data = await self._request("POST", f"/api/jobs/{job_id}/pause")
        return Job.from_json(data["job"])

    async def resume_job(self, job_id: str) -> Job:
        data = await self._request("POST", f"/api/jobs/{job_id}/resume")
        return Job.from_json(data["job"])

    async def run_job(self, job_id: str) -> Job:
        data = await self._request("POST", f"/api/jobs/{job_id}/run")
        return Job.from_json(data["job"])

    # -- chat --------------------------------------------------------------

    async def stream_turn(
        self,
        session_id: str,
        input_text: str,
        *,
        model: str | None = None,
        provider: str | None = None,
        sink: StreamEventSink | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Run one agent turn against a session, yielding SSE events.

        Events arrive incrementally; the iterator never buffers the whole
        stream, so the event loop stays responsive.
        """
        body: dict[str, Any] = {"input": input_text}
        if model:
            body["model"] = model
        if provider:
            body["provider"] = provider

        buffer = _SSEBuffer()
        async with self._client.stream(
            "POST", f"/api/sessions/{session_id}/chat/stream", json=body
        ) as resp:
            if resp.status_code >= 400:
                text = (await resp.aread()).decode("utf-8", "replace")[:500]
                path = f"/api/sessions/{session_id}/chat/stream"
                raise HermesError(f"POST {path} -> {resp.status_code}: {text}")
            async for line in resp.aiter_lines():
                event = buffer.push(line)
                if event is not None:
                    if sink is not None:
                        await sink(event)
                    yield event

    async def chat_turn(
        self,
        session_id: str,
        input_text: str,
        *,
        model: str | None = None,
        provider: str | None = None,
    ) -> str:
        """Synchronous (non-streaming) single turn; returns final assistant text."""
        events = [
            e
            async for e in self.stream_turn(session_id, input_text, model=model, provider=provider)
        ]
        for event in reversed(events):
            if event.event == "assistant.completed":
                return event.data.get("content", "")
        return ""

    async def stop_run(self, run_id: str) -> dict[str, Any]:
        """POST /v1/runs/{run_id}/stop -- interrupt a running agent turn.

        The gateway cooperatively interrupts the live agent (and reaps any
        background processes it spawned). Every SSE event from a ``/chat/stream``
        turn carries the ``run_id`` in its payload, so a client that tracks the
        last-seen run id can always target the turn it is currently streaming.
        Unknown/already-finished runs answer 404/409 -- callers should treat
        those as already-stopped.
        """
        return await self._request("POST", f"/v1/runs/{run_id}/stop")

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        """Interrupt a running agent turn in a session from any surface.

        The gateway answers 409 ``session_not_running`` when the session has
        no live turn.

        If the daemon lacks the ``/api/sessions/{id}/stop`` endpoint (404),
        falls back to listing the session's runs via ``GET /api/sessions/{id}``
        and stopping whichever one is active via ``POST /v1/runs/{run_id}/stop``.
        """
        try:
            return await self._request("POST", f"/api/sessions/{session_id}/stop")
        except HermesError as exc:
            # The session-stop endpoint may not exist on older daemon versions;
            # fall back to the run-stop endpoint by looking up active runs.
            if "404" in str(exc):
                return await self._stop_session_via_runs(session_id)
            raise

    async def _stop_session_via_runs(self, session_id: str) -> dict[str, Any]:
        """Fall-back: find the active run for a session and stop it."""
        try:
            session = await self._request("GET", f"/api/sessions/{session_id}")
        except HermesError as exc:
            raise HermesError(f"Failed to get session: {exc}")
        # The session row may carry an ``active_run_id`` on newer daemons.
        run_id = session.get("active_run_id") if isinstance(session, dict) else None
        if not run_id:
            # Some versions expose runs under a ``runs`` key or not at all.
            # Try the broader /v1/runs list and filter by session_id.
            try:
                runs_resp = await self._request("GET", "/v1/runs")
                runs = runs_resp.get("data", []) if isinstance(runs_resp, dict) else []
                for run in runs:
                    if run.get("session_id") == session_id and run.get("status") not in (
                        "completed", "failed", "cancelled", "idle", "stopped"
                    ):
                        run_id = run.get("id")
                        break
            except Exception:
                pass
        if not run_id:
            raise HermesError(f"Session {session_id} is not running")
        return await self._request("POST", f"/v1/runs/{run_id}/stop")
