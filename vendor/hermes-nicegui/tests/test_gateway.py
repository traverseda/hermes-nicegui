"""Unit tests for the Hermes gateway client (no UI, no network)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from hermes_nicegui import gateway
from hermes_nicegui.gateway import HermesClient, HermesError, Session, StreamEvent
from tests.conftest import FakeHermes


@pytest.fixture
async def client(hermes: FakeHermes) -> AsyncIterator[HermesClient]:
    c = HermesClient(
        "http://hermes.test",
        "token-123",
        transport=httpx.MockTransport(hermes.handle),
    )
    yield c
    await c.aclose()


def test_session_from_json() -> None:
    s = Session.from_json(
        {"id": "abc", "title": "t", "source": "webui", "message_count": 4, "tool_call_count": 2}
    )
    assert s.id == "abc"
    assert s.title == "t"
    assert s.message_count == 4
    assert s.tool_call_count == 2


async def test_list_sessions(client: HermesClient) -> None:
    sessions, has_more = await client.list_sessions(limit=25)
    assert has_more is False
    assert [s.id for s in sessions] == ["sess-1", "sess-2"]


async def test_create_session(client: HermesClient) -> None:
    s = await client.create_session(title="brand new")
    assert s.id == "sess-new"
    assert s.source == "webui"


async def test_get_session(client: HermesClient) -> None:
    s = await client.get_session("sess-1")
    assert s.id == "sess-1"
    assert s.title == "First session"


async def test_session_messages(client: HermesClient) -> None:
    msgs = await client.session_messages("sess-1")
    assert len(msgs) == 3
    assert msgs[0].role == "user"
    assert msgs[1].content == "Here is the answer."
    assert msgs[2].tool_calls and msgs[2].tool_calls[0]["function"]["name"] == "terminal"


async def test_set_session_model(client: HermesClient) -> None:
    result = await client.set_session_model("sess-1", model="m", provider="p")
    assert result["model_lock"] == "accepted"


async def test_fork_session(client: HermesClient) -> None:
    s = await client.fork_session("sess-1")
    assert s.id == "sess-fork"


async def test_delete_session(client: HermesClient) -> None:
    assert await client.delete_session("sess-1") is True


async def test_stop_session(client: HermesClient, hermes: FakeHermes) -> None:
    result = await client.stop_session("sess-1")
    assert result == {"session_id": "sess-1", "status": "stopping"}
    assert hermes.stop_session_calls == ["sess-1"]


async def test_health(client: HermesClient) -> None:
    # our fake returns 404 for /health; ensure a HermesError is raised cleanly
    with pytest.raises(HermesError):
        await client.health()


async def test_stream_turn_yields_events(client: HermesClient) -> None:
    events = [e async for e in client.stream_turn("sess-1", "hi")]
    assert isinstance(events[0], StreamEvent)
    assert events[0].event == "run.started"
    deltas = [e.data["delta"] for e in events if e.event == "assistant.delta"]
    assert "".join(deltas) == "Hello world"


async def test_stream_turn_with_sink(client: HermesClient) -> None:
    seen: list[str] = []

    async def sink(event: StreamEvent) -> None:
        seen.append(event.event)

    async for _ in client.stream_turn("sess-1", "hi", sink=sink):
        pass
    assert seen == [
        "run.started",
        "message.started",
        "tool.progress",
        "assistant.delta",
        "assistant.delta",
        "assistant.completed",
        "run.completed",
        "done",
    ]


async def test_chat_turn_returns_final_text(client: HermesClient) -> None:
    text = await client.chat_turn("sess-1", "hi")
    assert text == "Hello world"


def test_sse_buffer() -> None:
    buf = gateway._SSEBuffer()
    raw = 'event: run.started\ndata: {"a": 1}\n\nevent: tool.progress\ndata: {"tool_name": "x"}\n\n'
    events = []
    for line in raw.splitlines():
        evt = buf.push(line)
        if evt is not None:
            events.append(evt)
    assert [e.event for e in events] == ["run.started", "tool.progress"]
    assert events[0].data == {"a": 1}


def test_sse_buffer_multiline_data() -> None:
    buf = gateway._SSEBuffer()
    raw = 'event: run.completed\ndata: {"a": "one"\ndata: "two"}\n\n'
    events = [buf.push(line) for line in raw.splitlines()]
    done = [e for e in events if e is not None]
    # Per the SSE spec, consecutive data: lines are joined with a newline.
    # Splitting JSON across data: lines therefore produces invalid JSON, which
    # the parser surfaces verbatim rather than crashing.
    assert done[0].data == {"raw": '{"a": "one"\n"two"}'}
