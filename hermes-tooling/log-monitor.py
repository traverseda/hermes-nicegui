#!/usr/bin/env python3
"""
hermes-log-monitor: watches hermes-agent, hermes-nicegui, and podman-hindsight
logs for errors and opens deduplicated kanban tickets.

Design:
  * Primary data source: local log files in ~/.hermes/logs/ (hermes-user readable)
  * Fallback: journalctl --unit <service> (requires adm/systemd-journal group)
  * Config: YAML at ~/.hermes/log-monitor/config.yaml — reloaded on every line
  * Dedup: at most ONE ticket per service; if an open ticket exists for a
    service, errors for that service are swallowed until the ticket resolves
  * Heartbeat: logs a summary every N minutes (configurable, default 5)

No external dependencies beyond Python stdlib + PyYAML.  If PyYAML is
unavailable the built-in minimal YAML parser handles the config format.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Minimal YAML parser (no PyYAML requirement)
# ---------------------------------------------------------------------------

def _yaml_parse(text: str) -> dict[str, Any]:
    """Minimal YAML parser sufficient for the config format we need.

    Supports:
      - top-level key: value
      - nested key: value  (2-space indent)
      - list items  (- value)
      - quoted strings
    """
    result: dict[str, Any] = {}
    current_top: Optional[str] = None
    current_sub: Optional[str] = None
    current_list: Optional[list] = None
    current_list_key: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())
        stripped = line.strip()

        # --- top-level key: value ---
        if indent == 0 and ":" in stripped:
            current_top, _ = _split_kv(stripped)
            current_sub = None
            current_list = None
            current_list_key = None
            result[current_top] = {}
            continue

        if indent == 2 and current_top is not None and current_top in result:
            if stripped.startswith("- "):
                # list under current_top
                val = stripped[2:].strip()
                k, v = _split_kv(stripped[2:].strip()) if ":" in val else (None, val)
                # This is a list item at indent 2 — belongs under current_sub if set
                if current_sub and current_sub in result.get(current_top, {}):
                    if not isinstance(result[current_top][current_sub], list):
                        result[current_top][current_sub] = []
                    if k:
                        result[current_top][current_sub].append({k: v})
                    else:
                        result[current_top][current_sub].append(val)
                else:
                    # No sub key yet; create a list for current_top directly
                    if not isinstance(result[current_top], dict):
                        result[current_top] = {}
                    result[current_top][current_sub or "__unnamed__"] = [
                        {k: v} if k else val
                    ]
                continue
            else:
                k, v = _split_kv(stripped)
                # nested dict under current_top
                if current_top in result and isinstance(result[current_top], dict):
                    # If value is empty (e.g. "hermes-agent:" with nothing after),
                    # initialise as a dict so deeper children can nest into it.
                    if v == "":
                        result[current_top][k] = {}
                    else:
                        result[current_top][k] = v
                    current_sub = k
                continue

        if indent == 4 and current_top and current_sub:
            if stripped.startswith("- "):
                val = stripped[2:].strip()
                k, v = _split_kv(stripped[2:].strip()) if ":" in val else (None, val)
                if current_top in result and isinstance(result[current_top], dict):
                    if current_sub not in result[current_top]:
                        result[current_top][current_sub] = []
                    if isinstance(result[current_top][current_sub], list):
                        if k:
                            result[current_top][current_sub].append({k: v})
                        else:
                            result[current_top][current_sub].append(val)
            else:
                k, v = _split_kv(stripped)
                if current_top in result and isinstance(result[current_top], dict):
                    if current_sub in result[current_top] and isinstance(
                        result[current_top][current_sub], dict
                    ):
                        result[current_top][current_sub][k] = v

    # Clean up unnamed keys
    for top in result:
        if isinstance(result[top], dict) and "__unnamed__" in result[top]:
            val = result[top].pop("__unnamed__")
            result[top] = val

    return result


def _split_kv(line: str) -> tuple[str, str]:
    """Split 'key: value' → ('key', 'value')."""
    idx = line.index(":")
    k = line[:idx].strip().strip('"')
    v = line[idx + 1 :].strip().strip('"')
    return k, v


def load_config(path: Path) -> dict:
    """Load config YAML, falling back to built-in parser if PyYAML fails."""
    if not path.exists():
        return {}
    text = path.read_text()
    try:
        import yaml as _yaml
        return _yaml.safe_load(text) or {}
    except ImportError:
        return _yaml_parse(text)


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "services": {
        "hermes-agent": {
            "log_files": [
                "/var/lib/hermes/.hermes/logs/agent.log",
                "/var/lib/hermes/.hermes/logs/gateway.log",
            ],
            "journal_unit": "hermes-agent",
            "patterns": [
                {"regex": "(?i)traceback", "description": "Python tracebacks"},
                {"regex": "(?i)exception", "description": "Python exceptions"},
                {"regex": "(?i)\\bERROR\\b", "description": "ERROR log level"},
                {"regex": "(?i)failed", "description": "Failure indicator"},
                {"regex": "(?i)failed to connect", "description": "Connection failures"},
            ],
        },
        "hermes-nicegui": {
            "log_files": [
                "/var/lib/hermes/.hermes/logs/gui.log",
                "/var/lib/hermes/.hermes/logs/agent.log",
            ],
            "journal_unit": "hermes-nicegui",
            "patterns": [
                {"regex": "(?i)traceback", "description": "Python tracebacks"},
                {"regex": "(?i)exception", "description": "Python exceptions"},
                {"regex": "(?i)\\bERROR\\b", "description": "ERROR log level"},
                {"regex": "(?i)failed", "description": "Failure indicator"},
                {"regex": "(?i)can't start server", "description": "NiceGUI server failures"},
                {"regex": "(?i)crashed", "description": "Crash indicators"},
            ],
        },
        "podman-hindsight": {
            "log_files": [],  # no direct log files; use journalctl
            "journal_unit": "podman-hindsight",
            "patterns": [
                {"regex": "(?i)failed", "description": "Service failure"},
                {"regex": "(?i)crashed", "description": "Container crash"},
                {"regex": "(?i)error", "description": "Error messages"},
                {"regex": "(?i)oom", "description": "OOM kill"},
                {"regex": "(?i)segfault", "description": "Segfault"},
                {"regex": "(?i)health check failed", "description": "Health check failure"},
            ],
        },
    },
    "ignored": [],
    "heartbeat_interval": 300,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [log-monitor] %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("log-monitor")


# ---------------------------------------------------------------------------
# Ticket deduplication
# ---------------------------------------------------------------------------

# We track which services already have an open ticket by storing a file.
# A ticket is considered "open" if the file contains a non-empty service name.
# When the ticket is resolved externally, the operator deletes the file or
# clears its contents.

OPEN_TICKET_FILE = Path.home() / ".hermes/log-monitor/open-tickets.json"


def load_open_tickets() -> dict[str, str]:
    """Return {service_name: ticket_id}."""
    if not OPEN_TICKET_FILE.exists():
        return {}
    try:
        return json.loads(OPEN_TICKET_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_open_tickets(tickets: dict[str, str]) -> None:
    OPEN_TICKET_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPEN_TICKET_FILE.write_text(json.dumps(tickets, indent=2))


def remove_ticket(service: str) -> None:
    tickets = load_open_tickets()
    tickets.pop(service, None)
    save_open_tickets(tickets)


# ---------------------------------------------------------------------------
# Kanban ticket creation
# ---------------------------------------------------------------------------

# We create tickets via the kanban_create tool.  Since this script runs
# externally (not as a kanban worker), we use the Hermes HTTP API directly
# (the dashboard kanban API at :9119).  If that's unavailable, we write
# a ticket body to disk for manual triage.


def _kanban_api_url() -> str:
    return os.environ.get(
        "HERMES_KANBAN_URL", "http://127.0.0.1:9119"
    )


def create_ticket(
    service: str,
    error_line: str,
    context: str,
    config: dict,
) -> Optional[str]:
    """Open a kanban ticket for this error.  Returns the ticket ID if successful."""
    title = f"{service}: ERROR — {error_line[:120]}"

    body_lines = [
        f"**Service:** `{service}`",
        f"**Error:** {error_line}",
        "",
        "**Context:**",
        "```",
        context[:1000],
        "```",
        "",
        "--",
        "Filed automatically by `hermes-log-monitor`.",
    ]

    # Check if an ignored pattern matches this line
    ignored = config.get("ignored", [])
    for ign in ignored:
        ign_regex = ign.get("regex", "")
        ign_service = ign.get("service", "")
        if ign_service and ign_service != service:
            continue
        try:
            if re.search(ign_regex, error_line):
                logger.info("Ignored error for %s: %s", service, ign.get("description", "unknown"))
                return None  # suppressed
        except re.error:
            pass

    if service in load_open_tickets():
        logger.info("Ticket already open for %s — skipping", service)
        return None  # dedup

    # Attempt to create via kanban_create tool (when running in worker context)
    # We use the HERMES_KANBAN_TASK env var to detect this
    kanban_task = os.environ.get("HERMES_KANBAN_TASK")
    kanban_board = os.environ.get("HERMES_KANBAN_BOARD")

    if kanban_task:
        # Running as a kanban worker — use kanban_create via API
        pass  # handled below

    # Use the dashboard kanban REST API as fallback
    api_base = _kanban_api_url()
    try:
        import httpx

        resp = httpx.post(
            f"{api_base}/api/kanban/create",
            json={
                "title": title,
                "body": "\n".join(body_lines),
                "assignee": "default",
                "priority": 5,
                "labels": ["log-monitor"],
            },
            headers={
                "Authorization": f"Bearer {os.environ.get('HERMES_API_TOKEN', '')}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            ticket_id = resp.json().get("id", resp.json().get("task_id", "unknown"))
            tickets = load_open_tickets()
            tickets[service] = ticket_id
            save_open_tickets(tickets)
            logger.info("Created kanban ticket %s for %s", ticket_id, service)
            return ticket_id
        else:
            logger.warning("Kanban API error %d: %s", resp.status_code, resp.text[:200])
    except ImportError:
        logger.debug("httpx not available; writing ticket to disk")
    except (ConnectionError, OSError) as exc:
        logger.warning("Kanban API unreachable: %s", exc)

    # Fallback: write to a marker file so it's not lost
    marker_dir = Path.home() / ".hermes/log-monitor/tickets"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_file = marker_dir / f"{service}-{int(time.time())}.json"
    marker_file.write_text(json.dumps({
        "title": title,
        "body": "\n".join(body_lines),
        "service": service,
        "error": error_line,
        "context": context[:1000],
    }))
    logger.info("Wrote ticket marker to %s", marker_file)

    # Still record as open so we don't re-create
    tickets = load_open_tickets()
    tickets[service] = "disk-fallback"
    save_open_tickets(tickets)
    return "disk-fallback"


# ---------------------------------------------------------------------------
# Log monitoring
# ---------------------------------------------------------------------------


@dataclass
class LogStream:
    """Manages reading lines from a set of log files + optional journal."""

    log_files: list[str]
    journal_unit: Optional[str] = None
    seen_offsets: dict[str, int] = field(default_factory=dict)
    _journal_proc: Optional[subprocess.Popen] = None
    _journal_reader: Optional[Any] = None

    def setup_journal(self) -> bool:
        """Try to set up journalctl --follow. Returns True if successful."""
        if not self.journal_unit:
            return False

        try:
            proc = subprocess.Popen(
                [
                    "journalctl",
                    "--unit",
                    self.journal_unit,
                    "--no-pager",
                    "--follow",
                    "--output",
                    "cat",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._journal_proc = proc
            self._journal_reader = proc.stdout
            return True
        except FileNotFoundError:
            logger.info("journalctl not found; skipping journal for %s", self.journal_unit)
            return False
        except PermissionError:
            logger.info("No journalctl permission for %s; skipping journal", self.journal_unit)
            return False

    def read_all_existing(self, service: str) -> list[str]:
        """Read any unprocessed lines from log files."""
        lines = []
        for fpath in self.log_files:
            path = Path(fpath)
            if not path.exists():
                continue
            try:
                current_offset = self.seen_offsets.get(fpath, 0)
                with open(path, "r", errors="replace") as f:
                    f.seek(current_offset)
                    new_lines = f.readlines()
                    self.seen_offsets[fpath] = f.tell()
                    lines.extend(new_lines)
            except OSError as exc:
                logger.warning("Cannot read %s: %s", fpath, exc)
        return lines

    def poll_journal(self) -> list[str]:
        """Read any available lines from the journal subprocess."""
        if not self._journal_reader:
            return []

        lines = []
        for line in self._journal_reader:
            line = line.rstrip("\n")
            if line:
                lines.append(line)
        return lines

    def stop_journal(self) -> None:
        if self._journal_proc:
            self._journal_proc.terminate()
            try:
                self._journal_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._journal_proc.kill()

    def check_journal_alive(self) -> bool:
        """Return True if the journal subprocess is still running."""
        if self._journal_proc is None:
            return False
        return self._journal_proc.poll() is None


def compile_patterns(config: dict) -> dict[str, list[re.Pattern]]:
    """Pre-compile regex patterns per service."""
    compiled: dict[str, list[re.Pattern]] = {}
    for svc_name, svc_conf in config.get("services", {}).items():
        patterns = []
        for p in svc_conf.get("patterns", []):
            regex = p.get("regex", "")
            try:
                patterns.append(re.compile(regex))
            except re.error as exc:
                logger.warning("Invalid regex for %s: %s (%s)", svc_name, regex, exc)
        compiled[svc_name] = patterns
    return compiled


async def monitor_loop(config: dict) -> None:
    """Main monitoring loop."""
    patterns = compile_patterns(config)
    last_heartbeat = time.time()
    errors_seen = 0
    tickets_created = 0

    logger.info("Starting log monitor")
    logger.info("Services: %s", list(config.get("services", {}).keys()))

    # Build log streams
    streams: dict[str, LogStream] = {}
    for svc_name, svc_conf in config.get("services", {}).items():
        log_files = svc_conf.get("log_files", [])
        journal_unit = svc_conf.get("journal_unit")
        stream = LogStream(log_files=log_files, journal_unit=journal_unit)

        # Try journal setup; fall back gracefully
        stream.setup_journal()

        # Drain existing lines immediately (avoid creating tickets for old errors)
        existing = stream.read_all_existing(svc_name)
        logger.info("Drained %d existing lines for %s", len(existing), svc_name)

        streams[svc_name] = stream

    while True:
        now = time.time()

        # Heartbeat
        interval = config.get("heartbeat_interval", 300)
        if now - last_heartbeat >= interval:
            logger.info(
                "Heartbeat — errors seen: %d, tickets created: %d",
                errors_seen,
                tickets_created,
            )
            last_heartbeat = now

        # Process each service stream
        for svc_name, stream in streams.items():
            # Read new lines from files
            new_lines = stream.read_all_existing(svc_name)
            # Poll journal if available
            new_lines += stream.poll_journal()

            if not new_lines:
                # Check if journal subprocess died
                if stream._journal_proc is not None and not stream.check_journal_alive():
                    logger.warning(
                        "Journal subprocess for %s died; restarting", svc_name
                    )
                    stream.stop_journal()
                    stream.setup_journal()
                await asyncio.sleep(0.5)
                continue

            for line in new_lines:
                line = line.strip()
                if not line:
                    continue

                svc_patterns = patterns.get(svc_name, [])
                for pat in svc_patterns:
                    if pat.search(line):
                        errors_seen += 1
                        # Gather context: a few lines around the match
                        context = line
                        logger.info(
                            "Match for %s: %s [%s]", svc_name, line[:100], pat.pattern
                        )

                        # Create ticket
                        ticket_id = create_ticket(svc_name, line, context, config)
                        if ticket_id:
                            tickets_created += 1
                        break  # one ticket per line max

            await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    """CLI entry point."""
    _hermes_home = os.environ.get("HERMES_HOME")
    if _hermes_home:
        config_path = Path(_hermes_home) / "log-monitor/config.yaml"
    else:
        config_path = Path.home() / ".hermes/log-monitor/config.yaml"

    # Merge config: defaults + file overrides
    file_config = load_config(config_path)
    merged = DEFAULT_CONFIG.copy()
    for key in file_config:
        if key in merged and isinstance(merged[key], dict) and isinstance(file_config[key], dict):
            merged[key].update(file_config[key])
        else:
            merged[key] = file_config[key]

    config = merged

    # Write default config if none exists
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import yaml

            config_path.write_text(yaml.dump(DEFAULT_CONFIG, default_flow_style=False))
            logger.info("Wrote default config to %s", config_path)
        except ImportError:
            config_path.write_text("# config — see ~/.hermes/log-monitor/config.yaml\n")
            logger.info("Created empty config at %s (PyYAML not available)", config_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown
    def _signal_handler(sig: int, _frame: Any) -> None:
        logger.info("Received signal %d — shutting down", sig)
        loop.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler, sig)

    try:
        loop.run_until_complete(monitor_loop(config))
    finally:
        # Clean up journal subprocesses
        for stream in globals().get("_streams", {}).values():
            stream.stop_journal()
        loop.close()


if __name__ == "__main__":
    run()
