"""E2E tests for queued messages in the sessions plugin."""

from __future__ import annotations

import asyncio

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin


async def test_queued_message_shows_badge_during_streaming(
    user: User, context: PluginContext, hermes
) -> None:
    """A message sent while a turn is streaming is immediately shown as
    queued -- the \"Queued\" badge text is visible -- and is NOT sent to
    the gateway until the current turn completes."""
    hermes.stream_delay = 0.2
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    # Start the first turn
    user.find(marker="chat-input").type("first turn")
    user.find(marker="chat-send").click()
    await asyncio.sleep(0.05)  # let the first turn start streaming

    # Queue a second message while the first is still streaming
    user.find(marker="chat-input").type("queued message")
    user.find(marker="chat-send").click()

    # The queued badge should be visible
    await user.should_see("Queued — will send after this turn completes")

    # The queued message was NOT sent to the gateway during streaming
    recorded = [
        m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"
    ]
    assert "queued message" not in recorded
    assert recorded[-1] == "first turn"


async def test_queued_message_delivered_after_turn_completes(
    user: User, context: PluginContext, hermes
) -> None:
    """Once the streaming turn finishes, any queued message is automatically
    sent as the next turn and appears in the transcript."""
    hermes.stream_delay = 0.2
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("first turn")
    user.find(marker="chat-send").click()
    await asyncio.sleep(0.05)

    user.find(marker="chat-input").type("queued message")
    user.find(marker="chat-send").click()
    await user.should_see("Queued — will send after this turn completes")

    # Wait for the first turn to complete and the queued message to be sent
    for _ in range(50):
        recorded = [
            m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"
        ]
        if recorded[-2:] == ["first turn", "queued message"]:
            break
        await asyncio.sleep(0.1)

    assert recorded[-2:] == ["first turn", "queued message"]
    # The user should see the queued message in the transcript
    await user.should_see("queued message")


async def test_multiple_queued_messages_merge(
    user: User, context: PluginContext, hermes
) -> None:
    """Messages sent while another is queued merge into a single queued
    turn. They are delivered together (as one message with merged text)
    once the current turn completes."""
    hermes.stream_delay = 0.3
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    # Start first turn
    user.find(marker="chat-input").type("first turn")
    user.find(marker="chat-send").click()
    await asyncio.sleep(0.05)

    # Queue two messages
    user.find(marker="chat-input").type("queued one")
    user.find(marker="chat-send").click()
    await user.should_see("Queued — will send after this turn completes")

    # Queue another while already queued (should merge)
    user.find(marker="chat-input").type("queued two")
    user.find(marker="chat-send").click()
    await user.should_see("Queued — will send after this turn completes")

    # Wait for the first turn to finish and the merged queue to be sent
    found_merged = False
    for _ in range(50):
        recorded = [
            m["content"] for m in hermes.messages["sess-1"] if m["role"] == "user"
        ]
        merged = [m for m in recorded if "queued one" in m and "queued two" in m]
        if merged:
            found_merged = True
            break
        await asyncio.sleep(0.1)

    assert found_merged, "Merged queued messages not found"
    await user.should_see("queued one")
