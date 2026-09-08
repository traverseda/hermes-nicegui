# hermes-nicegui

A modular, plugin-based [NiceGUI](https://nicegui.io) web UI for
[Hermes Agent](https://github.com/NousResearch/hermes-agent), with a profile
switcher: pick which Hermes profile (a separate isolated agent identity/state
— `default`, or any named profile under `~/.hermes/profiles/`) a browser tab
is looking at, and sessions/cron/chat all follow that choice.

Sessions and cron reach Hermes by running the **`hermes` CLI itself** as a
subprocess (`hermes_nicegui.executor.HermesExecutor`), either locally (this
app colocated with the real Hermes install) or over SSH — never a bearer
token. Kanban talks to the Hermes CLI's own dashboard web server (cookie
login) instead, since its board has no CLI equivalent. The UI itself is a
plugin host: self-contained modules (sessions, cron, …) each expose a
`register()` hook and are discovered through `importlib.metadata` entry
points.

## Stack

- [NiceGUI](https://nicegui.io) 3.x — UI framework
- [httpx](https://www.python-httpx.org/) — async client for the dashboard (kanban/cron reads)
- `asyncio.create_subprocess_exec` — runs the `hermes` CLI (sessions, cron mutations, chat)
- [loguru](https://loguru.readthedocs.io/) — logging
- [uv](https://docs.astral.sh/uv/) — packaging / env management
- pytest + NiceGUI `user` fixture — fast, browserless UI tests

## Quick start

```bash
cp .env.example .env      # see Configuration below
uv sync                   # install deps + the package (editable)
uv run hermes-nicegui     # or: uv run python main.py
```

Then open http://127.0.0.1:8080. With no configuration at all, sessions/cron/
chat work against whatever `hermes` binary is on `PATH` and its `default`
profile — the common case is running this app on the same host as the real
Hermes install. Kanban needs `HERMES_KANBAN_URL`/`_USERNAME`/`_PASSWORD` set.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `HERMES_EXEC_MODE` | `local` | `local` runs `hermes` as a subprocess here; `ssh` runs it over `ssh -tt <HERMES_SSH_TARGET>` instead |
| `HERMES_CLI_BIN` | `hermes` | Path to the `hermes` binary (or command name on `PATH`) |
| `HERMES_SSH_TARGET` | *(none)* | `user@host` for `HERMES_EXEC_MODE=ssh` |
| `HERMES_SSH_OPTIONS` | *(none)* | Extra space-separated `ssh` args, e.g. `-o BatchMode=yes` |
| `HERMES_KANBAN_URL` | `http://127.0.0.1:9119` | Hermes dashboard base URL (kanban plugin) |
| `HERMES_KANBAN_USERNAME` | *(required for kanban)* | Dashboard login username |
| `HERMES_KANBAN_PASSWORD` | *(required for kanban)* | Dashboard login password |
| `HERMES_UI_HOST` | `0.0.0.0` | NiceGUI bind host |
| `HERMES_UI_PORT` | `8080` | NiceGUI bind port |
| `HERMES_UI_DARK` | `false` | Dark mode UI |
| `HERMES_AUTH_ENABLED` | `true` | Gate the whole app behind local username/password login |
| `HERMES_DATA_DIR` | `~/.local/share/hermes-nicegui` | Holds the admin account (`users.db`) and the session-encryption secret |
| `HERMES_PLUGINS_DISABLED` | *(none)* | Comma-separated plugin names to skip |
| `HERMES_LOG_LEVEL` | `INFO` | loguru level |
| `HERMES_FILES_ROOT` | `.` (cwd) | Directory the files plugin is confined to |
| `HERMES_FILES_EDIT_MAX_BYTES` | `2097152` | Max file size the editor will load (larger files are download-only) |
| `HERMES_CHAT_UPLOADS_DIR` | `<HERMES_HOME>/uploads` | Where chat attachments are saved (must be a path the agent on this box can read) |
| `HERMES_CHAT_UPLOAD_MAX_BYTES` | `26214400` | Per-file cap for chat attachments (bytes) |
| `HERMES_XAELWIKI_NOTES_DIR` | `/var/lib/xaelwiki/notes` | Directory of the xaelwiki notes vault (a git repo of markdown files with YAML frontmatter) the xaelwiki plugin browses read-only |
| `HERMES_XAELWIKI_REFRESH_SECONDS` | `30` | How often the notes list re-scans the vault (also the store's cache TTL) |
| `HERMES_COSTS_ENV_FILE` | `/run/agenix/hermes-env` | `KEY=value` file used by the costs plugin for provider credentials when they are not in the process environment |
| `HERMES_OPENCODE_DB` | `~/.local/share/opencode/opencode-stable.db` | OpenCode SQLite database used for local session spend |

`HERMES_GATEWAY_URL`/`HERMES_API_TOKEN`/`HERMES_DEFAULT_MODEL`/
`HERMES_DEFAULT_PROVIDER` still exist (`hermes_nicegui.gateway.HermesClient`)
but nothing in the UI uses them anymore — sessions/cron/chat reach Hermes
through the CLI instead. Left in place rather than ripped out in case a
direct gateway integration is useful again later.

> **Profiles need the CLI, not a token:** the profile switcher lists
> whatever `hermes profile list` reports and re-runs every sessions/cron/
> chat command with `-p <profile>`. There's no per-profile secret to
> configure — the same `hermes` install already knows about every profile's
> own state.

> **Kanban is a different server:** the kanban board lives on the Hermes
> CLI's own dashboard web server (`HERMES_KANBAN_URL`), not the gateway, and
> it authenticates with a username/password login (cookie session), not a
> bearer token or the CLI. It isn't scoped by the profile switcher — a
> task's `assignee` is itself a profile name, and the board shows every
> profile's tasks at once. Creating a task always starts it in the `ready`
> column — the dashboard's live dispatcher can claim it and spawn a real
> agent run within about a minute.

> **Files gives the browser real filesystem access:** the files plugin reads,
> writes, and lets anyone who can log in download `HERMES_FILES_ROOT`
> (default: the process's own working directory) — same exposure as the
> terminal plugin. The app *is* gated by login now (below), but that's still
> only one account behind one password; point `HERMES_FILES_ROOT` at
> something narrower than a whole home directory, and don't bind
> `HERMES_UI_HOST` past `127.0.0.1` unless something else is guarding access
> too.

> **Chat attachments are local paths, not uploads to the gateway:** the
> session chat's paperclip saves picked files under `HERMES_CHAT_UPLOADS_DIR`
> (default `<HERMES_HOME>/uploads`, per-session subdirectory) and the sent
> message carries an `Attached files:` block listing each absolute path —
> the same path-reference convention every other Hermes surface uses for
> inbound media. This only works when the agent runs on the same box (the
> deploy setup): a remote `HERMES_EXEC_MODE=ssh` gateway can't see these
> local paths. Attachments are not sent through the gateway API itself — its
> chat endpoint rejects `file` content parts.

> **Auth is one local admin account, no more:** first visit to any page
> shows a "create admin account" form (`hermes_nicegui.auth`, backed by a
> small SQLite file under `HERMES_DATA_DIR` — stdlib `hashlib.scrypt`
> password hashing, no extra dependency); every request after that is
> gated by `AuthMiddleware` until you log in. There's no user-management UI
> (add/remove/reset other accounts) — this is one shared admin login, not
> multi-tenant. `HERMES_DATA_DIR` also holds the generated session-encryption
> secret (`storage_secret`), created once and reused on every restart so
> logins survive one — deleting it (or the whole data dir) invalidates every
> active session, not just its own. Set `HERMES_AUTH_ENABLED=false` to skip
> login entirely (e.g. an already-firewalled deployment) — the login page
> still exists, it's just not enforced.

## Architecture

Two layers, and plugins are only allowed to depend on the outer one:

- **`hermes_nicegui.app`** — the process entrypoint. Reads `Settings()` from
  the environment, builds the real `HermesClient` and `HermesExecutor`,
  fetches the profile list (`hermes profile list`, via `hermes_cli`), calls
  `ui.run()`. Nothing else imports this module.
- **`hermes_nicegui.web`** — the UI shell: the shared `frame()` (header + nav
  drawer + profile switcher), the process-wide `AppState` (nav items,
  settings, executor, profile list), `current_profile()`/`current_executor()`
  (read the active profile / executor for whichever browser tab is calling,
  via NiceGUI's own per-connection `app.storage.client` — already
  contextvar-backed, so no extra parameter threading is needed anywhere a
  page or click handler needs "the current profile"), and
  `build(context, plugins=None, profiles=None)`, which registers the home
  page and every plugin's pages. `plugins=None` discovers the installed set
  through the `hermes_nicegui.plugins` entry-point group (the production
  path); an explicit list registers exactly those plugin instances (what the
  test suite does).
- **`hermes_nicegui.executor.HermesExecutor`** — runs the `hermes` CLI,
  local or over SSH; the same argv-building path serves one-shot commands
  and the interactive chat pty alike (`ssh -tt` allocates a pty either way).
- **`hermes_nicegui.hermes_cli`** — profile-scoped `hermes` CLI commands
  (session export/rename/delete, cron mutations, profile list), run through
  a `HermesExecutor`. Pure parsing (table/JSONL) lives in plain functions
  here, unit-tested without a subprocess.
- **`hermes_nicegui.dashboard_auth.DashboardSession`** — shared cookie-login
  HTTP primitive for the Hermes dashboard web server; `kanban` and `cron`'s
  read paths each build their own client on top of it (different route
  trees, same auth).
- **`hermes_nicegui.auth`** — local username/password auth for the app
  itself (distinct from `dashboard_auth`, which is this app authenticating
  *to* Hermes's dashboard, not a user authenticating to this app):
  `UserStore` (SQLite, `hashlib.scrypt` hashing), `load_or_create_secret`
  (the persisted `storage_secret`), and `AuthMiddleware` (deny-by-default,
  registered in `app.py::main` *before* `ui.run()` — see the module
  docstring for why the order matters). The `/login` page itself lives in
  `web.py` next to `frame()`/`_register_home()`, since it needs `AppState`
  but isn't plugin-owned.

Plugins (`src/hermes_nicegui/plugins/`) import from `hermes_nicegui.plugin`
(the `Plugin`/`PluginContext`/`NavItem` types) and `hermes_nicegui.web`
(`frame`), never from `hermes_nicegui.app` and never from each other. That
boundary is what makes it possible to edit one plugin without risking another,
and it's what would let a plugin move out into its own installable package
later with no import changes.

A plugin is any Python package exposing a `plugin` entry-point value that
implements the `Plugin` protocol from `hermes_nicegui.plugin`:

- `name`, `title`, `icon` — identity + nav metadata
- `nav_items() -> list[NavItem]` — entries shown in the left drawer
- `register() -> None` — called once at startup to add pages

`PluginContext` carries `settings`, the `loguru` logger, the shared
`HermesExecutor`, and the (now unused by any built-in plugin, kept for
compatibility) `HermesClient | None`, and is passed to `Plugin.__init__`.
Built-in plugins live in `src/hermes_nicegui/plugins/` and are wired through
`[project.entry-points."hermes_nicegui.plugins"]`. A third party plugin can be
installed with `uv add` and is discovered automatically.

Current plugins:

- `sessions` — session list and read-only transcript, scoped to the active
  profile (`hermes sessions export --format jsonl`, `rename`, `delete`); a
  "New chat" / "Continue in chat" action opens `/sessions/chat[/{id}]`, a
  browser terminal running `hermes -p <profile> chat [--resume <id>]` in a
  pty (same pty-per-page-load pattern as `terminal`, just a different
  command)
- `terminal` — a browser terminal (`ui.xterm`) backed by a real local shell,
  forked into its own pty per page load and torn down when the tab
  disconnects
- `cron` — scheduled job list/detail/edit, scoped to the active profile.
  Listing/get/update read from the dashboard (`CronDashboardClient`);
  pause/resume/run/remove/create run through the `hermes` CLI instead (no
  structured response worth parsing, so the UI re-fetches via the dashboard
  client afterwards to refresh)
- `kanban` — a multi-column board (triage/todo/scheduled/ready/running/
  blocked/review/done) over the Hermes dashboard's kanban task API: create
  tasks, move them between columns, comment, delete, and trigger a
  dispatcher tick. Talks to the dashboard's own web server, not the gateway
  — see the kanban gotcha above
- `files` — a file browser/editor over `HERMES_FILES_ROOT`: CodeMirror for
  text files (with syntax highlighting by extension), inline preview for
  images/PDF/audio/video, raw download for everything else, a streaming
  `.tar.zst` download of any directory, and file or whole-folder uploads
  (folder uploads keep their relative paths — see the files gotcha above)
- `xaelwiki` — read-only browser/search over the xaelwiki notes vault (a git
  repo of markdown files with YAML frontmatter). It reads the vault straight
  from disk (`HERMES_XAELWIKI_NOTES_DIR`, default `/var/lib/xaelwiki/notes`)
  — no MCP server, no token. `/xaelwiki` lists every note newest-updated
  first with a real-time search box (title/tags/body) and a folder filter;
  `/xaelwiki/{note_id}` shows the full note. The process user must be able to
  read the vault; on the deploy box the hermes user is in the xaelwiki group
  for exactly this.
- `costs` — provider spend summaries and usage tables for OpenRouter and the
  OpenCode Go plan. OpenCode Go budget is server-reported from the Go plan
  usage API (`GET https://opencode.ai/zen/go/v1/usage` with an
  `OPENCODE_API_KEY`/`OPENCODE_GO_API_KEY` from the environment or
  `HERMES_COSTS_ENV_FILE`), with a fallback to estimating from the local
  OpenCode SQLite database when the API is unavailable. OpenRouter
  credentials can come from `OPENROUTER_API_KEY` or `HERMES_COSTS_ENV_FILE`;
  unavailable providers are shown without preventing the rest of the page
  from loading.

### Adding the xaelwiki plugin to another NiceGUI app

The plugin is a plain `Plugin` subclass, so any existing NiceGUI app can host
it with one import + registration call (do this before `ui.run()` or inside
an `app.on_startup` handler, like the built-in plugins do):

```python
from loguru import logger

from hermes_nicegui.config import Settings
from hermes_nicegui.executor import HermesExecutor
from hermes_nicegui.plugin import PluginContext
from hermes_nicegui.plugins.xaelwiki import XaelWikiPlugin

context = PluginContext(
    settings=Settings(),                  # reads HERMES_XAELWIKI_* env vars
    logger=logger,
    executor=HermesExecutor(mode="local", hermes_bin="hermes"),
)
XaelWikiPlugin(context, notes_dir="/srv/notes", refresh_seconds=30).register()
```

This adds `/xaelwiki` (list + search) and `/xaelwiki/{note_id}` (detail) to
the app. `notes_dir`/`refresh_seconds` override the `HERMES_XAELWIKI_*`
environment defaults; omit them to use the defaults (the deploy-box vault).

## Testing

```bash
uv run pytest
```

Tests use NiceGUI's `user` fixture (a simulated in-process browser), so they
are fast and need no Selenium. There is no `main.py` involved
(`main_file = ""` in `pyproject.toml`): each test builds a `PluginContext` via
the `context`/`make_context` fixtures (`tests/conftest.py`) — settings
constructed directly, no environment variables, no network — and calls
`hermes_nicegui.web.build(context, [SomePlugin(context)])` itself, so it
registers exactly the plugin it's testing. `tests/test_plugin_discovery.py`
is the one test that instead calls `web.build(context)` with no explicit
list, checking that entry-point discovery itself still works.

Backends are faked at the same boundary the real code swaps out:
`HermesClient`/`KanbanClient`/`CronDashboardClient` take an injectable httpx
`transport` (`fake_kanban`/`fake_cron_dashboard` + `httpx.MockTransport`);
`HermesExecutor` takes an injectable `run_fn` in place of a real subprocess
(`fake_hermes_cli`/`executor`) — no real process or network access in a test
run. `make_context` does point `HERMES_DATA_DIR` at a fixture-managed temp
directory (`AppState.user_store` opens a real, small SQLite file there) —
otherwise every test run would read/write the real default
`~/.local/share/hermes-nicegui`.

`AuthMiddleware` must be registered *before* `ui.run()` (see
`hermes_nicegui/auth.py`), which the `user` fixture already calls during its
own setup — so `tests/test_auth.py`'s middleware tests don't use that
fixture. They instead replicate `nicegui.testing.user_simulation`'s own
internals directly, with `app.add_middleware(AuthMiddleware)` slotted in
before `ui.run()`. The `/login` page's own behavior (first-run form, wrong
password, successful login) is tested separately through the normal
`context`/`user` fixtures, since it works identically whether or not the
middleware is installed.

## Type checking and linting

```bash
uv run pyright   # type checking
uv run ruff check .   # lint
uv run ruff format .  # format
```

`pyrightconfig.json` pins the interpreter to the project venv and the include
paths to `src`, `main.py`, and `tests`. If your editor's LSP shows unresolved
imports, point it at `.venv/bin/python`.

## Performance notes

- All dashboard I/O is async (`httpx.AsyncClient`); the event loop is never
  blocked by network calls.
- `hermes` CLI calls run via `asyncio.create_subprocess_exec`, never
  `subprocess.run`/`os.system` — same rule, no blocking the event loop.
- SSE streaming (the still-present, currently-unused `HermesClient`) is
  consumed incrementally (`aiter_lines`).
- Use `ui.timer` / `asyncio` for background refresh — never `time.sleep` in a
  page handler.
