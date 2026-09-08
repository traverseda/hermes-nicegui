"""Read-only access to a xaelwiki notes vault.

The vault is a plain git repository of markdown files with YAML frontmatter
(title, tags, status, folder, created, updated, ...) laid out in the
canonical xaelwiki folder tree (00-inbox, 10-projects, 20-areas,
30-resources, 40-archive, _meta). This store scans that tree, parses the
frontmatter, and serves the notes + real-time search to the UI.

Everything here is deliberately dependency-free (stdlib + pyyaml) and
READ-ONLY: the plugin never writes to the vault, never shells out, and does
not need the xaelwiki MCP server or its auth token — it reads the same files
the MCP server reads, straight from disk. The directory must be readable by
the process user (on the deploy box the hermes user is added to the
xaelwiki group for exactly this).

Note ids are the file stem (``spiri-project-family``), which the vault's
slug-filename convention keeps unique; on the (unexpected) collision the
second-and-later files get a deterministic short-hash suffix so every note
stays addressable.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    """Split markdown into (frontmatter dict, body) — mirrors xaelwiki's own parser.

    Returns ``(None, text)`` when the file has no leading frontmatter block or
    the block is unparsable, so plain markdown files still show up.
    """
    if not text.startswith("---\n"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            raw = "\n".join(lines[1:i])
            try:
                data = yaml.safe_load(raw) or {}
            except yaml.YAMLError:
                return None, text
            if not isinstance(data, dict):
                return None, text
            return data, "\n".join(lines[i + 1 :]).lstrip("\n")
    return None, text


@dataclass(frozen=True)
class Note:
    """One note from the vault."""

    id: str
    title: str
    rel_path: str  # e.g. "10-projects/spiri-project-family.md"
    folder: str  # e.g. "10-projects"; "" for files at the vault root
    status: str
    tags: list[str]
    created: str
    updated: str
    body: str

    @property
    def body_preview(self) -> str:
        first = next((ln.strip() for ln in self.body.splitlines() if ln.strip()), "")
        return first[:160]


class NoteStore:
    """Scans and searches a xaelwiki vault directory (thread-safe, TTL-cached)."""

    def __init__(
        self,
        notes_dir: str | Path = "/var/lib/xaelwiki/notes",
        refresh_seconds: float = 30.0,
    ) -> None:
        self.notes_dir = Path(notes_dir)
        self.refresh_seconds = max(1.0, float(refresh_seconds))
        self._lock = threading.Lock()
        self._notes: list[Note] = []
        self._scanned_at = 0.0
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def is_readable(self) -> bool:
        """True when the vault directory exists (a quick check for the UI)."""
        return self.notes_dir.is_dir()

    def _scan_locked(self) -> None:
        """(Re)build the note index from disk. Caller must hold the lock."""
        notes: list[tuple[str, Note]] = []  # (stem, note)
        try:
            if not self.notes_dir.is_dir():
                raise NotADirectoryError(f"not a directory: {self.notes_dir}")
            for path in sorted(self.notes_dir.rglob("*.md")):
                if ".git" in path.parts:
                    continue
                rel = path.relative_to(self.notes_dir).as_posix()
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:  # unreadable file — skip, keep the rest
                    self._last_error = f"cannot read {rel}: {exc}"
                    continue
                fm, body = split_frontmatter(text)
                stem = path.stem
                title = (fm or {}).get("title") or stem
                notes.append(
                    (
                        stem,
                        Note(
                            id=stem,  # disambiguated below
                            title=str(title),
                            rel_path=rel,
                            folder=path.parent.relative_to(self.notes_dir).as_posix()
                            if path.parent != self.notes_dir
                            else "",
                            status=str((fm or {}).get("status") or ""),
                            tags=[str(t) for t in ((fm or {}).get("tags") or [])],
                            created=str((fm or {}).get("created") or ""),
                            updated=str((fm or {}).get("updated") or ""),
                            body=body,
                        ),
                    )
                )
        except OSError as exc:
            self._last_error = f"cannot scan {self.notes_dir}: {exc}"
            return

        # Disambiguate duplicate stems deterministically (slug convention makes
        # them rare; when they happen, suffix the later files with a path hash).
        by_stem: dict[str, list[tuple[str, Note]]] = {}
        for stem, note in notes:
            by_stem.setdefault(stem, []).append((note.rel_path, note))
        for stem, items in by_stem.items():
            if len(items) == 1:
                continue
            for rel, note in items[1:]:
                suffix = hashlib.sha1(rel.encode()).hexdigest()[:6]
                note = Note(
                    id=f"{stem}-{suffix}",
                    title=note.title,
                    rel_path=note.rel_path,
                    folder=note.folder,
                    status=note.status,
                    tags=note.tags,
                    created=note.created,
                    updated=note.updated,
                    body=note.body,
                )
                notes.append((stem, note))

        # Dedupe by final id, newest-updated first. For a duplicate stem the
        # first (path-sorted) occurrence keeps the bare stem id; the rest keep
        # their unique suffixed ids.
        unique = {n.id: n for _, n in notes}
        self._notes = sorted(
            unique.values(),
            key=lambda n: (n.updated or "", n.title.lower()),
            reverse=True,
        )
        self._last_error = None
        self._scanned_at = time.monotonic()

    def notes(self) -> list[Note]:
        """All notes, newest-updated first; rescanning only when the TTL expired."""
        with self._lock:
            if not self._notes or time.monotonic() - self._scanned_at >= self.refresh_seconds:
                self._scan_locked()
            return list(self._notes)

    def refresh(self) -> None:
        """Force a rescan (manual refresh button, folder changes)."""
        with self._lock:
            self._scan_locked()

    def get(self, note_id: str) -> Note | None:
        """Fetch one note by id (case-insensitive)."""
        want = note_id.lower()
        for note in self.notes():
            if note.id.lower() == want:
                return note
        return None

    def folders(self) -> list[str]:
        """Distinct folders, sorted (canonical xaelwiki order first)."""
        seen: set[str] = set()
        out: list[str] = []
        for prefix in (
            "00-inbox",
            "10-projects",
            "20-areas",
            "30-resources",
            "40-archive",
            "_meta",
        ):
            for note in self.notes():
                if note.folder == prefix and prefix not in seen:
                    seen.add(prefix)
                    out.append(prefix)
        for note in self.notes():
            if note.folder and note.folder not in seen:
                seen.add(note.folder)
                out.append(note.folder)
        return out

    def search(self, query: str = "", folder: str | None = None) -> list[Note]:
        """Filter notes in real time: case-insensitive substring across
        title, tags AND body. Empty query returns everything (still folder-
        filtered). Sorting is newest-updated first.
        """
        q = (query or "").strip().lower()
        out = []
        for note in self.notes():
            if folder and note.folder != folder:
                continue
            if not q:
                out.append(note)
                continue
            haystack = " ".join(
                [note.title, note.body, " ".join(note.tags), note.status, note.folder]
            ).lower()
            if q in haystack:
                out.append(note)
        return out
