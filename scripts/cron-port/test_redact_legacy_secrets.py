#!/usr/bin/env python3
"""Unit tests for redact-legacy-secrets.py (stdlib unittest, no network).

Run with any python3::

    python3 test_redact_legacy_secrets.py -v
"""

import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "redact_legacy_secrets", _HERE / "redact-legacy-secrets.py")
assert _SPEC is not None and _SPEC.loader is not None, "redactor module missing"
RLS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(RLS)


def _run(src: Path, dst: Path) -> int:
    """Invoke the redactor's main() with src/dst args (it reads sys.argv)."""
    with mock.patch.object(sys, "argv", ["redact-legacy-secrets.py",
                                         str(src), str(dst)]):
        return RLS.main()


class RedactTextTests(unittest.TestCase):
    def test_known_secrets_replaced(self):
        text = ("key 8e1f6186c1ef459399eac24b963951de and "
                "5U%Xo1TG$f#a8S here")
        out = RLS.redact_text(text)
        self.assertNotIn("8e1f6186c1ef459399eac24b963951de", out)
        self.assertIn("<SONARR_API_KEY>", out)
        self.assertIn("<LDAP_PASSWORD>", out)

    def test_generic_headers_and_userinfo(self):
        text = ("X-Api-Key: 0123456789abcdef0123456789abcdef "
                "https://hermes:secretpass@sonarr.0u0.ca/api "
                "?apikey=0123456789abcdef0123456789abcdef")
        out = RLS.redact_text(text)
        self.assertNotIn("0123456789abcdef0123456789abcdef", out)
        self.assertNotIn("secretpass", out)
        self.assertIn("https://<user>:<pass>@", out)
        self.assertIn("<ARR_API_KEY_FROM_ENV>", out)

    def test_no_op_on_clean_text(self):
        self.assertEqual(RLS.redact_text("just plain docs"), "just plain docs")


class VerifyTextTests(unittest.TestCase):
    def test_placeholder_shapes_are_not_flags(self):
        # The verifier must not trip on its own replacements.
        clean = ("key https://<user>:<pass>@host apikey=<key> "
                 "X-Api-Key: <ARR_API_KEY_FROM_ENV>")
        self.assertEqual(RLS.verify_text(clean, Path("x.md")), [])

    def test_leftover_secret_is_flagged(self):
        problems = RLS.verify_text(
            "leak 8e1f6186c1ef459399eac24b963951de", Path("x.md"))
        self.assertTrue(problems)
        self.assertTrue(any("known secret" in p for p in problems))

    def test_leftover_hex32_is_flagged(self):
        problems = RLS.verify_text("random 9f1a2b3c4d5e6f708192a3b4c5d6e7f8",
                                   Path("x.md"))
        self.assertTrue(any("hex32" in p for p in problems))


class DirRedactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="redact-test-")
        self.src = Path(self.tmp) / "src"
        self.dst = Path(self.tmp) / "dst"
        self.src.mkdir()
        self.skill = self.src / "skill"
        self.skill.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = self.skill / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_json_stays_parseable(self):
        self._write("jobs.json", json.dumps(
            {"jobs": [{"prompt": "key 8e1f6186c1ef459399eac24b963951de"}]}))
        rc = _run(self.src, self.dst)
        self.assertEqual(rc, 0)
        out = self.dst / "skill" / "jobs.json"
        data = json.loads(out.read_text(encoding="utf-8"))
        self.assertNotIn("8e1f6186c1ef459399eac24b963951de",
                         data["jobs"][0]["prompt"])
        self.assertNotIn("SECRETS REDACTED", out.read_text(encoding="utf-8"))

    def test_md_gets_note_and_redaction(self):
        self._write("SKILL.md", "token 5282cd6eba894cba8f9915e944e6f42b")
        rc = _run(self.src, self.dst)
        self.assertEqual(rc, 0)
        out = self.dst / "skill" / "SKILL.md"
        text = out.read_text(encoding="utf-8")
        self.assertNotIn("5282cd6eba894cba8f9915e944e6f42b", text)
        self.assertIn("SECRETS REDACTED", text)

    def test_binary_passthrough(self):
        (self.skill / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\nfakepng")
        rc = _run(self.src, self.dst)
        self.assertEqual(rc, 0)
        self.assertEqual((self.dst / "skill" / "logo.png").read_bytes(),
                         b"\x89PNG\r\n\x1a\nfakepng")

    def test_leftover_secret_fails_run(self):
        # A known secret inside an unprocessed suffix (.sh is copied verbatim,
        # not rewritten) must still trip the post-copy verification.
        self._write("job.sh", "TOKEN=41c467f681394b51aa689bb05905d357\n")
        rc = _run(self.src, self.dst)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
