"""Synthetic Pages and filesystem fixtures only; never contact GitHub or public feeds."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout

import verify_pages as pages
import hydrate_resources


COMMIT = "a" * 40


def package():
    bodies = {pages.BASE + name: (name + " fixture").encode() for name in pages.NAMES}
    rows = [{"name": name, "url": pages.BASE + name,
             "sha": hashlib.sha256(bodies[pages.BASE + name]).hexdigest()}
            for name in sorted(pages.NAMES)]
    version = hashlib.sha256("".join(r["sha"] for r in rows).encode()).hexdigest()[:12]
    manifest = {"version": version, "files": rows}
    bodies[pages.BASE + "manifest.json"] = json.dumps(manifest).encode()
    return manifest, bodies


class PagesTests(unittest.TestCase):
    def run_flow(self, statuses, bodies=None):
        expected, original = package()
        if bodies is None:
            bodies = original
        calls, time = [], [0]

        def request(method, path):
            calls.append((method, path))
            if method == "POST":
                return {"status": "queued"}
            return statuses.pop(0) if len(statuses) > 1 else statuses[0]

        def sleep(seconds):
            time[0] += seconds

        result = pages.publish_and_verify(COMMIT, expected, request, bodies.__getitem__,
                                          clock=lambda: time[0], sleep=sleep, timeout=30, interval=5)
        return result, calls

    def test_build_pending_then_exact_commit_and_24_hashes(self):
        result, calls = self.run_flow([{"status": "queued"}, {"status": "building"},
                                      {"status": "built", "commit": COMMIT}])
        self.assertEqual(result["verifiedFiles"], 24)
        self.assertEqual(result["commit"], COMMIT)
        self.assertEqual(calls[0], ("POST", pages.PAGES_PATH))
        self.assertEqual(len(calls), 4)

    def test_unchanged_content_still_requests_and_verifies_pages(self):
        result, calls = self.run_flow([{"status": "built", "commit": COMMIT}])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(calls, [("POST", pages.PAGES_PATH), ("GET", pages.PAGES_PATH + "/latest")])

    def test_terminal_pages_failure_does_not_verify_served_data(self):
        for build in ({"status": "errored"}, {"status": "errored", "commit": COMMIT}):
            with self.subTest(build=build), self.assertRaisesRegex(RuntimeError, "Pages build failed"):
                self.run_flow([build])

    def test_older_built_commit_can_transition_to_recorded_commit(self):
        result, calls = self.run_flow([{"status": "built", "commit": "b" * 40},
                                      {"status": "queued"}, {"status": "built", "commit": COMMIT}])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(calls), 4)

    def test_other_site_commit_times_out_without_force(self):
        with self.assertRaisesRegex(TimeoutError, "recorded publication commit"):
            self.run_flow([{"status": "built", "commit": "b" * 40}])

    def test_older_failed_build_can_transition_to_recorded_build(self):
        result, _ = self.run_flow([{"status": "errored", "commit": "b" * 40},
                                  {"status": "queued"}, {"status": "built", "commit": COMMIT}])
        self.assertEqual(result["status"], "PASS")

    def test_permanent_api_error_propagates(self):
        expected, bodies = package()
        for denied_method in ("POST", "GET"):
            with self.subTest(method=denied_method):
                def request(method, path):
                    if method == denied_method:
                        raise urllib.error.HTTPError("https://api.github.com" + path, 403,
                                                     "Forbidden", {}, None)
                    return {"status": "queued"}
                with self.assertRaises(urllib.error.HTTPError):
                    pages.publish_and_verify(COMMIT, expected, request, bodies.__getitem__)

    def test_build_wait_is_bounded(self):
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            self.run_flow([{"status": "building"}])

    def test_stale_manifest_and_corrupt_file_do_not_pass(self):
        for which in ("manifest", "dataset"):
            with self.subTest(which=which):
                _, bodies = package()
                url = pages.BASE + ("manifest.json" if which == "manifest" else "kmb_stops.json")
                bodies[url] = b"{}" if which == "manifest" else b"corrupted"
                with self.assertRaises(TimeoutError):
                    self.run_flow([{"status": "built", "commit": COMMIT}], bodies)

    def test_manifest_rejects_missing_duplicate_external_and_wrong_version(self):
        for defect in ("missing", "duplicate", "external", "version"):
            with self.subTest(defect=defect):
                data, _ = package()
                if defect == "missing":
                    data["files"].pop()
                elif defect == "duplicate":
                    data["files"][1] = data["files"][0]
                elif defect == "external":
                    data["files"][0]["url"] = "https://unrelated.example/data"
                else:
                    data["version"] = "0" * 12
                with self.assertRaises(ValueError):
                    pages.validate_manifest(json.dumps(data).encode())


class BaselineTests(unittest.TestCase):
    def fixture(self, name, baseline, live, generated):
        temp = tempfile.TemporaryDirectory(prefix="departly-public-baseline-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        tools = root / ".github/departly/tools"
        resources = root / ".github/departly/Departly/Resources"
        tools.mkdir(parents=True)
        resources.mkdir(parents=True)
        shutil.copy2(Path(__file__).with_name("publish_data.py"), tools / "publish_data.py")
        (resources / name).write_text(json.dumps(baseline))
        for command in (["git", "init", "-q", str(root)],
                        ["git", "-C", str(root), "add", "--", ".github/departly/Departly/Resources/" + name],
                        ["git", "-C", str(root), "-c", "user.name=Fixture",
                         "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Synthetic baseline"]):
            subprocess.run(command, capture_output=True, check=True)
        spec = importlib.util.spec_from_file_location("fixture_publisher", tools / "publish_data.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(Path(module.REPO), root)
        module.FILES = [(name, name)]
        site = root / "departly/data"
        site.mkdir(parents=True)
        (site / name).write_text(json.dumps(live))
        (resources / name).write_text(json.dumps(generated))
        module.SITE = str(site)
        return module, site, resources

    def test_exact_layout_reads_committed_baseline_and_blocks_repeated_corruption(self):
        module, site, _ = self.fixture("mtr_stations.json", [{}] * 100, [{}] * 5, [{}] * 5)
        self.assertEqual(module.git_head_count("mtr_stations.json", "mtr_stations.json"), 100)
        before = (site / "mtr_stations.json").read_bytes()
        with self.assertRaisesRegex(SystemExit, "100 -> 5"):
            module.main()
        self.assertEqual((site / "mtr_stations.json").read_bytes(), before)
        self.assertFalse((site / "manifest.json").exists())

    def test_deep_baseline_blocks_same_route_count_with_gutted_offsets(self):
        module, _, _ = self.fixture("stop_offsets.json", {"r": {"R": {"o": list(range(20))}}},
                                   {"r": {"R": {"o": [0]}}}, {"r": {"R": {"o": [0]}}})
        self.assertEqual(module.git_head_deep("stop_offsets.json", "stop_offsets.json"), 20)
        with self.assertRaisesRegex(SystemExit, "20 -> 1 entries"):
            module.main()

    def test_missing_required_output_refuses_before_any_site_write(self):
        module, site, resources = self.fixture("mtr_stations.json", [{}] * 100, [{}] * 100, [{}] * 100)
        (resources / "mtr_stations.json").unlink()
        before = (site / "mtr_stations.json").read_bytes()
        with self.assertRaisesRegex(SystemExit, "required baseline/output missing"):
            module.main()
        self.assertEqual((site / "mtr_stations.json").read_bytes(), before)
        self.assertFalse((site / "manifest.json").exists())

    def test_ordinary_publish_retains_guard_and_manifest_hash(self):
        module, site, _ = self.fixture("mtr_stations.json", [{}] * 100, [{}] * 100, [{}] * 100)
        with redirect_stdout(io.StringIO()):
            module.main()
        actual = json.loads((site / "manifest.json").read_text())
        self.assertEqual(actual["files"][0]["sha"], hashlib.sha256((site / "mtr_stations.json").read_bytes()).hexdigest())


class WorkflowTests(unittest.TestCase):
    def workflow(self):
        return (Path(__file__).resolve().parents[2] / "workflows/departly-nightly-data.yml").read_text()

    def body(self, name):
        import textwrap
        lines = self.workflow().splitlines()
        start = next(i for i, line in enumerate(lines) if line.strip() == "- name: " + name)
        start = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
        result = []
        for line in lines[start + 1:]:
            if line and len(line) - len(line.lstrip()) <= 8:
                break
            result.append(line)
        return textwrap.dedent("\n".join(result))

    def test_public_workflow_permission_trigger_and_fresh_gtfs_contract(self):
        workflow = self.workflow()
        self.assertIn('"on":\n  schedule:', workflow)
        self.assertIn("  workflow_dispatch:", workflow)
        for forbidden in ("pull_request", "workflow_call", "repository_dispatch", "SITE_PUSH_TOKEN",
                          "self-hosted", "actions/deploy-pages", "id-token:", "administration:"):
            self.assertNotIn(forbidden, workflow)
        self.assertIn("runs-on: ubuntu-latest", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("pages: write", workflow)
        self.assertIn("GH_TOKEN: ${{ github.token }}", workflow)
        self.assertIn("path: .github/departly/tools/.transit_cache/*.json", workflow)
        self.assertNotIn("path: .github/departly/tools/.transit_cache\n", workflow)
        self.assertIn("id: pages", workflow)
        self.assertNotIn("continue-on-error", workflow)
        self.assertIn('if [ "$PAGES_OUTCOME" = "success" ]', workflow)
        self.assertIn("if: steps.cache_policy.outputs.enabled == 'true'", workflow)

    def test_cache_error_unknown_and_paid_limit_skip_cache_action(self):
        body = self.body("Verify cache is limited to the free allowance")
        for value, rc, expected in [("10", 0, "true"), ("1", 0, "true"), ("11", 0, "false"),
                                    ("100", 0, "false"), ("", 0, "false"), ("null", 0, "false"),
                                    ("10", 1, "false"), ("402", 1, "false")]:
            with self.subTest(value=value, rc=rc), tempfile.TemporaryDirectory() as td:
                output = Path(td) / "output"
                result = subprocess.run(["/bin/bash", "-e"], text=True, capture_output=True,
                                        input='gh() { echo "$TEST_VALUE"; return "$TEST_RC"; }\n' + body,
                                        env={"PATH": "/usr/bin:/bin", "TEST_VALUE": value,
                                             "TEST_RC": str(rc), "GITHUB_OUTPUT": str(output)})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_text().strip(), "enabled=" + expected)

    def test_pages_error_is_nonzero_and_never_reported_ok(self):
        body = self.body("Rebuild existing Pages site and verify all 24 served datasets")
        result = subprocess.run(["/bin/bash", "-e"], input="python3() { return 7; }\n" + body,
                                text=True, capture_output=True, env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": "/unused"})
        self.assertEqual(result.returncode, 7)
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "summary"
            result = subprocess.run(["/bin/bash", "-e"], input=self.body("Summary"), text=True,
                                    capture_output=True, env={"PATH": "/usr/bin:/bin", "PUBLISH_RC": "0",
                                                             "PAGES_OUTCOME": "failure", "GITHUB_STEP_SUMMARY": str(output)})
            self.assertEqual(result.returncode, 0)
            self.assertIn("FAILED", output.read_text())
            self.assertNotIn("OK —", output.read_text())


class HydrationTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="departly-hydration-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        site, resources = root / "departly/data", root / ".github/departly/Departly/Resources"
        site.mkdir(parents=True)
        resources.mkdir(parents=True)
        manifest, _ = package()
        for row in manifest["files"]:
            body = json.dumps([{"latest": True}] * 100).encode()
            (site / row["name"]).write_bytes(body)
            row["sha"] = hashlib.sha256(body).hexdigest()
        manifest["version"] = hashlib.sha256("".join(r["sha"] for r in manifest["files"]).encode()).hexdigest()[:12]
        (site / "manifest.json").write_text(json.dumps(manifest))
        return root, site, resources

    def test_failed_generator_retains_latest_public_data_and_frozen_guard(self):
        root, site, resources = self.fixture()
        tools = root / ".github/departly/tools"
        tools.mkdir()
        shutil.copy2(Path(__file__).with_name("publish_data.py"), tools / "publish_data.py")
        (resources / "mtr_stations.json").write_text(json.dumps([{"seed": True}] * 100))
        for command in (["git", "init", "-q", str(root)],
                        ["git", "-C", str(root), "add", "--", ".github/departly/Departly/Resources/mtr_stations.json"],
                        ["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-qm", "Synthetic frozen baseline"]):
            subprocess.run(command, check=True, capture_output=True)
        latest = (site / "mtr_stations.json").read_bytes()
        self.assertEqual(hydrate_resources.hydrate(site, resources)["hydratedFiles"], 24)
        # A failed generator leaves its output untouched, as the real shell suite separately verifies.
        self.assertEqual(subprocess.run(["/bin/bash", "-c", "exit 1"]).returncode, 1)
        spec = importlib.util.spec_from_file_location("hydrated_publisher", tools / "publish_data.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.SITE, module.FILES = str(site), [("mtr_stations.json", "mtr_stations.json")]
        with redirect_stdout(io.StringIO()):
            module.main()
        self.assertEqual((site / "mtr_stations.json").read_bytes(), latest)
        self.assertEqual(module.git_head_count("mtr_stations.json", "mtr_stations.json"), 100)
        for path in (site / "mtr_stations.json", resources / "mtr_stations.json"):
            path.write_text(json.dumps([{}] * 5))
        with self.assertRaisesRegex(SystemExit, "100 -> 5"):
            module.main()

    def test_corrupt_live_input_refuses_before_overwriting_any_working_file(self):
        _, site, resources = self.fixture()
        sentinel = resources / "mtr_stations.json"
        sentinel.write_text("preserved working seed")
        (site / "tram_stops.json").write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            hydrate_resources.hydrate(site, resources)
        self.assertEqual(sentinel.read_text(), "preserved working seed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
