#!/usr/bin/env python3
"""Redact live credentials from ported legacy skills before they enter git.

The legacy skills on hermesagent.lan carried production secrets inline
(Sonarr/Radarr/Prowlarr API keys, Jellyfin tokens, an LDAP basic-auth
password). Copying them verbatim into the versioned flake repo would commit
secrets to the git ledger. This tool replaces every known secret value and
generic credential pattern with a named placeholder plus a pointer to the
runtime secret store (~/.hermes/.env), which is NOT versioned.

Verification: after rewriting, it greps for the known secret values and
generic token patterns and fails if anything is left behind.

Usage::

    python3 redact-legacy-secrets.py <src-dir> <dst-dir>
"""

import re
import shutil
import sys
from pathlib import Path

# Exact secret values found in the legacy skills (old agent, hermesagent.lan).
# Order matters: longer/more-specific values first.
KNOWN_SECRETS = [
    # (value, placeholder)
    ("8e1f6186c1ef459399eac24b963951de", "<SONARR_API_KEY>"),
    ("41c467f681394b51aa689bb05905d357", "<RADARR_API_KEY>"),
    ("4420d11323c840329fec948a4d5bb888", "<PROWLARR_API_KEY>"),
    ("5282cd6eba894cba8f9915e944e6f42b", "<JELLYFIN_API_KEY>"),
    ("5282cd6eba894cba", "<JELLYFIN_API_KEY>"),
    ("7fe31366c9f74d109b13272319d986ff", "<JELLYFIN_SESSION_TOKEN>"),
    ("0629f0b16b4c4909b917a89366b36030", "<JELLYFIN_ALT_TOKEN>"),
    ("bea9b218c97bbf98c5dc1303bdb9a0ca", "<JELLYFIN_ALT_TOKEN>"),
    ("0c9ee3a88fc15547c6852205480da1fd", "<JELLYFIN_ALT_TOKEN>"),
    ("5U%Xo1TG$f#a8S", "<LDAP_PASSWORD>"),
]

# Generic credential patterns (headers, query params, basic-auth userinfo).
GENERIC_PATTERNS = [
    # X-Api-Key / X-Emby-Token header values
    (re.compile(r"(X-Api-Key:\s*)[A-Za-z0-9._%$#@!~-]{8,}"), r"\1<ARR_API_KEY_FROM_ENV>"),
    (re.compile(r"(X-Emby-Token:\s*)[A-Za-z0-9._-]{8,}"), r"\1<JELLYFIN_TOKEN_FROM_ENV>"),
    # apikey/api_key query params
    (re.compile(r"(\?|&)apikey=[A-Za-z0-9._-]{8,}"), r"\1apikey=<key>"),
    (re.compile(r"(\?|&)api_key=[A-Za-z0-9._-]{8,}"), r"\1api_key=<key>"),
    # basic-auth userinfo in URLs: https://user:pass@host
    (re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@"), r"\1<user>:<pass>@"),
    # 32-hex token lookalikes that slipped past the named list
    (re.compile(r"\b[0-9a-f]{32}\b"), "<REDACTED_HEX_TOKEN>"),
]

REPLACEMENT_NOTE = (
    "\n<!-- SECRETS REDACTED during the cron-manager port (2026-08-15).\n"
    "Live values live in ~/.hermes/.env (ARR_* / JELLYFIN_* keys) and\n"
    "~/.hermes/internal-creds.json (basic auth). Do not paste real keys\n"
    "into this file. -->\n"
)

# Text-document suffixes get the trailing HTML-comment note; .json must stay
# parseable, so it is redacted in place with NO note appended.
_NOTE_SUFFIXES = (".md", ".py", ".txt", ".yaml", ".yml")
_PROCESSED_SUFFIXES = _NOTE_SUFFIXES + (".json",)


def redact_text(text: str) -> str:
    for value, placeholder in KNOWN_SECRETS:
        text = text.replace(value, placeholder)
    for pattern, repl in GENERIC_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def verify_text(text: str, path: Path) -> list:
    problems = []
    for value, _ in KNOWN_SECRETS:
        if value in text:
            problems.append(f"{path}: known secret value still present: {value[:12]}...")
    for name, (pattern, _) in zip(
            ["header-key", "header-token", "apikey", "api_key",
             "userinfo", "hex32"], GENERIC_PATTERNS):
        for m in pattern.finditer(text):
            # Placeholders like https://<user>:<pass>@ or apikey=<key> contain
            # angle brackets; a real leaked credential never does. Skip them.
            if "<" in m.group(0) and ">" in m.group(0):
                continue
            problems.append(f"{path}: {name} pattern matched: {m.group(0)[:40]!r}")
    return problems


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    problems = []
    for path in sorted(src.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        raw = path.read_bytes()
        if path.suffix in _PROCESSED_SUFFIXES:
            text = raw.decode("utf-8", errors="replace")
            redacted = redact_text(text)
            if path.suffix in _NOTE_SUFFIXES and redacted != text \
                    and not redacted.endswith(REPLACEMENT_NOTE):
                redacted += REPLACEMENT_NOTE
            out.write_text(redacted, encoding="utf-8")
            problems += verify_text(redacted, out)
        else:
            shutil.copy2(path, out)
            # Unprocessed text-like files (shell scripts, configs) are NOT
            # exempt from the scan: copy them, then verify the copy for
            # secrets. Binary files fail the UTF-8 decode and are skipped.
            try:
                copied_text = out.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            problems += verify_text(copied_text, out)
    if problems:
        print("REDACTION VERIFICATION FAILED:")
        for p in problems:
            print("  " + p)
        return 1
    print(f"redacted {src} -> {dst}: OK (no secrets left)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
