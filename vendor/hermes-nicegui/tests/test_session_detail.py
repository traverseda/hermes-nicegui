"""Tests for the session detail/transcript page and inline chat."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

from nicegui import ui
from nicegui.elements.upload_files import SmallFileUpload
from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.gateway import Message
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin
from hermes_nicegui.plugins.sessions.ui import (
    _delete_if_attached,
    render_transcript_events,
)


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


async def test_live_reasoning_shown_in_timeline(user: User, context: PluginContext) -> None:
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


async def test_live_render_keeps_multiple_assistant_messages_separate(
    user: User, context: PluginContext, hermes
) -> None:
    """A single run can produce more than one assistant message -- reason,
    call a tool, reason again, call another, only then reply -- each its own
    message server-side (confirmed against a real `run.completed` transcript).
    The live path used to treat the whole run as one pending message,
    concatenating every step's own `content` into one string with no
    separator between them -- confirmed live: a session doing a two-step
    tool loop rendered its intermediate reply glued directly onto its final
    one (`"...memory.Got it — ..."`, no space, no line break)."""
    hermes.stream_events = [
        ("run.started", {"run_id": "run_multi"}),
        ("message.started", {"message": {"id": 30, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 30, "delta": "I'll store that in your memory."}),
        (
            "assistant.completed",
            {"message_id": 30, "content": "I'll store that in your memory."},
        ),
        (
            "tool.started",
            {"message_id": 31, "tool_call_id": "call_b", "tool_name": "capture", "args": "{}"},
        ),
        ("tool.completed", {"message_id": 31, "tool_call_id": "call_b", "tool_name": "capture"}),
        ("message.started", {"message": {"id": 32, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 32, "delta": "Got it — saved."}),
        ("assistant.completed", {"message_id": 32}),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("remember this")
    user.find(marker="chat-send").click()

    await user.should_see("Got it — saved.", retries=20)
    await user.should_see("I'll store that in your memory.")
    await user.should_not_see("memory.Got it")


async def test_run_completed_reconciliation_skips_message_missing_id(
    user: User, context: PluginContext, hermes
) -> None:
    """The gateway's `run.completed` payload has been observed omitting `id`
    entirely on some message entries (confirmed live against hermes.lan's
    production logs: `Message.from_json` raised `KeyError: 'id'` inside this
    reconciliation, repeatedly, with no exception handler anywhere in the
    live-rendering coroutine -- silently killing it mid-turn, which read as
    "the page just freezes" since NiceGUI itself stayed healthy, and dropped
    every *valid* message in the same payload along with the bad one). One
    malformed entry must not take down the whole reconciliation."""
    hermes.stream_events = [
        ("run.started", {"run_id": "run_bad_id"}),
        ("message.started", {"message": {"id": 40, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 40, "delta": "Working on it."}),
        (
            "run.completed",
            {
                "completed": True,
                "usage": {},
                "messages": [
                    {"session_id": "sess-1", "role": "assistant", "content": "Working on it."},
                    {
                        "id": 40,
                        "session_id": "sess-1",
                        "role": "assistant",
                        "content": "All done.",
                    },
                ],
            },
        ),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("do the thing")
    user.find(marker="chat-send").click()

    await user.should_see("All done.", retries=20)


async def test_live_tool_cleanup_ignores_detached_spinner(
    user: User, context: PluginContext, hermes
) -> None:
    hermes.stream_delay = 0.1
    hermes.stream_events = [
        ("run.started", {"run_id": "run_detach"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        (
            "tool.started",
            {"message_id": 20, "tool_name": "terminal", "args": '{"command": "ls"}'},
        ),
        ("tool.completed", {"message_id": 20, "tool_name": "terminal"}),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("detach during tool cleanup")
    user.find(marker="chat-send").click()
    await user.should_see(marker="live-tool", retries=20)
    await user.should_see(marker="live-tool-done", retries=20)

    # A live tool row built through the single render path, then detached
    # before cleanup, must not crash `_delete_if_attached` -- the same
    # detach-safety that matters during a concurrent page teardown.
    messages = [
        Message(
            id=50,
            session_id="sess-1",
            role="assistant",
            content="",
            tool_calls=[
                {
                    "id": "call_detach",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command": "ls"}'},
                }
            ],
        )
    ]
    timeline = next(iter(user.find(kind=ui.timeline).elements))
    with timeline:
        handles = render_transcript_events(
            messages, active_id=50, tool_status={"call_detach": "running"}
        )
    live_tool = handles[50].tool_rows["call_detach"]
    assert live_tool.spinner is not None
    slot = live_tool.spinner.parent_slot
    assert slot is not None
    slot.parent.delete()  # detach the whole row
    _delete_if_attached(live_tool.spinner)


async def test_compressed_messages_get_a_boundary_marker(
    user: User, context: PluginContext
) -> None:
    """A run of ``compacted`` messages followed by a live one gets a
    "Context compressed" divider ahead of the live message -- the visible
    trace of the daemon's in-place context compaction (``state.db``'s
    ``messages.compacted``), which otherwise leaves no visual cue that older
    turns were archived and summarized away."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    messages = [
        Message(
            id=101, session_id="sess-1", role="user", content="archived turn one",
            compacted=True,
        ),
        Message(
            id=102, session_id="sess-1", role="assistant", content="archived reply",
            compacted=True,
        ),
        Message(
            id=103, session_id="sess-1", role="user", content="summary handoff",
        ),
    ]
    timeline = next(iter(user.find(kind=ui.timeline).elements))
    with timeline:
        render_transcript_events(messages)
    await user.should_see("Context compressed — 2 earlier messages archived")


async def test_no_thinking_spinner_during_turn(user: User, context: PluginContext, hermes) -> None:
    """The per-turn "Thinking…" status row is gone: while a turn streams
    (reasoning delta in flight), that exact label never appears on the page."""
    hermes.stream_delay = 0.2
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("no spinner")
    user.find(marker="chat-send").click()

    # Wait until we are unambiguously mid-turn (reasoning streamed), then
    # check no "Thinking…" label exists anywhere on the page.
    await user.should_see(marker="live-reasoning", retries=20)
    await user.should_not_see("Thinking…", retries=20)
    await user.should_see("thinking…")
    await user.should_see("Hello world", retries=30)


async def test_out_of_order_arrival_renders_canonical_order(
    user: User, context: PluginContext, hermes
) -> None:
    """A deliberately scrambled SSE order (reply delta before tool.started)
    still renders canonically -- user -> tool -> reply -- because events fold
    into the message buffer and rendering follows buffer order, not arrival
    order."""
    hermes.stream_events = [
        ("run.started", {"run_id": "run_scrambled"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 20, "delta": "first delta"}),
        (
            "tool.started",
            {
                "message_id": 20,
                "tool_name": "terminal",
                "args": '{"command": "ls"}',
            },
        ),
        ("assistant.delta", {"message_id": 20, "delta": " and second delta"}),
        ("tool.completed", {"message_id": 20, "tool_name": "terminal"}),
        (
            "assistant.completed",
            {"message_id": 20, "content": "first delta and second delta"},
        ),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("scrambled order")
    user.find(marker="chat-send").click()

    await user.should_see(marker="live-tool", retries=20)
    await user.should_see(marker="live-reply", retries=20)
    timeline = next(iter(user.find(kind=ui.timeline).elements))
    marked = [
        child._props.get("mark") or " ".join(child._markers)
        for child in timeline.default_slot.children
    ]
    # Canonical, not arrival order: the tool row (which arrived after the
    # reply bubble started) still precedes the reply.
    assert marked.index("live-tool") < marked.index("live-reply")
    await user.should_see("terminal:")
    await user.should_see("first delta and second delta")


async def test_thinking_tool_progress_is_reasoning_not_tool_row(
    user: User, context: PluginContext, hermes
) -> None:
    """A `tool.progress` named `_thinking` folds into the reasoning body and
    never produces a `live-tool` row."""
    hermes.stream_events = [
        ("run.started", {"run_id": "run_thinking"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("tool.progress", {"message_id": 20, "tool_name": "_thinking", "delta": "reasoning text"}),
        ("assistant.delta", {"message_id": 20, "delta": "reply"}),
        ("assistant.completed", {"message_id": 20, "content": "reply"}),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("thinking")
    user.find(marker="chat-send").click()

    await user.should_see(marker="live-reasoning", retries=20)
    await user.should_see("reasoning text")
    await user.should_not_see(marker="live-tool", retries=20)


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


async def test_concurrent_tool_calls_attribute_completion_correctly(
    user: User, context: PluginContext, hermes
) -> None:
    """Two tool calls in flight at once, completing in reverse start order,
    must each land on their own row -- not on "whichever was started last"
    (the daemon's chat-stream endpoint never sends a tool_call_id on
    tool.completed, so the client can only disambiguate by name)."""
    hermes.stream_events = [
        ("run.started", {"run_id": "run_concurrent"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("tool.started", {"message_id": 20, "tool_name": "terminal", "args": '{"command": "ls"}'}),
        ("tool.started", {"message_id": 20, "tool_name": "web_search", "args": '{"q": "weather"}'}),
        # web_search started 2nd but finishes 1st; terminal started 1st but finishes 2nd.
        ("tool.completed", {"message_id": 20, "tool_name": "web_search"}),
        ("tool.completed", {"message_id": 20, "tool_name": "terminal"}),
        ("assistant.delta", {"message_id": 20, "delta": "Done"}),
        ("assistant.completed", {"message_id": 20, "content": "Done"}),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("run two tools")
    user.find(marker="chat-send").click()

    await user.should_see("terminal:")
    await user.should_see("web_search:")
    done_marks = user.find(marker="live-tool-done").elements
    for _ in range(30):
        if len(done_marks) >= 2:
            break
        await asyncio.sleep(0.05)
        done_marks = user.find(marker="live-tool-done").elements
    assert len(done_marks) == 2, "both tool rows should finish -- neither stuck spinning"


async def test_queued_message_survives_authoritative_reconciliation(
    user: User, context: PluginContext, hermes
) -> None:
    """A message queued mid-turn must not be wiped out when the *current*
    turn's own run.completed reconciliation replaces its messages."""
    hermes.stream_delay = 0.2
    hermes.stream_events = [
        ("run.started", {"run_id": "run_first"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("assistant.delta", {"message_id": 20, "delta": "first reply"}),
        ("assistant.completed", {"message_id": 20, "content": "first reply"}),
        (
            "run.completed",
            {
                "completed": True,
                "usage": {},
                "messages": [
                    {
                        "id": 30,
                        "session_id": "sess-1",
                        "role": "user",
                        "content": "first turn",
                        "timestamp": 1786620100.0,
                    },
                    {
                        "id": 31,
                        "session_id": "sess-1",
                        "role": "assistant",
                        "content": "first reply (authoritative)",
                        "timestamp": 1786620101.0,
                    },
                ],
            },
        ),
        ("done", {}),
    ]
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("first turn")
    user.find(marker="chat-send").click()
    await asyncio.sleep(0.05)  # let the first turn start streaming

    user.find(marker="chat-input").type("second turn")
    user.find(marker="chat-send").click()
    await user.should_see("Queued — will send after this turn completes")

    # The reconciled first turn lands...
    await user.should_see("first reply (authoritative)", retries=30)
    # ...and the queued message must still be visible right after it.
    await user.should_see("second turn")


async def test_load_earlier_disabled_while_turn_streaming(
    user: User, context: PluginContext, hermes, fake_hermes_cli
) -> None:
    """"Load earlier" must not run concurrently with a live turn's own
    rendering -- see the history/live split in ui.py -- so it's disabled for
    the duration of a streaming turn and re-enabled once it ends."""
    con = sqlite3.connect(fake_hermes_cli._state_db_path())
    con.executemany(
        "INSERT INTO messages (id, session_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        [(i, "sess-1", "user", f"filler message {i}", 1786620000.0 + i) for i in range(4, 105)],
    )
    con.commit()
    con.close()
    hermes.stream_delay = 0.3

    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("filler message 104")

    user.find(marker="chat-input").type("long turn")
    user.find(marker="chat-send").click()
    await asyncio.sleep(0.05)  # into the streaming turn

    button = next(iter(user.find(marker="load-earlier-button").elements))
    assert button.enabled is False

    user.find(marker="load-earlier-button").click()
    await asyncio.sleep(0.1)
    await user.should_not_see("How do I access your API?")  # click was a no-op

    for _ in range(30):
        if button.enabled:
            break
        await asyncio.sleep(0.1)
    assert button.enabled is True

    user.find(marker="load-earlier-button").click()
    await user.should_see("How do I access your API?")


async def test_tool_started_does_not_rebuild_history(
    user: User, context: PluginContext, hermes
) -> None:
    """A structural event on the live turn (a new tool call starting) must
    not tear down and rebuild the static historical prefix -- only the live
    region grows. Regression test for the O(history length) rebuild that
    `_render_history`/`_render_live` replaced `_render_transcript` with:
    counts actual `ui.timeline()` constructions rather than comparing
    element ids across time, since NiceGUI's own testing `ElementFilter`
    can transiently surface a just-deleted element's id alongside its
    replacement, which makes identity comparison across an `await` an
    unreliable signal even when nothing was actually rebuilt."""
    hermes.stream_delay = 0.2
    hermes.stream_events = [
        ("run.started", {"run_id": "run_cheap"}),
        ("message.started", {"message": {"id": 20, "role": "assistant"}}),
        ("tool.started", {"message_id": 20, "tool_name": "terminal", "args": '{"command": "ls"}'}),
        ("tool.completed", {"message_id": 20, "tool_name": "terminal"}),
        ("assistant.completed", {"message_id": 20, "content": "Done"}),
        ("run.completed", {"completed": True, "usage": {}}),
        ("done", {}),
    ]
    construct_count = 0
    original_init = ui.timeline.__init__

    def counting_init(self: ui.timeline, *args: object, **kwargs: object) -> None:
        nonlocal construct_count
        construct_count += 1
        original_init(self, *args, **kwargs)

    with patch.object(ui.timeline, "__init__", counting_init):
        web.build(context, [SessionsPlugin(context)])
        await user.open("/sessions/sess-1")
        await user.should_see("How do I access your API?")
        assert construct_count == 1  # initial historical render

        user.find(marker="chat-input").type("watch history")
        user.find(marker="chat-send").click()
        for _ in range(30):
            if construct_count >= 2:
                break
            await asyncio.sleep(0.05)
        assert construct_count == 2  # turn start: one more historical render

        await user.should_see(marker="live-tool", retries=20)
        assert construct_count == 2, (
            "tool.started is a live-region event -- it must not reconstruct "
            "the history timeline"
        )


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
        recorded = [m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"]
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


async def test_stop_button_stops_foreign_running_session(
    user: User, context: PluginContext, hermes
) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("Running")

    user.find(marker="chat-stop").click()
    await asyncio.sleep(0.3)

    assert hermes.stop_session_calls == ["sess-1"]
    assert hermes.stop_calls == []
    await user.should_not_see(marker="session-running", retries=20)


async def test_stop_button_hidden_for_idle_session(user: User, context: PluginContext) -> None:
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-2")
    await user.should_not_see(marker="chat-stop", retries=20)


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
