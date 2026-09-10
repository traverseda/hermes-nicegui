"""Tests for the session detail/transcript page and inline chat."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path

from nicegui import ui
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin


async def test_detail_page_loads_directly(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")


async def test_detail_page_shows_all_messages(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")
    await user.should_see("Here is the answer.")


async def test_detail_page_has_actions(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("Rename")
    await user.should_see("Delete")


async def test_send_message_streams_reply(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("What's the weather?")
    user.find(marker="chat-send").click()

    await user.should_see("What's the weather?")
    await user.should_see("Hello world")


async def test_live_reasoning_shown_in_timeline(
    user: User, context: PluginContext
) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("show reasoning")
    user.find(marker="chat-send").click()

    await user.should_see(marker="live-reasoning", retries=20)
    await user.should_see("thinking…")
    await user.should_not_see("_thinking:")


async def test_live_tool_call_renders_then_completes(
    user: User, context: PluginContext, hermes
) -> None:
    hermes.stream_events = [
        ("run.started", {"run_id": "run_tool"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        (
            "tool.started",
            {"message_id": 20, "tool_name": "terminal", "args": '{"command": "ls"}'},
        ),
        ("assistant.delta", {"message_id": 20, "delta": "Done"}),
        ("assistant.completed", {"message_id": 20, "content": "Done"}),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("run a tool")
    user.find(marker="chat-send").click()

    await user.should_see(marker="live-tool", retries=20)
    await user.should_see("terminal:")
    await user.should_see(marker="live-tool-done", retries=20)


async def test_run_completed_reconciles_authoritative_transcript(
    user: User, context: PluginContext, hermes
) -> None:
    hermes.stream_events = [
        ("run.started", {"run_id": "run_authoritative"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 20, "delta": "streamed reply"}),
        ("assistant.completed", {"message_id": 20, "content": "streamed reply"}),
        (
            "run.completed",
            {
                "completed": True,
                "usage": {},
                "messages": [
                    {
                        "id": 10,
                        "session_id": "sess-1",
                        "role": "assistant",
                        "content": "",
                        "reasoning": "I reasoned about it.",
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {
                                    "name": "terminal",
                                    "arguments": '{"command": "ls"}',
                                },
                            }
                        ],
                        "timestamp": 1786620100.0,
                    },
                    {
                        "id": 11,
                        "session_id": "sess-1",
                        "role": "tool",
                        "tool_call_id": "call_9",
                        "content": "file1\nfile2",
                        "timestamp": 1786620101.0,
                    },
                    {
                        "id": 12,
                        "session_id": "sess-1",
                        "role": "assistant",
                        "content": "Authoritative final",
                        "timestamp": 1786620102.0,
                    },
                ],
            },
        ),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("reconcile this")
    user.find(marker="chat-send").click()

    await user.should_see("Authoritative final")
    await user.should_see("terminal:")
    await user.should_not_see("streamed reply")


async def test_send_message_refreshes_message_count(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("3 messages")

    user.find(marker="chat-input").type("another turn")
    user.find(marker="chat-send").click()

    await user.should_see("5 messages")


async def test_empty_session_shows_placeholder(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-2")
    await user.should_see("No messages yet.")


async def test_load_earlier_messages_paginates(
    user: User, context: PluginContext, fake_hermes_cli
) -> None:
    """Seed sess-1 past the default 100-message page so the detail page's
    "Load earlier" control has something to fetch, and its own oldest
    message (id 1, "How do I access your API?") starts out unloaded."""
    con = sqlite3.connect(fake_hermes_cli._state_db_path())
    con.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        [(i, "sess-1", "user", f"filler message {i}", 1786620000.0 + i) for i in range(4, 105)],
    )
    con.commit()
    con.close()

    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("filler message 104")
    await user.should_not_see("How do I access your API?")

    user.find(marker="load-earlier-button").click()
    await user.should_see("How do I access your API?")


async def test_stop_button_interrupts_running_turn(
    user: User, context: PluginContext, hermes
) -> None:
    """While a turn streams, the stop button asks the gateway to interrupt the
    run (via /v1/runs/{run_id}/stop) and the composer returns to idle."""
    hermes.stream_delay = 0.5  # keep the SSE stream open so stop can land
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("long turn")
    user.find(marker="chat-send").click()
    # Let run.started arrive (it carries the run id) before stopping.
    await asyncio.sleep(0.1)

    user.find(marker="chat-stop").click()
    await asyncio.sleep(0.3)

    assert hermes.stop_calls == ["run_1"]
    # The composer recovered: a fresh turn still streams a full reply.
    user.find(marker="chat-input").type("after stop")
    user.find(marker="chat-send").click()
    await user.should_see("after stop")
    await user.should_see("Hello world")


async def test_message_sent_mid_turn_is_queued_then_delivered(
    user: User, context: PluginContext, hermes
) -> None:
    """A message sent while a turn is streaming is shown as queued and sent as
    the next turn after the current one completes -- never dropped, never run
    concurrently against the same session."""
    hermes.stream_delay = 0.2
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("first turn")
    user.find(marker="chat-send").click()
    await asyncio.sleep(0.05)  # let the first turn start streaming

    user.find(marker="chat-input").type("second turn")
    user.find(marker="chat-send").click()
    await user.should_see("Queued — will send after this turn completes")

    # The second message was NOT sent concurrently -- it is only queued.
    recorded = [m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"]
    assert "second turn" not in recorded

    # Once the first turn completes, the queued turn is delivered in order.
    for _ in range(50):
        recorded = [
            m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"
        ]
        if recorded[-2:] == ["first turn", "second turn"]:
            break
        await asyncio.sleep(0.1)
    assert recorded[-2:] == ["first turn", "second turn"]
    assert hermes.sessions[0]["message_count"] == 7  # 3 seeded + 2 per turn
    await user.should_see("second turn")


async def test_running_session_shows_indicator(user: User, context: PluginContext) -> None:
    """sess-1 is seeded with a live activity label -> header shows a spinner
    + 'Running' + the short description."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")
    await user.should_see("Running")
    await user.should_see("sequential tool running")


async def test_idle_session_shows_no_running_indicator(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-2")
    await user.should_see("No messages yet.")
    await user.should_not_see(marker="session-running", retries=20)


# -- chat file uploads --------------------------------------------------


async def _upload(user: User, name: str, data: bytes) -> None:
    """Drive the chat composer's uploader directly, like a picked file."""
    uploader = next(iter(user.find(kind=ui.upload).elements))
    with user.client:
        await uploader.handle_uploads(
            [SmallFileUpload(name=name, content_type="application/octet-stream", _data=data)]
        )


def _user_contents(hermes) -> list[str]:
    return [m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"]


async def test_chat_has_file_upload_control(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(chat_uploads_dir=str(tmp_path / "uploads"))
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    # `find` (unlike `should_see`) is a single non-retrying lookup, so wait
    # for the page build to land first -- the compose card renders last.
    await user.should_see("How do I access your API?")
    assert user.find(marker="chat-upload").elements


async def test_upload_saves_file_and_sends_its_path(
    user: User,
    make_context: Callable[..., PluginContext],
    tmp_path: Path,
    hermes,
) -> None:
    uploads = tmp_path / "uploads"
    context = make_context(chat_uploads_dir=str(uploads))
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    await _upload(user, "notes.txt", b"hello world")
    # The chip only renders after the save lands, so seeing it means the
    # file is on disk.
    await user.should_see("notes.txt", retries=20)
    saved = uploads / "sess-1" / "notes.txt"
    assert saved.read_text() == "hello world"

    user.find(marker="chat-input").type("Read this")
    user.find(marker="chat-send").click()
    await user.should_see("Read this")
    # The reply only streams in after the gateway recorded the turn, so
    # waiting for it means `hermes.messages` is safe to read.
    await user.should_see("Hello world")

    assert any("Attached files:" in c and str(saved) in c for c in _user_contents(hermes))
    # Attachments are consumed: the composer is empty again.
    await user.should_not_see(marker="chat-attachment", retries=20)


async def test_upload_rejects_oversize_file(
    user: User, make_context: Callable[..., PluginContext], tmp_path: Path
) -> None:
    context = make_context(
        chat_uploads_dir=str(tmp_path / "uploads"),
        chat_upload_max_bytes=10,
    )
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    await _upload(user, "big.txt", b"x" * 20)
    await user.should_see("too large", retries=20)
    assert not (tmp_path / "uploads" / "sess-1" / "big.txt").exists()


async def test_attachment_can_be_removed_before_send(
    user: User,
    make_context: Callable[..., PluginContext],
    tmp_path: Path,
    hermes,
) -> None:
    uploads = tmp_path / "uploads"
    context = make_context(chat_uploads_dir=str(uploads))
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    await _upload(user, "doomed.txt", b"bye")
    await user.should_see("doomed.txt", retries=20)

    user.find(marker="chat-attachment-remove").click()
    await user.should_not_see(marker="chat-attachment", retries=20)
    # The discarded file is deleted from disk too.
    assert not (uploads / "sess-1" / "doomed.txt").exists()

    user.find(marker="chat-input").type("no attachment")
    user.find(marker="chat-send").click()
    assert all("Attached files:" not in c for c in _user_contents(hermes))


async def test_attachment_only_message_sends(
    user: User,
    make_context: Callable[..., PluginContext],
    tmp_path: Path,
    hermes,
) -> None:
    context = make_context(chat_uploads_dir=str(tmp_path / "uploads"))
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    await _upload(user, "only.txt", b"just me")
    await user.should_see("only.txt", retries=20)

    # No text typed -- the attachment alone is a valid message.
    user.find(marker="chat-send").click()
    await user.should_see("Hello world")
    assert any("Attached files:" in c for c in _user_contents(hermes))
    # Attachments were consumed by the send: no chips left in the composer.
    await user.should_not_see(marker="chat-attachment", retries=20)


async def test_live_tool_call_precedes_streamed_reply(
    user: User, context: PluginContext, hermes
) -> None:
    hermes.stream_events = [
        ("run.started", {"run_id": "run_order"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 20, "delta": "Let me check that"}),
        (
            "tool.started",
            {
                "message_id": 20,
                "tool_name": "terminal",
                "args": '{"command": "ls"}',
            },
        ),
        ("tool.completed", {"message_id": 20, "tool_name": "terminal"}),
        ("assistant.delta", {"message_id": 20, "delta": " and here is the result"}),
        (
            "assistant.completed",
            {"message_id": 20, "content": "Let me check that and here is the result"},
        ),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("check ordering")
    user.find(marker="chat-send").click()

    await user.should_see(marker="live-tool", retries=20)
    await user.should_see(marker="live-reply", retries=20)
    timeline = next(iter(user.find(kind=ui.timeline).elements))
    marked = [
        child._props.get("mark") or " ".join(child._markers)
        for child in timeline.default_slot.children
    ]
    assert marked.index("live-tool") < marked.index("live-reply")
    await user.should_see("terminal:")


async def test_failed_send_does_not_appear_in_transcript(
    user: User,
    context: PluginContext,
    hermes,
) -> None:
    """A send that fails (500) must NOT leave a ghost user message in the
    transcript, and the input box must be restored with the original text."""
    hermes.stream_error = 500
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("failed message")
    user.find(marker="chat-send").click()

    # The user message must NOT appear in the transcript timeline.
    timelines = list(user.find(kind=ui.timeline).elements)
    assert len(timelines) == 1
    # Count entries that contain "failed message" in their DOM
    has_ghost = False
    for entry in timelines[0].default_slot.children:
        for child in entry.default_slot.children:
            attrs = getattr(child, "_props", {})
            text = attrs.get("text", "") or attrs.get("content", "")
            if "failed message" in text:
                has_ghost = True
                break
        if has_ghost:
            break
    assert not has_ghost, "ghost user message found in timeline"
    # The input box should be restored with the original text.
    assert next(iter(user.find(marker="chat-input").elements)).value == "failed message"
