"""Exercise the real nightly shell control flow with isolated, non-network command stubs.

No generator, repository, token, LaunchAgent or live publisher is used. Set
NIGHTLY_SCRIPT_UNDER_TEST only to compare a retained script with the current one.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(os.environ.get("NIGHTLY_SCRIPT_UNDER_TEST",
                            Path(__file__).with_name("regenerate_and_publish.sh")))
STUB = r'''
import json, os, pathlib, shutil, sys
root = pathlib.Path(os.environ["TEST_RUN_ROOT"])
kind, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
case = os.environ["TEST_CASE"]
with (root / "calls.jsonl").open("a") as f:
    f.write(json.dumps({"command": kind, "args": args, "cwd": os.getcwd()}) + "\n")
rc = 0
if kind == "mktemp":
    if case == "mktemp_failure":
        rc = 1
    elif case != "mktemp_empty":
        target = root / "owned-worktree"
        target.mkdir()
        print(target)
elif kind == "python3":
    name = pathlib.Path(args[0]).name
    if name == "publish_data.py":
        rc = 1 if case == "guard_refusal" else 0
    elif name == "build_lrt_stations.py" and case in ("lrt_source_failure", "lrt_rollback_failure"):
        rc = 2 if case == "lrt_rollback_failure" else 1
    elif (case == "generator_failure" and name == "build_mtr_stations.py") or (
            case == "crosswalk_failure" and name == "build_gtfs_crosswalk.py"):
        rc = 1
elif kind == "git":
    if args[0] == "rev-parse":
        if case == "record_failure":
            rc = 1
        else:
            print("a" * 40)
    elif args[0] == "worktree" and args[1] == "add":
        if case == "worktree_failure":
            rc = 1
        if case == "enter_worktree_failure":
            shutil.rmtree(root / "owned-worktree")
    elif args[0] == "diff":
        rc = 0 if case == "no_changes" else (2 if case == "diff_failure" else 1)
    elif case == args[0] + "_failure":
        rc = 1
sys.exit(rc)
'''


class NightlyPublishTests(unittest.TestCase):
    def run_case(self, case, record=False):
        temp = tempfile.TemporaryDirectory(prefix="departly-nightly-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        tools, site, commands = (root / name for name in ("tools", "site", "bin"))
        for directory in (tools, site / ".git", commands):
            directory.mkdir(parents=True)
        (site / "preserve.txt").write_text("unrelated site content")
        local_script = tools / "regenerate_and_publish.sh"
        shutil.copy2(SCRIPT, local_script)
        for name in ("git", "python3", "mktemp"):
            stub = commands / name
            stub.write_text(f"#!{sys.executable}\n" + STUB)
            stub.chmod(0o700)
        environment = {
                                    "PATH": f"{commands}:/usr/bin:/bin",
                                    "DEPARTLY_SITE_REPO": str(site),
                                    "TEST_RUN_ROOT": str(root), "TEST_CASE": case,
                                }
        if record:
            environment["DEPARTLY_PUBLISHED_COMMIT_FILE"] = str(root / "published-commit")
        result = subprocess.run(["/bin/bash", str(local_script)], text=True,
                                capture_output=True, timeout=15, env=environment)
        recorded = root / "published-commit"
        result.recorded_commit = recorded.read_text().strip() if recorded.exists() else None
        calls = [json.loads(line) for line in (root / "calls.jsonl").read_text().splitlines()]
        self.assertEqual((site / "preserve.txt").read_text(), "unrelated site content")
        self.assertFalse((root / "owned-worktree").exists(), "Owned temporary worktree must be cleaned")
        for call in calls:
            if call["command"] == "git" and call["args"][0] in {"add", "diff", "commit", "push"}:
                self.assertEqual(call["cwd"], str(root / "owned-worktree"),
                                 "Publication must never run from the shared site checkout")
        return result, calls

    def assert_no_git(self, calls, *commands):
        actual = {call["args"][0] for call in calls if call["command"] == "git"}
        self.assertFalse(actual.intersection(commands), actual)

    def test_clean_publish(self):
        result, calls = self.run_case("clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT: OK", result.stdout)
        pushes = [c for c in calls if c["command"] == "git" and c["args"][0] == "push"]
        self.assertEqual([c["args"] for c in pushes], [["push", "origin", "HEAD:main"]])

    def test_unchanged_data_does_not_commit_or_push(self):
        result, calls = self.run_case("no_changes")
        self.assertEqual(result.returncode, 0)
        self.assertIn("No data changes to publish", result.stdout)
        self.assert_no_git(calls, "commit", "push")

    def test_publication_commit_record_for_clean_and_no_change(self):
        for case in ("clean", "no_changes"):
            with self.subTest(case=case):
                result, _ = self.run_case(case, record=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.recorded_commit, "a" * 40)

    def test_commit_record_failure_cannot_start_pages_with_false_success(self):
        result, _ = self.run_case("record_failure", record=True)
        self.assertEqual(result.returncode, 4)
        self.assertNotIn("RESULT: OK", result.stdout)

    def test_generator_failure_keeps_degraded_contract(self):
        result, calls = self.run_case("generator_failure")
        self.assertEqual(result.returncode, 3)
        self.assertIn("RESULT: DEGRADED", result.stdout)
        self.assertNotIn("RESULT: OK", result.stdout)
        self.assertTrue(any(c["command"] == "git" and c["args"][0] == "push" for c in calls))

    def test_crosswalk_failure_is_degraded_and_skips_dependents(self):
        result, calls = self.run_case("crosswalk_failure")
        self.assertEqual(result.returncode, 3)
        self.assertIn("RESULT: DEGRADED", result.stdout)
        generators = {Path(c["args"][0]).name for c in calls if c["command"] == "python3"}
        self.assertNotIn("build_timetable.py", generators)
        self.assertNotIn("build_fares_od.py", generators)
        self.assertIn("build_bus_schedule.py", generators)

    def test_lrt_source_failure_can_publish_retained_baseline(self):
        result, calls = self.run_case("lrt_source_failure")
        self.assertEqual(result.returncode, 3)
        self.assertIn("RESULT: DEGRADED", result.stdout)
        self.assertTrue(any(c["command"] == "git" and c["args"][0] == "push" for c in calls))

    def test_lrt_incomplete_rollback_refuses_before_any_publication(self):
        result, calls = self.run_case("lrt_rollback_failure", record=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Light Rail rollback incomplete; publish refused", result.stdout)
        self.assertIsNone(result.recorded_commit)
        self.assertFalse(any(c["command"] == "git" for c in calls))
        self.assertFalse(any(c["command"] == "python3" and Path(c["args"][0]).name == "publish_data.py" for c in calls))
        self.assertNotIn("RESULT: DEGRADED", result.stdout)
        self.assertNotIn("RESULT: OK", result.stdout)

    def test_guard_refusal_never_stages_or_pushes(self):
        result, calls = self.run_case("guard_refusal")
        self.assertEqual(result.returncode, 2)
        self.assertIn("publish refused", result.stdout)
        self.assert_no_git(calls, "add", "commit", "push")

    def test_publication_failures_are_red_and_do_not_claim_live_unchanged(self):
        for case, forbidden in [("add_failure", ("commit", "push")),
                                ("diff_failure", ("commit", "push")),
                                ("commit_failure", ("push",)),
                                ("push_failure", ())]:
            with self.subTest(case=case):
                result, calls = self.run_case(case)
                self.assertEqual(result.returncode, 4, result.stdout)
                self.assertIn("RESULT: PUBLISH FAILED", result.stdout)
                self.assertNotIn("RESULT: OK", result.stdout)
                self.assertNotIn("Live data unchanged", result.stdout)
                self.assertNotIn("Live data untouched", result.stdout)
                self.assert_no_git(calls, *forbidden)

    def test_failed_worktree_setup_never_touches_site_index(self):
        for case in ("mktemp_failure", "mktemp_empty", "worktree_failure", "enter_worktree_failure"):
            with self.subTest(case=case):
                result, calls = self.run_case(case)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assert_no_git(calls, "add", "diff", "commit", "push")


if __name__ == "__main__":
    unittest.main(verbosity=2)
