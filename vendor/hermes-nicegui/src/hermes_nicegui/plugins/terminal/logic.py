"""Pty-spawning helper for the terminal plugin: no NiceGUI, no async.

Kept separate from ``ui.py`` so the process-management half is unit-testable
without a browser, mirroring ``plugins/cron/logic.py`` and
``plugins/sessions/logic.py``.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import pty
import signal
import struct
import termios


class PtySession:
    """A shell process attached to its own pseudo-terminal.

    ``start()`` forks the child and sets the master fd non-blocking, so a
    caller can drive it from an event loop (``asyncio.add_reader``) instead
    of blocking on read.
    """

    def __init__(
        self, shell: str | None = None, cwd: str | None = None, args: list[str] | None = None
    ) -> None:
        self.shell = shell or os.environ.get("SHELL", "/bin/sh")
        self.cwd = cwd
        self.args = args or []
        self.pid: int | None = None
        self.fd: int | None = None

    def start(self) -> int:
        """Fork the child shell and return the master fd."""
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - runs only in the forked child
            if self.cwd:
                os.chdir(self.cwd)
            env = dict(os.environ, TERM="xterm-256color")
            os.execvpe(self.shell, [self.shell, *self.args], env)
        self.pid = pid
        self.fd = fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        return fd

    def write(self, data: bytes) -> None:
        if self.fd is not None:
            os.write(self.fd, data)

    def resize(self, cols: int, rows: int) -> None:
        if self.fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    def read(self, size: int = 4096) -> bytes | None:
        """Read available output.

        Returns ``b""`` if nothing is available yet, or ``None`` once the
        shell has exited (Linux reports that as ``EIO`` on the master fd,
        not an empty read).
        """
        if self.fd is None:
            return None
        try:
            data = os.read(self.fd, size)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            return None
        return data if data else None

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        if self.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGHUP)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)
            self.pid = None
