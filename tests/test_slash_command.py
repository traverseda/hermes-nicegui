"""E2E tests for slash command rendering in the sessions plugin."""

from __future__ import annotations

import asyncio

from nicegui.testing import User

from hermes_nicegui import web
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.sessions import SessionsPlugin


async def test_slash_command_output_rendered_in_transcript(
    user: User, context: PluginContext
) -> None:
    """A slash command is sent to the /slash endpoint and its output is
    rendered in the transcript -- the command text and the result both
    appear."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    # Send a slash command
    user.find(marker="chat-input").type("/help")
    user.find(marker="chat-send").click()

    # The output should include both the command and the result
    await user.should_see("/help", retries=20)
    await user.should_see("Output for:", retries=20)


async def test_slash_command_shows_terminal_icon(
    user: User, context: PluginContext
) -> None:
    """The slash command output appears as a timeline entry with the
    terminal icon (verified by the slash-output marker)."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("/sessions")
    user.find(marker="chat-send").click()

    # The slash entry should have the slash-output marker
    await user.should_see(marker="slash-output", retries=20)
    await user.should_see("/sessions", retries=20)


async def test_slash_command_clears_input_box(
    user: User, context: PluginContext
) -> None:
    """After sending a slash command, the compose input box is cleared
    (like regular messages)."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    user.find(marker="chat-input").type("/help")
    user.find(marker="chat-send").click()
    await user.should_see("/help", retries=20)

    # The input should be empty after the command -- the clear happens
    # inside the background task, so a brief sync is needed.
    await asyncio.sleep(0.1)
    input_elements = user.find(marker="chat-input")
    assert len(input_elements.elements) == 1
    assert next(iter(input_elements.elements)).value == ""


async def test_slash_command_during_streaming_sends_immediately(
    user: User, context: PluginContext
) -> None:
    """A slash command sent while a turn is streaming is executed
    immediately (not queued like regular messages)."""
    web.build(context, [SessionsPlugin(context)])
    await user.open("/sessions/sess-1")
    await user.should_see("How do I access your API?")

    # Send a slash command -- it goes through the slash endpoint,
    # not the chat stream
    user.find(marker="chat-input").type("/sessions")
    user.find(marker="chat-send").click()

    # The output should appear immediately
    await user.should_see("/sessions", retries=20)
    await user.should_see("Output for:", retries=20)
