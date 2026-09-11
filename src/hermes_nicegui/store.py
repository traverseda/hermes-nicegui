"""The joint layer of indirection: one facade for "the hermes daemon and
profiles" that every plugin reaches through instead of hand-combining a
``HermesExecutor`` and a profile name at each call site.

Reads go straight at the daemon's own SQLite databases/JSON files
(``HermesExecutor.read_sqlite``/``read_json``) -- fast, and correct alongside
the live daemon process since those are always opened read-only. Mutations
delegate to ``hermes_cli``, which runs the actual `hermes` CLI -- the thing
that safely maintains the daemon's own invariants (message counts, FTS
triggers, cron ticker locks, ...).

No NiceGUI import here (mirrors ``gateway.py``/``hermes_cli.py``): plugins
reach this through ``hermes_nicegui.web.current_store()``, which is the only
place that knows about ``app.storage``/the active browser tab.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from hermes_nicegui import hermes_cli
from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.gateway import (
    Comment,
    CronRun,
    HermesError,
    Job,
    Message,
    Session,
    Task,
    TaskDetail,
)

# `Session.from_json` reads `last_active`, but the on-disk column is
# `last_activity_at` -- aliased explicitly rather than relying on callers to
# know the storage-layer name. `:search`, when given, is a `LIKE` pattern
# (caller wraps the raw query in `%...%`) matched against title, source, or
# any message in the session -- a superset of the `preview` column (the
# first user message only), so a hit inside a later message still surfaces
# the session, same as substring-matching the old, fully-materialized list
# in Python did.
_SESSIONS_LIST_SQL = """
SELECT s.*, s.last_activity_at AS last_active, (
    SELECT SUBSTR(content, 1, 500) FROM messages m
    WHERE m.session_id = s.id AND m.role = 'user' AND m.content IS NOT NULL
    ORDER BY m.id LIMIT 1
) AS preview
FROM sessions s
WHERE (:source IS NULL OR s.source = :source)
  AND (
    :search IS NULL
    OR s.title LIKE :search
    OR s.source LIKE :search
    OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.content LIKE :search)
  )
ORDER BY s.last_activity_at DESC
LIMIT :limit OFFSET :offset
"""
_SESSIONS_COUNT_SQL = """
SELECT COUNT(*) AS n FROM sessions s
WHERE (:source IS NULL OR s.source = :source)
  AND (
    :search IS NULL
    OR s.title LIKE :search
    OR s.source LIKE :search
    OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id AND m.content LIKE :search)
  )
"""

_SESSION_ROW_SQL = "SELECT *, last_activity_at AS last_active FROM sessions WHERE id = ?"

# Keyset (not offset) pagination: `:before_id` anchors on `messages.id`, which
# is monotonic per session, so "the next page back" is stable even if newer
# messages are being appended concurrently (an offset would skew under
# concurrent inserts). Fetches `limit + 1` rows so `get_messages_page` can
# tell whether an even-older page exists from the row count alone, without a
# second COUNT query.
_SESSION_MESSAGES_PAGE_SQL = """
SELECT * FROM messages
WHERE session_id = :session_id AND (:before_id IS NULL OR id < :before_id)
ORDER BY id DESC LIMIT :limit
"""

DEFAULT_MESSAGES_PAGE_SIZE = 100

# How close a cron output file's wall-clock timestamp must be to a run's
# anchor timestamp for it to be attributed to that run -- a stale or
# unrelated file in the same directory must not be matched.
_RUN_OUTPUT_TOLERANCE_S = 600
# Same idea for the state.db session a cron run spawns: its `started_at`
# must be within this of the run's `claimed_at`.
_RUN_SESSION_TOLERANCE_S = 1800


def _naive_wall_clock(value: str | float | None) -> datetime | None:
    """Parse a daemon timestamp -- an ISO-8601 string (``...T...``, with or
    without tzinfo) or a unix epoch float -- into a *naive* wall-clock
    ``datetime`` (tzinfo stripped).

    Cron output filenames and cron session ids are stamped with the
    scheduler's local wall clock, so the same value seen as a tz-aware ISO
    string (e.g. ``2026-08-15T11:51:48+00:00``) has to be compared against
    that wall clock in its own (stripped) frame -- not converted to UTC,
    which could shift it by the host's offset and silently break the
    tolerance comparison.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_output_filename(name: str) -> datetime | None:
    """A cron output file's leading ``YYYY-MM-DD_HH-MM-SS`` wall-clock stamp
    (the rest, e.g. a ``.md`` suffix, is ignored) as a naive datetime, or
    ``None`` when the name doesn't carry a parseable stamp."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})", name)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}_{match.group(2)}", "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _message_from_row(row: dict) -> Message:
    """`messages.tool_calls` is a JSON-encoded column on disk; ``Message``
    wants it already parsed (mirrors what `Message.from_json` expects from
    the old gateway API's already-decoded JSON body)."""
    data = dict(row)
    if data.get("tool_calls"):
        data["tool_calls"] = json.loads(data["tool_calls"])
    return Message.from_json(data)


@dataclass
class SessionsStore:
    executor: HermesExecutor
    profile: str

    async def list(
        self,
        *,
        source: str | None = None,
        search: str | None = None,
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[Session], int]:
        params = {
            "source": source,
            "search": f"%{search}%" if search else None,
            "limit": limit,
            "offset": offset,
        }
        rows = await self.executor.read_sqlite(
            "state.db", _SESSIONS_LIST_SQL, params, profile=self.profile
        )
        count_rows = await self.executor.read_sqlite(
            "state.db",
            _SESSIONS_COUNT_SQL,
            {"source": params["source"], "search": params["search"]},
            profile=self.profile,
        )
        total = count_rows[0]["n"] if count_rows else 0
        return [Session.from_json(row) for row in rows], total

    async def get_session(self, session_id: str) -> Session:
        """Find a session by ID, searching across all available profiles.

        Sessions from sources like ``cron``, ``kanban``, and
        ``api_server`` are stored in the root ``state.db``, not in
        profile-scoped DBs.  This method searches across all profiles to
        ensure root-level sessions are always accessible regardless of
        which profile is active in the UI.
        """
        # Try current profile first
        rows = await self.executor.read_sqlite(
            "state.db", _SESSION_ROW_SQL, (session_id,), profile=self.profile
        )
        if rows:
            return Session.from_json(rows[0])

        # Session not found in current profile - search the root profile
        if self.profile != "":
            rows = await self.executor.read_sqlite(
                "state.db", _SESSION_ROW_SQL, (session_id,), profile=""
            )
            if rows:
                return Session.from_json(rows[0])

        # Search other named profiles
        try:
            profiles = await self.executor.list_profile_names()
            for profile in profiles:
                if profile == self.profile or not profile:
                    continue
                rows = await self.executor.read_sqlite(
                    "state.db", _SESSION_ROW_SQL, (session_id,), profile=profile
                )
                if rows:
                    return Session.from_json(rows[0])
        except Exception:
            pass

        raise HermesError(f"session {session_id} not found")

    async def get_messages_page(
        self,
        session_id: str,
        *,
        before_id: int | None = None,
        limit: int = DEFAULT_MESSAGES_PAGE_SIZE,
    ) -> tuple[list[Message], bool]:
        """One page of messages, newest-first on the wire but returned in
        chronological order (what the transcript renders in) -- `before_id`
        is the smallest `id` already loaded, so the *next* call walks
        further back in history. `has_more=True` means an older page still
        exists (a "Load earlier" control has something to fetch).

        Searches across all profiles for the session's ``state.db`` so that
        root-level sessions (cron, kanban, api_server) are accessible even
        when browsing from a named profile.
        """
        # Find which profile's DB holds this session's messages
        profiles_to_try = [self.profile]
        if self.profile != "":
            profiles_to_try.append("")
        try:
            profiles_to_try.extend(await self.executor.list_profile_names())
        except Exception:
            pass

        rows: list[dict] = []
        for p in profiles_to_try:
            if rows:
                break
            rows = await self.executor.read_sqlite(
                "state.db",
                _SESSION_MESSAGES_PAGE_SQL,
                {"session_id": session_id, "before_id": before_id, "limit": limit + 1},
                profile=p,
            )
        if not rows:
            return [], False
        has_more = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        return [_message_from_row(row) for row in rows], has_more

    async def rename(self, session_id: str, title: str) -> None:
        await hermes_cli.rename_session(self.executor, self.profile, session_id, title)

    async def delete(self, session_id: str) -> None:
        await hermes_cli.delete_session(self.executor, self.profile, session_id)


@dataclass
class CronStore:
    executor: HermesExecutor
    profile: str

    async def list_jobs(
        self, *, limit: int | None = None, offset: int = 0
    ) -> tuple[list[Job], int]:
        """`cron/jobs.json` is one small file with no partial-read story of
        its own, so "offset/limit" here means slicing the fully-parsed list
        in Python rather than a database query -- same kwargs shape as
        `SessionsStore.list`/`KanbanStore.list_tasks` so every plugin list
        page's pagination looks the same from the UI side, even though only
        the SQL-backed stores can push the slicing down to the read itself.
        """
        data = await self.executor.read_json("cron/jobs.json", profile=self.profile)
        jobs = [Job.from_json(j) for j in (data or {}).get("jobs", [])]
        total = len(jobs)
        page = jobs[offset:] if limit is None else jobs[offset : offset + limit]
        return page, total

    async def get_job(self, job_id: str) -> Job:
        jobs, _total = await self.list_jobs(limit=None)
        for job in jobs:
            if job.id == job_id:
                return job
        raise HermesError(f"cron job {job_id} not found")

    async def list_runs(self, job_id: str, limit: int = 50) -> list[CronRun]:
        """Return the newest execution attempts for a cron job."""
        rows = await self.executor.read_sqlite(
            "cron/executions.db",
            "SELECT * FROM executions WHERE job_id = ? ORDER BY claimed_at DESC LIMIT ?",
            (job_id, limit),
            profile=self.profile,
        )
        return [CronRun.from_json(row) for row in rows]

    async def get_run(self, job_id: str, run_id: str) -> CronRun | None:
        """One execution attempt for `job_id`, or `None` if it doesn't
        exist (a stale run link from a re-run/delete job)."""
        rows = await self.executor.read_sqlite(
            "cron/executions.db",
            "SELECT * FROM executions WHERE job_id = ? AND id = ?",
            (job_id, run_id),
            profile=self.profile,
        )
        return CronRun.from_json(rows[0]) if rows else None

    async def find_run_output(self, job_id: str, run: CronRun) -> tuple[str, str] | None:
        """Locate and read the output markdown file for `run`, if one exists.

        The scheduler saves each run's output under ``cron/output/{job_id}/``
        named ``{timestamp}.md`` where ``timestamp`` is its *local* wall clock
        at save time (``YYYY-MM-DD_HH-MM-SS``) -- there's no run id in the
        filename, so the match is by proximity: parse each file's wall-clock
        stamp and pick the one nearest the run's ``finished_at`` (falling back
        to ``claimed_at``/``started_at``), but only when it's within
        ``_RUN_OUTPUT_TOLERANCE_S`` seconds -- a stale/unrelated file in the
        same directory must not be attributed to this run.

        Returns ``(filename, content)`` or ``None`` when no file is close
        enough or the directory is missing/empty (``no_agent``/silent runs
        legitimately never write one).
        """
        anchor = (
            _naive_wall_clock(run.finished_at)
            or _naive_wall_clock(run.claimed_at)
            or _naive_wall_clock(run.started_at)
        )
        if anchor is None:
            return None
        names = await self.executor.list_dir(f"cron/output/{job_id}", profile=self.profile)
        best: tuple[float, str] | None = None
        for name in names:
            stamp = _parse_output_filename(name)
            if stamp is None:
                continue
            delta = abs((stamp - anchor).total_seconds())
            if best is None or delta < best[0]:
                best = (delta, name)
        if best is None or best[0] > _RUN_OUTPUT_TOLERANCE_S:
            return None
        filename = best[1]
        content = await self.executor.read_text(
            f"cron/output/{job_id}/{filename}", profile=self.profile
        )
        if content is None:
            return None
        return filename, content

    async def find_related_session_id(self, job_id: str, run: CronRun) -> str | None:
        """Find the state.db session `run` produced, if any.

        The cron agent names its sessions ``cron_{job_id}_{%Y%m%d_%H%M%S}``
        (the run-start local wall clock), so the candidate set is a SQL prefix
        range scan -- ``source = 'cron'`` with id in ``[prefix, prefix_hi)``
        where ``prefix_hi`` bumps the prefix's last character. Pick the
        session whose ``started_at`` is nearest to the run's ``claimed_at``
        within ``_RUN_SESSION_TOLERANCE_S`` seconds; ``None`` when nothing is
        close enough (a no-agent run spawns no session).
        """
        prefix = f"cron_{job_id}_"
        prefix_hi = prefix[:-1] + chr(ord(prefix[-1]) + 1)
        rows = await self.executor.read_sqlite(
            "state.db",
            "SELECT id, started_at FROM sessions WHERE source = ? AND id >= ? AND id < ?",
            ("cron", prefix, prefix_hi),
            profile=self.profile,
        )
        anchor = _naive_wall_clock(run.claimed_at)
        if anchor is None:
            return None
        best: tuple[float, str] | None = None
        for row in rows:
            started = _naive_wall_clock(row.get("started_at"))
            if started is None:
                continue
            delta = abs((started - anchor).total_seconds())
            if best is None or delta < best[0]:
                best = (delta, row["id"])
        if best is None or best[0] > _RUN_SESSION_TOLERANCE_S:
            return None
        return best[1]

    async def create(
        self,
        *,
        schedule: str,
        prompt: str = "",
        name: str | None = None,
        deliver: str | None = None,
        skills: list[str] | None = None,
        repeat: int | None = None,
    ) -> None:
        await hermes_cli.create_job(
            self.executor,
            self.profile,
            schedule=schedule,
            prompt=prompt,
            name=name,
            deliver=deliver,
            skills=skills,
            repeat=repeat,
        )

    async def save_fields(self, job_id: str, fields: dict) -> Job:
        """Apply `fields` (whatever subset of name/schedule/prompt/deliver/
        skills/repeat is present -- the cron detail page's form and
        raw-YAML tabs both funnel through here) via `hermes cron edit`, then
        re-read the job so the caller sees the daemon's own resulting
        state."""
        await hermes_cli.edit_job(
            self.executor,
            self.profile,
            job_id,
            name=fields.get("name"),
            schedule=fields.get("schedule"),
            prompt=fields.get("prompt"),
            deliver=fields.get("deliver"),
            skills=fields.get("skills"),
            repeat=fields.get("repeat"),
        )
        return await self.get_job(job_id)

    async def pause(self, job_id: str) -> Job:
        await hermes_cli.pause_job(self.executor, self.profile, job_id)
        return await self.get_job(job_id)

    async def resume(self, job_id: str) -> Job:
        await hermes_cli.resume_job(self.executor, self.profile, job_id)
        return await self.get_job(job_id)

    async def delete(self, job_id: str) -> None:
        await hermes_cli.delete_job(self.executor, self.profile, job_id)

    async def run(self, job_id: str) -> Job:
        await hermes_cli.run_job(self.executor, self.profile, job_id)
        return await self.get_job(job_id)


_KANBAN_TASK_COLUMNS = """
    t.*,
    (SELECT COUNT(*) FROM task_comments c WHERE c.task_id = t.id) AS comment_count,
    (SELECT r.summary FROM task_runs r
     WHERE r.task_id = t.id AND r.summary IS NOT NULL
     ORDER BY r.id DESC LIMIT 1) AS latest_summary
"""
_KANBAN_STATUS_RE = re.compile(r"^[a-z0-9_]+$")


def _kanban_escape_like(value: str) -> str:
    """Escape `%` and `_` in a user's search text so they match literally
    instead of acting as `LIKE` wildcards (the caller wraps the escaped text
    in `%...%` for a substring match)."""
    return value.replace("%", "\\%").replace("_", "\\_")


def _kanban_where(search: str | None) -> str:
    """The optional `LIKE` predicate shared by `_kanban_list_sql` and
    `_kanban_count_sql` so a filtered page's `total` always matches the rows
    it returns: a case-insensitive substring match against a task's title,
    body, assignee, id, or comment body. Empty when no search is active, so the rest of the
    query is unchanged. `:search` is a named param holding the caller's
    already-escaped `%...%` pattern."""
    if not search:
        return ""
    return (
        " AND (t.title LIKE :search ESCAPE '\\'"
        " OR t.body LIKE :search ESCAPE '\\'"
        " OR t.assignee LIKE :search ESCAPE '\\'"
        " OR t.id LIKE :search ESCAPE '\\'"
        " OR EXISTS (SELECT 1 FROM task_comments c"
        " WHERE c.task_id = t.id AND c.body LIKE :search ESCAPE '\\'))"
    )


def _kanban_list_sql(status_order: list[str] | None = None, search: str | None = None) -> str:
    if status_order is None:
        order_by = "t.created_at DESC"
    else:
        for status in status_order:
            if not _KANBAN_STATUS_RE.fullmatch(status):
                raise ValueError(f"invalid kanban status: {status!r}")
        cases = " ".join(
            f"WHEN t.status = '{status}' THEN {index}" for index, status in enumerate(status_order)
        )
        order_by = f"CASE {cases} ELSE {len(status_order)} END, t.created_at DESC"
    return f"""
SELECT {_KANBAN_TASK_COLUMNS} FROM tasks t
WHERE (:status IS NULL OR t.status = :status)
{_kanban_where(search)}
ORDER BY {order_by}
LIMIT :limit OFFSET :offset
"""


def _kanban_count_sql(search: str | None = None) -> str:
    """`COUNT(*)` twin of `_kanban_list_sql` -- the same status/search WHERE
    clauses, so pagination totals track the visible (filtered) set."""
    return (
        "SELECT COUNT(*) AS n FROM tasks t WHERE (:status IS NULL OR t.status = :status)"
        f"{_kanban_where(search)}"
    )


_KANBAN_LIST_SQL = _kanban_list_sql()
_KANBAN_TASK_SQL = f"SELECT {_KANBAN_TASK_COLUMNS} FROM tasks t WHERE t.id = :task_id"
_KANBAN_COMMENTS_SQL = "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id"
_KANBAN_RUNS_SQL = "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id"

# The dispatcher tags every worker session `source='kanban'` and titles it
# "work kanban task <task_id>" (with a " #N" suffix when a task is retried,
# one session per run), so the session *doing the work* for a task is
# findable in the assignee's state.db even though `tasks.session_id` only
# records the *spawning* session. Newest first: for a retried task the
# latest session is the current run.
_KANBAN_WORKER_SESSION_SQL = """
SELECT id FROM sessions
WHERE source = 'kanban' AND title LIKE :pattern
ORDER BY last_activity_at DESC
LIMIT 1
"""


@dataclass
class KanbanStore:
    """Kanban's board is daemon-global, not profile-scoped (confirmed
    against the real host: `kanban.db` lives at the daemon's root, not under
    any `profiles/<name>/`), so unlike `SessionsStore`/`CronStore` this
    takes no `profile` -- reads always go to `profile=""` (the root).

    Reads go straight at `kanban.db`; writes delegate to
    `HermesExecutor`'s dashboard-REST methods -- kanban's CLI (`hermes
    kanban ...`) is a task-lifecycle tool (block/unblock/promote/
    complete/...), not a generic field-setter, so title/body/priority
    edits and arbitrary status moves can't go through the CLI the way
    cron's/sessions' writes do. See `HermesExecutor`'s "kanban writes"
    section.
    """

    executor: HermesExecutor

    async def create(
        self,
        *,
        title: str,
        body: str = "",
        assignee: str = "default",
        priority: int = 2,
        status: str | None = None,
        skills: list[str] | None = None,
    ) -> Task:
        return await self.executor.create_kanban_task(
            title=title, body=body, assignee=assignee, priority=priority, status=status,
            skills=skills,
        )

    async def save_fields(self, task_id: str, fields: dict[str, Any]) -> Task:
        return await self.executor.update_kanban_task(task_id, fields)

    async def move_status(self, task_id: str, status: str) -> Task:
        return await self.executor.update_kanban_task(task_id, {"status": status})

    async def delete(self, task_id: str) -> None:
        await self.executor.delete_kanban_task(task_id)

    async def add_comment(self, task_id: str, body: str) -> None:
        await self.executor.add_kanban_comment(task_id, body)

    async def list_tasks(
        self,
        *,
        status: str | None = None,
        search: str | None = None,
        limit: int = 30,
        offset: int = 0,
        status_order: list[str] | None = None,
    ) -> tuple[list[Task], int]:
        """`status=None` ("all") lists across every status; otherwise scoped
        to just that one -- `plugins/kanban/ui.py`'s status tabs pass their
        own filter through here rather than fetching everything and
        re-filtering client-side, so a status tab's own page count reflects
        only tasks in that status. When `status_order` is provided, status
        groups are ordered as given, unknown statuses come last, and each
        group is ordered by created_at descending (newest first).

        `search` narrows the same set further: a case-insensitive substring
        match against title, body, assignee, or task id (`%`/`_` in the input
        are escaped so they match literally), combining with `status` rather
        than replacing it. The returned `total` counts only the filtered set,
        so a pager stays in sync with the visible rows."""
        search = search.strip() if search else None
        params = {
            "status": status,
            "search": f"%{_kanban_escape_like(search)}%" if search else None,
            "limit": limit,
            "offset": offset,
        }
        rows = await self.executor.read_sqlite(
            "kanban.db", _kanban_list_sql(status_order, search), params
        )
        count_rows = await self.executor.read_sqlite(
            "kanban.db", _kanban_count_sql(search), {"status": status, "search": params["search"]}
        )
        total = count_rows[0]["n"] if count_rows else 0
        return [Task.from_json(row) for row in rows], total

    async def get_task_detail(self, task_id: str) -> TaskDetail:
        rows = await self.executor.read_sqlite("kanban.db", _KANBAN_TASK_SQL, {"task_id": task_id})
        if not rows:
            raise HermesError(f"task {task_id} not found")
        comment_rows = await self.executor.read_sqlite(
            "kanban.db", _KANBAN_COMMENTS_SQL, (task_id,)
        )
        run_rows = await self.executor.read_sqlite("kanban.db", _KANBAN_RUNS_SQL, (task_id,))
        return TaskDetail(
            task=Task.from_json(rows[0]),
            comments=[Comment.from_json(row) for row in comment_rows],
            runs=run_rows,
            worker_session_id=await self._resolve_worker_session(task_id, rows[0]),
        )

    async def _resolve_worker_session(self, task_id: str, task_row: dict) -> str | None:
        """The kanban-tagged worker session doing the work for `task_id`.

        The worker session lives in the *assignee's* profile state.db (the
        dispatcher spawns ``hermes -p <assignee>``), not the daemon root the
        kanban read path otherwise uses, so the lookup targets
        ``profile_home(assignee)`` -- which for the ``default`` profile is
        the same root ``state.db`` the board itself lives next to. Newest
        first: a retried task gets one session per run and the latest one is
        the current run. Returns None when the task has never been claimed
        (no worker session exists); ``read_sqlite`` returns ``[]`` for a
        missing state.db, so an unknown profile can't raise here.
        """
        pattern = f"work kanban task {task_id}%"
        rows = await self.executor.read_sqlite(
            "state.db",
            _KANBAN_WORKER_SESSION_SQL,
            {"pattern": pattern},
            profile="",
        )
        return rows[0]["id"] if rows else None


@dataclass
class HermesStore:
    sessions: SessionsStore
    cron: CronStore
    kanban: KanbanStore


def build_store(executor: HermesExecutor, profile: str) -> HermesStore:
    return HermesStore(
        sessions=SessionsStore(executor, profile),
        cron=CronStore(executor, profile),
        kanban=KanbanStore(executor),
    )
