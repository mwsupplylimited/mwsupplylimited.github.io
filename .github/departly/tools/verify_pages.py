#!/usr/bin/env python3
"""Request the existing legacy Pages build and verify the exact public OTA package.

The CLI is Actions-only. Unit tests inject all I/O; no personal access token is used.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request

REPOSITORY = "mwsupplylimited/mwsupplylimited.github.io"
API = "https://api.github.com"
BASE = "https://mwsupplylimited.github.io/departly/data/"
PAGES_PATH = f"/repos/{REPOSITORY}/pages/builds"
NAMES = frozenset([
    "kmb_stops.json", "kmb_routes.json", "ctb_stops.json", "ctb_routes.json",
    "gmb_stops.json", "gmb_routes.json", "nlb_stops.json", "nlb_routes.json",
    "mtrbus_stops.json", "mtrbus_routes.json", "tram_stops.json", "tram_routes.json",
    "mtr_fares.json", "lrt_fares.json", "bus_schedule.json", "departures.json",
    "stop_offsets.json", "service_calendar.json", "fares_od.json", "mtr_stations.json",
    "lrt_stations.json", "bus_fares.json", "mtr_lines_and_stations.csv",
    "light_rail_routes_and_stops.csv",
])


def validate_manifest(data):
    manifest = json.loads(data)
    if not isinstance(manifest, dict) or not re.fullmatch(r"[0-9a-f]{12}", str(manifest.get("version", ""))):
        raise ValueError("Invalid content-derived manifest version")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != len(NAMES):
        raise ValueError("Expected exactly 24 datasets")
    names = []
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("Invalid dataset entry")
        name = row.get("name")
        if name not in NAMES or row.get("url") != BASE + name:
            raise ValueError("Dataset name or public URL outside allowlist")
        if not isinstance(row.get("sha"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["sha"]):
            raise ValueError("Invalid dataset SHA256")
        names.append(name)
    if set(names) != NAMES:
        raise ValueError("Missing or duplicate dataset")
    version = hashlib.sha256("".join(row["sha"] for row in files).encode()).hexdigest()[:12]
    if manifest["version"] != version:
        raise ValueError("Manifest version does not match its content hashes")
    return manifest


def verify_live(expected, fetch):
    actual = validate_manifest(fetch(BASE + "manifest.json"))
    if actual != expected:
        raise ValueError("Served manifest differs from the published commit")
    for row in expected["files"]:
        if hashlib.sha256(fetch(row["url"])).hexdigest() != row["sha"]:
            raise ValueError("Served dataset hash mismatch: " + row["name"])
    return {"version": actual["version"], "verifiedFiles": len(actual["files"])}


def publish_and_verify(commit, expected, request, fetch, *, clock=time.monotonic,
                       sleep=time.sleep, timeout=600, interval=15):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid publication commit")
    expected = validate_manifest(json.dumps(expected).encode())
    deadline = clock() + timeout
    response = request("POST", PAGES_PATH)
    if response.get("status") not in {"queued", "building", "built"}:
        raise ValueError("Pages did not accept a build request")
    last = "Pages build pending"
    while clock() < deadline:
        build = request("GET", PAGES_PATH + "/latest")
        status = build.get("status")
        if status in {"errored", "failed"}:
            previous = build.get("commit")
            if isinstance(previous, str) and re.fullmatch(r"[0-9a-f]{40}", previous) and previous != commit:
                last = "Pages still exposes an older failed build"
            else:
                raise RuntimeError("Pages build failed; content may already be committed")
        elif status == "built":
            if build.get("commit") != commit:
                # Immediately after POST, latest can still expose the previous completed build.
                last = "Pages has not built the recorded publication commit"
            else:
                try:
                    result = verify_live(expected, fetch)
                    if clock() >= deadline:
                        raise TimeoutError("Pages verification deadline exceeded")
                    return {"commit": commit, "status": "PASS", **result}
                except ValueError as error:
                    # A completed Pages build can take time to reach every CDN edge.
                    last = str(error)
        elif status not in {"queued", "building"}:
            raise RuntimeError("Unknown Pages build status")
        sleep(min(interval, max(0, deadline - clock())))
    raise TimeoutError("Pages verification timed out: " + last)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit-file", required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
        raise SystemExit("Refusing publication verification outside the exact public-site Actions job")
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Missing ephemeral workflow token")
    with open(args.commit_file) as stream:
        commit = stream.read().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit("Invalid publication commit record")
    blob = subprocess.run(["git", "show", f"{commit}:departly/data/manifest.json"],
                          capture_output=True, check=True).stdout
    expected = validate_manifest(blob)
    deadline = time.monotonic() + 600

    def read(request):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Pages verification deadline exceeded")
        with urllib.request.urlopen(request, timeout=min(20, remaining)) as response:
            body = response.read(32 * 1024 * 1024 + 1)
        if len(body) > 32 * 1024 * 1024:
            raise ValueError("Response exceeds verification size limit")
        return body

    def request(method, path):
        if path not in {PAGES_PATH, PAGES_PATH + "/latest"}:
            raise ValueError("API operation outside Pages build scope")
        req = urllib.request.Request(API + path, method=method, headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Departly-nightly-verifier",
        })
        return json.loads(read(req))

    def fetch(url):
        # The token is deliberately never attached to public Pages requests.
        allowed = {BASE + "manifest.json"} | {BASE + name for name in NAMES}
        if url not in allowed:
            raise ValueError("Public request outside dataset allowlist")
        return read(urllib.request.Request(url, headers={"User-Agent": "Departly-nightly-verifier"}))

    print(json.dumps(publish_and_verify(commit, expected, request, fetch), sort_keys=True))


if __name__ == "__main__":
    main()
