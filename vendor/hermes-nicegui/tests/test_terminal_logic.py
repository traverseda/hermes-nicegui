"""Tests for PtySession: spawns a real local shell in a pty."""

from __future__ import annotations

import os
import sys
import time

import pytest

from hermes_nicegui.plugins.terminal.logic import PtySession


def _drain(session: PtySession, *, until: bytes, timeout: float = 5.0) -> bytes:
    """Poll ``read()`` until ``until`` appears in the accumulated output."""
    buf = b""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = session.read()
        if data:
            buf += data
            if until in buf:
                return buf
        elif data is None:
            return buf
        else:
            time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {until!r}, got {buf!r}")


def test_echoes_written_input() -> None:
    session = PtySession(shell="/bin/sh")
    try:
        session.start()
        session.write(b"echo hello-pty\n")
        assert b"hello-pty" in _drain(session, until=b"hello-pty")
    finally:
        session.close()


def test_reports_eof_after_shell_exits() -> None:
    session = PtySession(shell="/bin/sh")
    try:
        session.start()
        session.write(b"exit\n")
        deadline = time.monotonic() + 5.0
        saw_eof = False
        while time.monotonic() < deadline:
            data = session.read()
            if data is None:
                saw_eof = True
                break
            time.sleep(0.02)
        assert saw_eof
    finally:
        session.close()


def test_resize_does_not_raise() -> None:
    session = PtySession(shell="/bin/sh")
    try:
        session.start()
        session.resize(120, 40)
    finally:
        session.close()


def test_close_terminates_and_reaps_child() -> None:
    session = PtySession(shell="/bin/sh")
    session.start()
    pid = session.pid
    session.close()
    assert session.pid is None
    assert session.fd is None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_read_before_start_returns_none() -> None:
    session = PtySession(shell="/bin/sh")
    assert session.read() is None


def test_args_are_passed_to_the_command() -> None:
    """`args` lets a caller run a specific command (e.g. `hermes -p ha chat`
    for the chat page) instead of just a bare interactive shell."""
    session = PtySession(shell=sys.executable, args=["-c", "print('hello-args')"])
    try:
        session.start()
        assert b"hello-args" in _drain(session, until=b"hello-args")
    finally:
        session.close()
