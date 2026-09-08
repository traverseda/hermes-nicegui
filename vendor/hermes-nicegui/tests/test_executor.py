"""Tests for `HermesExecutor.argv` -- pure argv construction, no subprocess.

Nothing else in the suite exercised this: `conftest.py`'s `FakeHermesCli`
fakes out `run_fn` entirely, so the real ssh/pty argv this builds was never
checked against what actually works on a real host. `-tt` (forced remote
pty) hangs indefinitely on a one-shot command like `sessions export` --
confirmed against `hermesagent.lan` -- so `run()` must never request it,
and only the interactive chat pty (`tty=True`) may.
"""

from __future__ import annotations

import json
import sqlite3

from hermes_nicegui.executor import HermesExecutor


def test_local_argv_ignores_tty() -> None:
    executor = HermesExecutor(mode="local", hermes_bin="hermes")
    assert executor.argv("sessions", "export") == ["hermes", "sessions", "export"]
    assert executor.argv("chat", tty=True) == ["hermes", "chat"]


def test_ssh_argv_omits_tt_by_default() -> None:
    executor = HermesExecutor(mode="ssh", hermes_bin="hermes", ssh_target="hermes@host")
    argv = executor.argv("sessions", "export", "--format", "jsonl", "-")
    assert "-tt" not in argv
    assert argv == ["ssh", "hermes@host", "hermes", "sessions", "export", "--format", "jsonl", "-"]


def test_ssh_argv_adds_tt_only_for_tty() -> None:
    executor = HermesExecutor(mode="ssh", hermes_bin="hermes", ssh_target="hermes@host")
    argv = executor.argv("chat", tty=True)
    assert argv == ["ssh", "-tt", "hermes@host", "hermes", "chat"]


def test_run_never_requests_tty(monkeypatch) -> None:
    """`run()` -- used for `sessions`/`cron` one-shot commands -- must build
    its argv the same way `argv(tty=False)` (the default) does, since a
    forced remote pty is what caused those commands to hang."""
    seen: list[list[str]] = []

    async def fake_run_fn(argv: list[str]):
        from hermes_nicegui.executor import CompletedResult

        seen.append(argv)
        return CompletedResult(0, "", "")

    executor = HermesExecutor(
        mode="ssh", hermes_bin="hermes", ssh_target="hermes@host", run_fn=fake_run_fn
    )
    import asyncio

    asyncio.run(executor.run("sessions", "export", "--format", "jsonl", "-"))
    assert "-tt" not in seen[0]


def test_remote_io_argv_defaults_to_bare_python3() -> None:
    executor = HermesExecutor(mode="ssh", hermes_bin="hermes", ssh_target="hermes@host")
    assert executor._remote_io_argv() == ["ssh", "hermes@host", "python3", "-"]


def test_remote_io_argv_honors_configured_python_bin() -> None:
    """Some hosts' non-interactive PATH has no bare `python3` at all
    (confirmed against a real NixOS host, `hermes.lan`) -- `remote_python_bin`
    lets the read path point at an absolute interpreter path instead."""
    executor = HermesExecutor(
        mode="ssh",
        hermes_bin="hermes",
        ssh_target="hermes@host",
        remote_python_bin="/nix/store/xyz-hermes-agent-env/bin/python3",
    )
    assert executor._remote_io_argv() == [
        "ssh",
        "hermes@host",
        "/nix/store/xyz-hermes-agent-env/bin/python3",
        "-",
    ]


# -- read primitives (state.db/cron/jobs.json/profiles, local mode) ---------
#
# Local-mode reads open a real file directly -- no fake subprocess boundary
# needed, unlike `run()`'s CLI-subprocess path. These exercise `read_sqlite`/
# `read_json`/`list_profile_names`/`profile_home` against real tmp files.


def test_profile_home() -> None:
    executor = HermesExecutor(hermes_home="/home/hermes/.hermes")
    assert executor.profile_home() == "/home/hermes/.hermes"
    assert executor.profile_home("default") == "/home/hermes/.hermes"
    assert executor.profile_home("ha") == "/home/hermes/.hermes/profiles/ha"


async def test_read_sqlite_against_real_local_file(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO sessions (id, title) VALUES ('sess-1', 'First session')")
    con.commit()
    con.close()

    executor = HermesExecutor(hermes_home=str(tmp_path))
    rows = await executor.read_sqlite("state.db", "SELECT * FROM sessions")
    assert rows == [{"id": "sess-1", "title": "First session"}]


async def test_read_sqlite_missing_file_returns_empty(tmp_path) -> None:
    executor = HermesExecutor(hermes_home=str(tmp_path))
    assert await executor.read_sqlite("state.db", "SELECT * FROM sessions") == []


async def test_read_sqlite_is_read_only(tmp_path) -> None:
    """Read methods must never be able to write, even if called with a
    mutating statement -- this is what makes it safe to read alongside the
    live daemon process without touching its own locking."""
    db_path = tmp_path / "state.db"
    sqlite3.connect(db_path).execute("CREATE TABLE t (x INTEGER)").connection.commit()

    executor = HermesExecutor(hermes_home=str(tmp_path))
    try:
        await executor.read_sqlite("state.db", "INSERT INTO t VALUES (1)")
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("expected a read-only connection to reject a write")


async def test_read_json_round_trips(tmp_path) -> None:
    (tmp_path / "cron").mkdir()
    (tmp_path / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "j-1"}]}))

    executor = HermesExecutor(hermes_home=str(tmp_path))
    assert await executor.read_json("cron/jobs.json") == {"jobs": [{"id": "j-1"}]}


async def test_read_json_missing_file_returns_none(tmp_path) -> None:
    executor = HermesExecutor(hermes_home=str(tmp_path))
    assert await executor.read_json("cron/jobs.json") is None


async def test_read_sqlite_scoped_to_profile(tmp_path) -> None:
    (tmp_path / "profiles" / "ha").mkdir(parents=True)
    con = sqlite3.connect(tmp_path / "profiles" / "ha" / "state.db")
    con.execute("CREATE TABLE sessions (id TEXT)")
    con.execute("INSERT INTO sessions VALUES ('ha-sess')")
    con.commit()
    con.close()

    executor = HermesExecutor(hermes_home=str(tmp_path))
    assert await executor.read_sqlite("state.db", "SELECT * FROM sessions", profile="ha") == [
        {"id": "ha-sess"}
    ]


async def test_read_sqlite_abs_reads_a_path_outside_hermes_home(tmp_path) -> None:
    """Unlike `read_sqlite`, `read_sqlite_abs` isn't joined onto
    `profile_home` -- it's for another tool's own database (e.g. OpenCode's)
    that happens to live on the same host but outside any Hermes profile
    dir."""
    other_dir = tmp_path / "elsewhere"
    other_dir.mkdir()
    db_path = other_dir / "opencode-stable.db"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT)")
    con.execute("INSERT INTO session (id, title) VALUES ('ses-1', 'a session')")
    con.commit()
    con.close()

    executor = HermesExecutor(hermes_home=str(tmp_path / "hermes-home"))
    rows = await executor.read_sqlite_abs(str(db_path), "SELECT * FROM session")
    assert rows == [{"id": "ses-1", "title": "a session"}]


async def test_read_sqlite_abs_missing_file_returns_empty(tmp_path) -> None:
    executor = HermesExecutor(hermes_home=str(tmp_path))
    missing = tmp_path / "nowhere" / "opencode.db"
    assert await executor.read_sqlite_abs(str(missing), "SELECT * FROM session") == []
    assert await executor.read_sqlite("state.db", "SELECT * FROM sessions") == []


async def test_list_profile_names(tmp_path) -> None:
    (tmp_path / "profiles" / "ha").mkdir(parents=True)
    (tmp_path / "profiles" / "boats").mkdir(parents=True)

    executor = HermesExecutor(hermes_home=str(tmp_path))
    assert await executor.list_profile_names() == ["default", "boats", "ha"]


async def test_list_profile_names_no_profiles_dir(tmp_path) -> None:
    executor = HermesExecutor(hermes_home=str(tmp_path))
    assert await executor.list_profile_names() == ["default"]
