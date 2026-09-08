"""Pure helpers for the files plugin: path sandboxing, type/size formatting,
and streaming tar.zst archival. No NiceGUI, no request handling -- kept
separate so it's directly unit-testable, mirroring the other plugins'
``logic.py`` modules.
"""

from __future__ import annotations

import mimetypes
import os
import tarfile
import threading
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath

import zstandard


class PathEscapesRoot(ValueError):
    """A requested path would resolve outside the configured root."""


class DestinationExists(ValueError):
    """A move/copy destination already exists -- refuse to silently clobber it."""


def resolve_under_root(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` (URL path segments, possibly empty) under ``root``.

    Rejects anything -- a literal ``..`` segment or a symlink -- that would
    resolve outside ``root``. This is the only thing standing between the
    files plugin and unrestricted filesystem access from the browser, so
    every route handler must go through it before touching a path.
    """
    candidate = root
    for part in PurePosixPath(rel_path).parts:
        if part in ("", "."):
            continue
        if part == "..":
            raise PathEscapesRoot(rel_path)
        candidate = candidate / part
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise PathEscapesRoot(rel_path)
    return resolved


def validate_new_path(root: Path, source: Path, dest_rel: str) -> Path:
    """Resolve and validate a move/copy destination.

    Reuses `resolve_under_root` for sandboxing, then rejects a destination
    identical to the source (``ValueError``) or one that already exists
    (``DestinationExists``) -- callers should surface the latter as "pick a
    different name" rather than silently overwriting.
    """
    dest = resolve_under_root(root, dest_rel)
    if dest == source:
        raise ValueError("Source and destination are the same")
    if dest.exists():
        raise DestinationExists(dest_rel)
    return dest


def sanitize_relative_path(name: str) -> Path | None:
    """Turn an untrusted upload filename into a safe relative path.

    Folder uploads send a browser-supplied ``webkitRelativePath`` (e.g.
    ``"project/src/main.py"``) as the filename; this rejects anything that
    could escape the upload's target directory (``..`` segments, absolute
    paths) and returns ``None`` for those instead of a path.
    """
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return Path(*parts)


def parent_rel(rel: str) -> str:
    """The rel-path of ``rel``'s parent directory (``""`` at the root)."""
    return "/".join(Path(rel).parts[:-1])


def human_size(num_bytes: int) -> str:
    """A short human-readable size label (``"1.2 MB"``)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# Extensions CodeMirror (via nicegui's bundled language list) has a mode
# for. Not exhaustive -- unmapped text files still get a plain editor via
# `classify`'s text/binary sniff, just without syntax highlighting.
_LANGUAGE_BY_EXT: dict[str, str] = {
    ".py": "Python",
    ".pyw": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JSX",
    ".ts": "TypeScript",
    ".tsx": "TSX",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "SCSS",
    ".less": "LESS",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".sql": "SQL",
    ".xml": "XML",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cc": "C++",
    ".cs": "C#",
    ".rb": "Ruby",
    ".php": "PHP",
    ".lua": "Lua",
    ".pl": "Perl",
    ".r": "R",
    ".swift": "Swift",
    ".kt": "Kotlin",
    ".scala": "Scala",
    ".vue": "Vue",
    ".ini": "Properties files",
    ".cfg": "Properties files",
    ".properties": "Properties files",
    ".diff": "diff",
    ".patch": "diff",
}


def guess_language(name: str) -> str | None:
    """Best-effort CodeMirror language name for a file's name/extension."""
    if name.lower() == "dockerfile":
        return "Dockerfile"
    return _LANGUAGE_BY_EXT.get(Path(name).suffix.lower())


def is_probably_text(sample: bytes) -> bool:
    """Sniff a chunk of a file to guess whether it's text.

    A null byte is the standard binary tell; short of that, anything that
    decodes as UTF-8 is accepted, since that's what the editor would
    manage to display anyway.
    """
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


Kind = str  # one of "image", "pdf", "audio", "video", "text", "binary"


def classify(name: str, *, sniff: bytes = b"") -> Kind:
    """Classify a file for preview purposes from its name (and optionally a
    sample of its content, for text detection when the extension is
    unrecognized)."""
    mime, _ = mimetypes.guess_type(name)
    if mime == "application/pdf":
        return "pdf"
    if mime:
        if mime.startswith("image/"):
            return "image"
        if mime.startswith("audio/"):
            return "audio"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("text/") or mime in (
            "application/json",
            "application/xml",
            "application/javascript",
            "application/x-sh",
        ):
            return "text"
    if guess_language(name) is not None:
        return "text"
    if sniff and is_probably_text(sniff):
        return "text"
    return "binary"


# (icon, color) per `classify` kind, in the same style as the cron plugin's
# `state_icon` -- a listing row's icon carries its type at a glance instead
# of everything sharing one generic file glyph.
_KIND_ICONS: dict[Kind, tuple[str, str]] = {
    "image": ("image", "teal"),
    "pdf": ("picture_as_pdf", "red"),
    "audio": ("audiotrack", "purple"),
    "video": ("movie", "indigo"),
    "text": ("description", "primary"),
    "binary": ("insert_drive_file", "grey"),
}


def kind_icon(name: str, *, is_dir: bool, sniff: bytes = b"") -> tuple[str, str]:
    """(icon, color) for a file/directory listing row."""
    if is_dir:
        return "folder", "amber"
    return _KIND_ICONS[classify(name, sniff=sniff)]


def fmt_mtime(ts: float) -> str:
    """A short relative label (``"now"``, ``"5m"``, ``"3h"``, or a date),
    matching `plugins/sessions/logic.py::fmt_age`'s convention."""
    delta = datetime.now().timestamp() - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return datetime.fromtimestamp(ts).strftime("%b %d")


def _write_tar_zst(root: Path, arcname: str, write_fd: int) -> None:
    with os.fdopen(write_fd, "wb") as raw:
        compressor = zstandard.ZstdCompressor(level=6)
        with compressor.stream_writer(raw) as zf, tarfile.open(fileobj=zf, mode="w|") as tf:
            tf.add(root, arcname=arcname)


def iter_tar_zst(root: Path, *, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """Stream ``root`` (a file or directory) as a ``.tar.zst`` archive.

    Archiving happens in a background thread feeding one end of an OS pipe
    while this generator reads the other, so the archive is produced and
    sent incrementally instead of being buffered whole in memory or on disk
    first. Starlette's ``StreamingResponse`` runs plain (non-async)
    generators like this one in its own thread pool, one ``next()`` call at
    a time, which is what actually drives the read loop below.
    """
    read_fd, write_fd = os.pipe()
    thread = threading.Thread(target=_write_tar_zst, args=(root, root.name, write_fd), daemon=True)
    thread.start()
    try:
        with os.fdopen(read_fd, "rb") as reader:
            while chunk := reader.read(chunk_size):
                yield chunk
    finally:
        thread.join()
