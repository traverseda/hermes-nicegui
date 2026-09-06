#!/usr/bin/env python3
"""Unit tests for import-old-cron.py (stdlib unittest, no network, no LAN).

Run with any python3::

    python3 test_import_old_cron.py -v

The fixture below is a sanitized stand-in for the legacy jobs.json that lived
on hermesagent.lan (identical schema; prompts trimmed, no secrets).
"""

import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location(
    "import_old_cron", _HERE / "import-old-cron.py")
assert _SPEC is not None and _SPEC.loader is not None, "importer module missing"
IOC = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(IOC)

LEGACY_FIXTURE = {
    "updated_at": "2026-08-14T22:51:00-03:00",
    "jobs": [
        {
            "id": "ee2bbc93c7b2",
            "name": "arr-queue-nightly-check",
            "prompt": "## Nightly arr Maintenance (trimmed fixture)",
            "skills": ["arr-stack"],
            "skill": "arr-stack",
            "model": None, "provider": None, "base_url": None,
            "script": None, "no_agent": False, "context_from": None,
            "schedule": {"kind": "cron", "expr": "0 2 * * *",
                         "display": "0 2 * * *"},
            "schedule_display": "0 2 * * *",
            "repeat": {"times": None, "completed": 25},
            "enabled": True, "state": "scheduled",
            "paused_at": None, "paused_reason": None,
            "created_at": "2026-07-23T13:42:09.498952-03:00",
            "next_run_at": "2026-08-15T02:00:00-03:00",
            "last_run_at": "2026-08-14T02:14:21.650390-03:00",
            "last_status": "ok", "last_error": None,
            "last_delivery_error": None,
            "deliver": "discord:1530660171720953977",
            "origin": {"platform": "discord", "chat_id": "1503191648962871437",
                       "chat_name": "traverseda", "thread_id": None},
            "enabled_toolsets": None, "workdir": None, "fire_claim": None,
        },
        {
            "id": "94165a4948c7",
            "name": "Sailboat Scan Nightly",
            "prompt": "Script-only job (trimmed fixture)",
            "skills": [], "skill": None,
            "model": None, "provider": None, "base_url": None,
            "script": "nightly-sailboat-scan.sh", "no_agent": True,
            "context_from": None,
            "schedule": {"kind": "cron", "expr": "0 2 * * *",
                         "display": "0 2 * * *"},
            "schedule_display": "0 2 * * *",
            "repeat": {"times": None, "completed": 40},
            "enabled": True, "state": "scheduled",
            "paused_at": None, "paused_reason": None,
            "created_at": "2026-08-11T08:33:31.723687-03:00",
            "next_run_at": "2026-08-15T02:00:00-03:00",
            "last_run_at": "2026-08-14T14:57:43.672640-03:00",
            "last_status": "ok", "last_error": None,
            "last_delivery_error": None,
            "deliver": "local",
            "origin": None,
            "enabled_toolsets": None, "workdir": None, "fire_claim": None,
        },
        {
            "id": "8270b3c221e7",
            "name": "Tailscale token rotation reminder",
            "prompt": "Remind traverseda about token rotation (trimmed)",
            "skills": [], "skill": None,
            "model": None, "provider": None, "base_url": None,
            "script": None, "no_agent": False, "context_from": None,
            "schedule": {"kind": "once", "run_at": "2026-11-05T09:00:00-03:00",
                         "display": "once at 2026-11-05 09:00"},
            "schedule_display": "once at 2026-11-05 09:00",
            "repeat": {"times": None, "completed": 0},
            "enabled": True, "state": "scheduled",
            "paused_at": None, "paused_reason": None,
            "created_at": "2026-08-12T15:41:52.603758-03:00",
            "next_run_at": None,
            "last_run_at": None, "last_status": None,
            "last_error": None, "last_delivery_error": None,
            "deliver": "discord:1503191648962871437",
            "origin": {"platform": "discord", "chat_id": "1503191648962871437",
                       "chat_name": "traverseda", "thread_id": None},
            "enabled_toolsets": None, "workdir": None, "fire_claim": None,
        },
        {
            "id": "9e8fe0d6a12e",
            "name": "Note organization",
            "prompt": "Fetch the organize prompt from xaelwiki (trimmed)",
            "skills": [], "skill": None,
            "model": None, "provider": None, "base_url": None,
            "script": None, "no_agent": False, "context_from": None,
            "schedule": {"kind": "cron", "expr": "0 4 * * *",
                         "display": "0 4 * * *"},
            "schedule_display": "0 4 * * *",
            "repeat": {"times": None, "completed": 12},
            "enabled": True, "state": "scheduled",
            "paused_at": None, "paused_reason": None,
            "created_at": "2026-08-10T14:10:29.069243-03:00",
            "next_run_at": "2026-08-15T04:00:00-03:00",
            "last_run_at": "2026-08-14T04:18:33.697793-03:00",
            "last_status": "ok", "last_error": None,
            "last_delivery_error": None,
            "deliver": "discord",
            "origin": {"platform": "discord", "chat_id": None,
                       "chat_name": None, "thread_id": None},
            "enabled_toolsets": None, "workdir": None, "fire_claim": None,
        },
    ],
}


class DeliverMapTests(unittest.TestCase):
    def test_exact_and_wildcard(self):
        dm = IOC.DeliverMap(IOC.DEFAULT_DELIVER_MAP)
        self.assertEqual(dm.apply("discord:1530660171720953977"), "local")
        self.assertEqual(dm.apply("discord"), "local")
        self.assertEqual(dm.apply("local"), "local")
        self.assertEqual(dm.apply(None), None)
        self.assertEqual(dm.apply("telegram:123"), "telegram:123")

    def test_custom_map(self):
        dm = IOC.DeliverMap("discord=origin,telegram:1=local")
        self.assertEqual(dm.apply("discord"), "origin")
        self.assertEqual(dm.apply("discord:999"), "discord:999")
        self.assertEqual(dm.apply("telegram:1"), "local")

    def test_empty_map(self):
        dm = IOC.DeliverMap("")
        self.assertEqual(dm.apply("discord:1"), "discord:1")


class AdaptJobTests(unittest.TestCase):
    def setUp(self):
        self.dm = IOC.DeliverMap(IOC.DEFAULT_DELIVER_MAP)

    def test_definition_fields_preserved(self):
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], self.dm)
        self.assertEqual(job["id"], "ee2bbc93c7b2")
        self.assertEqual(job["name"], "arr-queue-nightly-check")
        self.assertEqual(job["prompt"].startswith("## Nightly arr"), True)
        self.assertEqual(job["skills"], ["arr-stack"])
        self.assertEqual(job["schedule"]["expr"], "0 2 * * *")
        self.assertEqual(job["enabled"], True)
        self.assertEqual(job["created_at"], "2026-07-23T13:42:09.498952-03:00")

    def test_runtime_fields_reset(self):
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], self.dm)
        for field in ("next_run_at", "last_run_at", "last_status",
                      "last_error", "last_delivery_error", "fire_claim",
                      "paused_at", "paused_reason", "origin"):
            self.assertIsNone(job[field], field)
        self.assertEqual(job["state"], "scheduled")

    def test_repeat_counter_reset_limit_kept(self):
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], self.dm)
        self.assertEqual(job["repeat"], {"times": None, "completed": 0})

    def test_deliver_remapped(self):
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], self.dm)
        self.assertEqual(job["deliver"], "local")
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][3], self.dm)
        self.assertEqual(job["deliver"], "local")  # bare discord
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][1], self.dm)
        self.assertEqual(job["deliver"], "local")  # already local

    def test_once_schedule_preserved(self):
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][2], self.dm)
        self.assertEqual(job["schedule"]["kind"], "once")
        self.assertEqual(job["schedule"]["run_at"], "2026-11-05T09:00:00-03:00")
        self.assertIsNone(job["next_run_at"])

    def test_missing_id_rejected(self):
        with self.assertRaises(ValueError):
            IOC.adapt_job({"name": "no id"}, self.dm)

    def test_missing_schedule_rejected(self):
        with self.assertRaises(ValueError):
            IOC.adapt_job({"id": "x", "name": "no sched"}, self.dm)

    def test_disabled_job_keeps_pause_markers(self):
        job = {
            "id": "pz123", "name": "paused job",
            "prompt": "x", "skills": [], "skill": None,
            "model": None, "provider": None, "base_url": None,
            "script": None, "no_agent": False, "context_from": None,
            "schedule": {"kind": "cron", "expr": "0 2 * * *",
                         "display": "0 2 * * *"},
            "schedule_display": "0 2 * * *",
            "repeat": {"times": None, "completed": 5},
            "enabled": False, "state": "paused",
            "paused_at": "2026-08-10T00:00:00+00:00",
            "paused_reason": "dependency missing on new box",
            "deliver": "discord:1",
        }
        adapted = IOC.adapt_job(job, self.dm)
        self.assertEqual(adapted["enabled"], False)
        self.assertEqual(adapted["state"], "paused")
        self.assertEqual(adapted["paused_reason"], "dependency missing on new box")
        self.assertIsNotNone(adapted["paused_at"])
        self.assertIsNone(adapted["next_run_at"])
        self.assertEqual(adapted["repeat"]["completed"], 0)

    def test_enabled_job_forced_back_to_scheduled(self):
        job = dict(LEGACY_FIXTURE["jobs"][0])
        job["enabled"] = True
        job["state"] = "error"
        adapted = IOC.adapt_job(job, self.dm)
        self.assertEqual(adapted["state"], "scheduled")
        self.assertEqual(adapted["enabled"], True)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cron-import-test-")
        self.cron_dir = Path(self.tmp) / "cron"
        self.cron_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_fixture(self):
        jobs_file = self.cron_dir / "jobs.json"
        with open(jobs_file, "w", encoding="utf-8") as f:
            json.dump({"jobs": [], "updated_at": "x"}, f)
        return jobs_file

    def test_write_store_atomic_and_mode(self):
        jobs_file = self._write_fixture()
        jobs = [IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], IOC.DeliverMap(""))]
        IOC.write_store(self.cron_dir, jobs, backup=False)
        self.assertTrue(jobs_file.exists())
        self.assertEqual(jobs_file.stat().st_mode & 0o777, 0o600)
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data["jobs"]), 1)
        self.assertIn("updated_at", data)

    def test_write_store_backup(self):
        jobs_file = self._write_fixture()
        IOC.write_store(self.cron_dir, [], backup=True)
        baks = list(self.cron_dir.glob("jobs.json.bak-*"))
        self.assertEqual(len(baks), 1)

    def test_idempotency_skip(self):
        # First import lands a job; second import of the same id skips it.
        old = [IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], IOC.DeliverMap(""))]
        IOC.write_store(self.cron_dir, old, backup=False)
        existing = {str(j.get("id")) for j in IOC.load_target_jobs(self.cron_dir)}
        self.assertIn("ee2bbc93c7b2", existing)
        # Simulate importer path: same id, no --replace -> skip.
        job = IOC.adapt_job(LEGACY_FIXTURE["jobs"][0], IOC.DeliverMap(""))
        to_import = [] if job["id"] in existing else [job]
        self.assertEqual(to_import, [])

    def test_load_source_shapes(self):
        with open(Path(self.tmp) / "s.json", "w", encoding="utf-8") as f:
            json.dump(LEGACY_FIXTURE, f)
        jobs = IOC.load_source(Path(self.tmp) / "s.json")
        self.assertEqual(len(jobs), 4)
        with open(Path(self.tmp) / "s2.json", "w", encoding="utf-8") as f:
            json.dump(LEGACY_FIXTURE["jobs"], f)
        self.assertEqual(len(IOC.load_source(Path(self.tmp) / "s2.json")), 4)

    def test_lock_acquired(self):
        fd = IOC.acquire_jobs_lock(self.cron_dir)
        self.assertTrue(IOC._jobs_lock_file(self.cron_dir).exists())
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cron-import-cli-")
        self.cron_dir = Path(self.tmp) / "cron"
        self.cron_dir.mkdir()
        self.src = Path(self.tmp) / "legacy.json"
        with open(self.src, "w", encoding="utf-8") as f:
            json.dump(LEGACY_FIXTURE, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dry_run_writes_nothing(self):
        rc = IOC.main(["--source", str(self.src),
                       "--cron-dir", str(self.cron_dir), "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse((self.cron_dir / "jobs.json").exists())

    def test_import_and_filter(self):
        rc = IOC.main(["--source", str(self.src),
                       "--cron-dir", str(self.cron_dir),
                       "--jobs", "8270b3c221e7"])
        self.assertEqual(rc, 0)
        jobs = IOC.load_target_jobs(self.cron_dir)
        self.assertEqual([j["id"] for j in jobs], ["8270b3c221e7"])

    def test_import_all_and_replace(self):
        IOC.main(["--source", str(self.src),
                  "--cron-dir", str(self.cron_dir)])
        self.assertEqual(len(IOC.load_target_jobs(self.cron_dir)), 4)
        # Re-import without --replace: nothing changes.
        IOC.main(["--source", str(self.src),
                  "--cron-dir", str(self.cron_dir)])
        self.assertEqual(len(IOC.load_target_jobs(self.cron_dir)), 4)
        # With --replace the record is refreshed.
        IOC.main(["--source", str(self.src),
                  "--cron-dir", str(self.cron_dir), "--replace"])
        self.assertEqual(len(IOC.load_target_jobs(self.cron_dir)), 4)


class ManifestTests(unittest.TestCase):
    """Round-trip the real port manifest (legacy-jobs-import.json).

    The manifest is the redacted + adapted snapshot of the old box's store that
    lives next to this test file. It must import cleanly, contain no secrets,
    and land with the intended enabled/paused split.
    """

    MANIFEST = Path(__file__).resolve().parent / "legacy-jobs-import.json"
    KNOWN_IDS = {"ee2bbc93c7b2", "beca0bd83532", "8e4e00013457",
                 "38475d6b630e", "9e8fe0d6a12e", "94165a4948c7"}

    def setUp(self):
        if not self.MANIFEST.exists():
            self.skipTest("manifest not present")
        self.tmp = tempfile.mkdtemp(prefix="cron-manifest-test-")
        self.cron_dir = Path(self.tmp) / "cron"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_has_no_secrets(self):
        raw = self.MANIFEST.read_text(encoding="utf-8")
        self.assertNotRegex(raw, r"\b[0-9a-f]{32}\b")
        for secret in ("8e1f6186c1ef459399eac24b963951de",
                       "41c467f681394b51aa689bb05905d357",
                       "5282cd6eba894cba8f9915e944e6f42b",
                       "5U%Xo1TG$f#a8S"):
            self.assertNotIn(secret, raw)

    def test_manifest_imports_fully(self):
        rc = IOC.main(["--source", str(self.MANIFEST),
                       "--cron-dir", str(self.cron_dir)])
        self.assertEqual(rc, 0)
        jobs = {j["id"]: j for j in IOC.load_target_jobs(self.cron_dir)}
        self.assertEqual(set(jobs), self.KNOWN_IDS)
        # Two jobs active (self-review, note organization)…
        self.assertEqual(jobs["38475d6b630e"]["enabled"], True)
        self.assertEqual(jobs["9e8fe0d6a12e"]["enabled"], True)
        self.assertNotIn("8270b3c221e7", jobs)  # Tailscale reminder not ported
        # …four paused with markers (arr/tunarr/sailboat deps not provisioned).
        for jid in ("ee2bbc93c7b2", "beca0bd83532", "8e4e00013457",
                    "94165a4948c7"):
            self.assertEqual(jobs[jid]["enabled"], False)
            self.assertEqual(jobs[jid]["state"], "paused")
            self.assertTrue(jobs[jid]["paused_reason"])
        # Runtime fields reset for every imported job.
        for job in jobs.values():
            self.assertIsNone(job["next_run_at"])
            self.assertIsNone(job["last_run_at"])
            self.assertEqual(job["repeat"]["completed"], 0)

    def test_manifest_adaptation_spot_checks(self):
        jobs = {j["id"]: j for j in IOC.load_source(self.MANIFEST)}
        # Note organization prompt rewritten for this box's MCP server name.
        self.assertIn("mcp__xaelwiki__get_prompt",
                      jobs["9e8fe0d6a12e"]["prompt"])
        self.assertNotIn("mcp__xael__get_prompt",
                         jobs["9e8fe0d6a12e"]["prompt"])
        # cronjob-self-review keeps its script reference.
        self.assertEqual(jobs["38475d6b630e"]["script"], "cron-usage-summary.py")
        # Discarded jobs must not appear (Food Log / Fitness trio already
        # migrated under new ids by earlier tickets).
        for jid in ("1f4925e6fbe1", "4bb816aecdfe", "95de575a47b9",
                    "8270b3c221e7"):
            self.assertNotIn(jid, jobs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
