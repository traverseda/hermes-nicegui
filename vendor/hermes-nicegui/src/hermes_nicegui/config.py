"""Application settings loaded from environment / .env."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the Hermes NiceGUI app.

    All values can be overridden through environment variables with the
    ``HERMES_`` prefix or a ``.env`` file at the project root.
    """

    model_config = SettingsConfigDict(
        env_prefix="HERMES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gateway_url: str = "http://127.0.0.1:8443"
    api_token: str = ""

    # The kanban plugin talks to a *different* server than the gateway above:
    # the Hermes CLI's own dashboard web server, which uses cookie/password
    # auth rather than a bearer token. See `HermesExecutor`'s kanban-writes
    # methods.
    kanban_url: str = ""
    kanban_username: str = ""
    kanban_password: str = ""

    ui_host: str = "0.0.0.0"
    ui_port: int = 8080
    ui_dark: bool = False
    ui_reload: bool = True

    default_model: str = "hermes-agent"
    default_provider: str = ""

    # How `sessions`/`cron` reach the `hermes` CLI for profile-scoped
    # commands (session export, chat, cron mutations): "local" runs it as a
    # subprocess on this host; "ssh" runs the same argv over `ssh
    # <ssh_target>` (only the interactive chat pty adds `-tt`; a forced
    # remote pty on a one-shot command hangs indefinitely). See
    # `hermes_nicegui/executor.py`.
    exec_mode: Literal["local", "ssh"] = "local"
    cli_bin: str = "hermes"
    ssh_target: str = ""
    ssh_options: str = ""

    # The interpreter `HermesExecutor`'s ssh read path (`read_sqlite`/
    # `read_json`/...) runs on the remote host. Bare `python3` works on most
    # hosts, but not all -- e.g. a NixOS host's non-interactive PATH may have
    # no `python3` at all (only `hermes` itself, via a wrapper bundling its
    # own interpreter); point this at an absolute path in that case (see
    # `HermesExecutor`'s "kanban writes"-adjacent read-path docs).
    ssh_python_bin: str = "python3"

    # Where the daemon keeps its data (state.db, cron/jobs.json, kanban.db,
    # profiles/<name>/...) -- read directly (see `HermesExecutor.read_sqlite`/
    # `read_json`) rather than through the CLI. `~` is expanded locally for
    # "local" exec_mode, remotely (by the ssh-side script) for "ssh".
    hermes_home: str = "~/.hermes"

    plugins_disabled: str = ""

    log_level: str = "INFO"

    # Local username/password auth (see `hermes_nicegui/auth.py`). Disabling
    # is an escape hatch for an already-firewalled deployment -- the login
    # page and user store still exist either way, just ungated.
    auth_enabled: bool = True

    # Holds `users.db` (the admin account) and `storage_secret` (NiceGUI
    # session-cookie encryption key, generated once and persisted).
    data_dir: str = "~/.local/share/hermes-nicegui"

    # The `files` plugin gives the browser read/write access to everything
    # under this directory -- confining it here (default: the process's own
    # cwd, not `/`) is the only thing standing between that plugin and the
    # whole filesystem.
    files_root: str = "."
    files_edit_max_bytes: int = 2 * 1024 * 1024

    # The `sessions` chat's file attachments are saved to this directory on
    # this host: the agent runs on the same box, so a path the UI can write
    # is a path the agent's own file tools can read. Empty -> `<hermes_home>`
    # `/uploads`, which on the deploy box is inside the agent's home and in
    # this service's ReadWritePaths. Per-file cap in bytes (default 25 MiB,
    # matching the gateway's own media cap).
    chat_uploads_dir: str = ""
    chat_upload_max_bytes: int = 25 * 1024 * 1024

    # The `xaelwiki` plugin browses notes straight from the vault on disk: a
    # git repo of markdown files with YAML frontmatter. Read-only -- the
    # plugin never writes and never talks to the xaelwiki MCP server. Point
    # this at the xaelwiki service's notes directory; the process user must
    # be able to read it (on the deploy box the hermes user is added to the
    # xaelwiki group for exactly this).
    xaelwiki_notes_dir: str = "/var/lib/xaelwiki/notes"
    # How often the list re-scans the vault (also the store's cache TTL).
    xaelwiki_refresh_seconds: int = 30

    # The `vnc` plugin shows the operator the Xvfb display a sandboxed browser
    # bot drives (via noVNC), so a human can click/type when the bot hits a
    # captcha/human-wall. A companion systemd service runs Xvfb :99 + x11vnc
    # on `vnc_host:vnc_port` with a per-start password at `vnc_password_file`
    # and writes help-requests to `vnc_state_dir`/state.json. `vnc_novnc_dir`
    # is the noVNC package's install root (nixpkgs: <store>/share/webapps/
    # novnc); when empty, the page renders a "client assets missing" notice
    # instead of a live canvas.
    vnc_novnc_dir: str = ""
    vnc_state_dir: str = "/var/lib/hermes-vnc"
    vnc_password_file: str = "/run/hermes-vnc/passwd"
    vnc_host: str = "127.0.0.1"
    vnc_port: int = 5901

    # The costs plugin reads provider API keys from this "KEY=value" env file
    # when they are not in the process environment.
    costs_env_file: str = "/run/agenix/hermes-env"
    # OpenCode's local per-session spend database.
    opencode_db: str = "~/.local/share/opencode/opencode-stable.db"
    # OpenCode Go plan usage limits in USD-equivalent usage per window.
    opencode_go_monthly_limit: float = 60.0
    opencode_go_weekly_limit: float = 30.0
    opencode_go_5h_limit: float = 12.0

    @property
    def disabled_plugins(self) -> list[str]:
        """Plugin names to skip at startup, split on commas."""
        return [name.strip() for name in self.plugins_disabled.split(",") if name.strip()]

    @property
    def ssh_options_list(self) -> list[str]:
        """Extra `ssh` args (e.g. `-o BatchMode=yes`), split on whitespace."""
        return self.ssh_options.split()

    @property
    def files_root_path(self) -> Path:
        return Path(self.files_root).expanduser().resolve()

    @property
    def chat_uploads_dir_path(self) -> Path:
        """Where chat attachments are stored; defaults under ``hermes_home``.

        ``hermes_home`` is the daemon's own directory (state.db and friends),
        so the default keeps uploaded files next to the data they belong to
        and inside the same writable area the service already has.
        """
        configured = self.chat_uploads_dir.strip()
        if configured:
            return Path(configured).expanduser().resolve()
        return Path(self.hermes_home).expanduser().resolve() / "uploads"

    @property
    def data_dir_path(self) -> Path:
        return Path(self.data_dir).expanduser().resolve()

    def client_kwargs(self) -> dict:
        """Keyword arguments for the Hermes gateway client."""
        return {
            "base_url": self.gateway_url,
            "token": self.api_token,
            "default_model": self.default_model,
            "default_provider": self.default_provider,
        }
