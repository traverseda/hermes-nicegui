#!/usr/bin/env python3
"""smoke_test.py — read-only/DRY_RUN smoke test for the ported calorie-tracking scripts.

Runs every script in a mode that never writes to the real log/library and
never touches Home Assistant (DRY_RUN where supported; read-only commands
otherwise). Uses temp dirs for log/library paths so the seeded data in
../data is never modified.

Exit 0 = all checks pass; non-zero = at least one check failed.
"""

import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "..", "data")
PY = os.environ.get("PYTHON", sys.executable)


def run(cmd, env=None):
    merged = dict(os.environ)
    merged.setdefault("DRY_RUN", "1")
    if env:
        merged.update(env)
    return subprocess.run(cmd, capture_output=True, text=True, env=merged)


def main():
    with tempfile.TemporaryDirectory() as td:
        log_path = os.path.join(td, "food-log.jsonl")
        foods_path = os.path.join(td, "foods.json")
        workouts_path = os.path.join(td, "workouts.jsonl")
        # Copy the seeded library so the real one is untouched.
        with open(foods_path, "w") as fh:
            fh.write(open(os.path.join(DATA, "foods.json")).read())
        # Seed the temp log with one history entry so edit/undo have a target.
        with open(log_path, "w") as fh:
            with open(os.path.join(DATA, "food-log.jsonl")) as src:
                fh.write(src.readline())

        env = {
            "FOOD_LOG_PATH": log_path,
            "FOODS_PATH": foods_path,
            "WORKOUTS_LOG_PATH": workouts_path,
            # NOTE: food_log.py undo/edit push helpers in DRY_RUN (no creds
            # needed). fitness_planner.py sync/log REQUIRE live HA creds even
            # in DRY_RUN (they read the todo list) — verified separately.
            "HASS_ENV_PATH": "/nonexistent/.env",
        }
        checks = [
            ("food_log.py foods", [PY, os.path.join(HERE, "food_log.py"), "foods"], env),
            ("food_log.py foods --search", [PY, os.path.join(HERE, "food_log.py"), "foods", "--search", "chicken"], env),
            ("food_log.py status", [PY, os.path.join(HERE, "food_log.py"), "status"], env),
            ("food_log.py today", [PY, os.path.join(HERE, "food_log.py"), "today"], env),
            ("food_log.py add --food (DRY_RUN)", [PY, os.path.join(HERE, "food_log.py"), "add", "--food", "banana (medium)"], env),
            ("food_log.py add explicit (DRY_RUN)", [PY, os.path.join(HERE, "food_log.py"), "add", "--item", "test meal", "--kcal", "100", "--protein", "10", "--carbs", "5", "--fat", "3", "--remember"], env),
            ("food_log.py add ambiguous exits 2", [PY, os.path.join(HERE, "food_log.py"), "add", "--food", "omelette"], env),
            ("food_log.py undo (DRY_RUN)", [PY, os.path.join(HERE, "food_log.py"), "undo"], env),
            ("food_log.py edit --remove (DRY_RUN)", [PY, os.path.join(HERE, "food_log.py"), "edit", "--item", "0", "--remove", "--day", "2026-08-11"], env),
            ("fitness_planner.py status", [PY, os.path.join(HERE, "fitness_planner.py"), "status"], env),
            ("fitness_planner.py next", [PY, os.path.join(HERE, "fitness_planner.py"), "next"], env),
            ("weekly_fitness_collect.py syntax", [PY, "-m", "py_compile", os.path.join(HERE, "weekly_fitness_collect.py")], {}),
            ("fitness_snapshot.py syntax", [PY, "-m", "py_compile", os.path.join(HERE, "fitness_snapshot.py")], {}),
            ("food_log.py syntax", [PY, "-m", "py_compile", os.path.join(HERE, "food_log.py")], {}),
            ("fitness_planner.py syntax", [PY, "-m", "py_compile", os.path.join(HERE, "fitness_planner.py")], {}),
        ]
        # The ambiguous-add check must exit 2; treat its non-zero as PASS.
        special_rc = {"food_log.py add ambiguous exits 2": 2}

        failed = []
        for name, cmd, extra_env in checks:
            p = run(cmd, env=extra_env)
            want = special_rc.get(name, 0)
            ok = p.returncode == want
            status = "PASS" if ok else f"FAIL(rc={p.returncode}, want {want})"
            print(f"[{status}] {name}")
            if not ok:
                failed.append(name)
                if p.stderr.strip():
                    print("    stderr: " + p.stderr.strip().splitlines()[-1][:300])
        if failed:
            print(f"\n{len(failed)} check(s) failed: {', '.join(failed)}")
            return 1
        print("\nall smoke checks passed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
