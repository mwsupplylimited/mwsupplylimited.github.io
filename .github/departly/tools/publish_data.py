#!/usr/bin/env python3
"""Publish the bundled transit-data files to the GitHub Pages site as an over-the-air data package.

Copies the managed datasets (the same files the build_*.py generators produce) into
mwsupplylimited-site/departly/data/, computes a SHA-256 per file, and writes manifest.json. The app
(RemoteDataManager) reads the manifest and downloads only the files whose hash changed, overriding
the bundled copies — so new routes / stations / fares ship without an App Store update.

Run after the generators. The version is content-derived, so the manifest only changes when data does.

Two pre-flight guards run before anything is copied, and BOTH abort the whole publish (the caller,
regenerate_and_publish.sh, treats a non-zero exit as "do not commit/push"): an empty-dataset guard and
a shrink guard. See the comments in main() — each is there because of a real shipped incident.
"""
import hashlib, json, os, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(ROOT, "..", "..", ".."))
BASELINE_PREFIX = ".github/departly/Departly/Resources"
RES = os.path.join(ROOT, "..", "Departly", "Resources")
# Publish target = the LIVE GitHub Pages repo's departly/data. The live https://mwsupplylimited.github.io/
# Pages site is served by mwsupplylimited/mwsupplylimited.github.io, branch main (confirmed via
# `gh api repos/.../pages`). The old default (../../mwsupplylimited-site) points at a fork with NO Pages,
# so publishing there silently never went live — hence this default points at the live clone, and argv[1]
# lets regenerate_and_publish.sh redirect it into a detached origin/main worktree for a fast-forward push.
DEFAULT_SITE = os.path.normpath(os.path.join(ROOT, "..", "..", "mwsupplylimited.github.io", "departly", "data"))
SITE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SITE
URL_BASE = "https://mwsupplylimited.github.io/departly/data/"

# (source path relative to Resources, published filename)
FILES = [(f"transit_index/{n}.json", f"{n}.json") for n in [
    "kmb_stops", "kmb_routes", "ctb_stops", "ctb_routes", "gmb_stops", "gmb_routes",
    "nlb_stops", "nlb_routes", "mtrbus_stops", "mtrbus_routes", "tram_stops", "tram_routes",
    "mtr_fares", "lrt_fares", "bus_schedule",
    # Timetable spine (build_gtfs_crosswalk.py -> build_timetable.py). OTA-refreshable like the rest,
    # so a TD feed change reaches riders without an App Store release.
    "departures", "stop_offsets", "service_calendar", "fares_od",
]] + [
    ("mtr_stations.json", "mtr_stations.json"),
    ("lrt_stations.json", "lrt_stations.json"),
    ("bus_fares.json", "bus_fares.json"),
    ("mtr_lines_and_stations.csv", "mtr_lines_and_stations.csv"),
    ("light_rail_routes_and_stops.csv", "light_rail_routes_and_stops.csv"),
]

# --- shrink guard --------------------------------------------------------------------------------
# A dataset may lose at most this share of its rows in one publish before the publish is refused.
# 10% is deliberately far from BOTH edges of the observed data: across the last 14 published commits
# the only legitimate drop of any kind was bus_fares 1482 -> 1480 (0.13%) and every other managed file
# was 0.00% volatile, while every real corruption was catastrophic (97 -> 5, 119 -> 2, 4 -> 2). So the
# threshold sits in a ~50x dead zone — it cannot false-positive on real churn, and cannot miss an
# outage-blanked file.
MAX_SHRINK_PCT = 10.0
# --- files where a ROW COUNT is the wrong invariant -----------------------------------------------
# The 10% rule was calibrated on tables with thousands of rows: across 14 published commits the only
# legitimate drop of any kind was bus_fares 1482 -> 1480 (0.13%). Applied to a count of ~20 it means
# "two rows", which is noise rather than a signal.
#
# service_calendar holds one entry per GTFS service_id ACTUALLY REFERENCED BY A TRIP, and TD
# consolidates calendars between its biweekly cuts — 21 -> 16 is ordinary churn, not corruption, and
# it blocked an otherwise-healthy publish. Its real invariants are absolute, not relative ("enough
# services to cover a week, and public-holiday dates that have not expired"), and build_timetable.py
# already asserts exactly those and aborts before writing.
#
# This is deliberately a NAMED exemption, not a blanket small-N rule: tram_routes has FOUR rows and
# its 4 -> 2 collapse is precisely what the guard exists to catch. The empty/unparseable check still
# applies to everything, exempt or not.
SHRINK_EXEMPT = {
    "service_calendar.json": "service_id count is upstream-volatile; absolute floors live in build_timetable.py",
}

# Escape hatch for a GENUINE large shrink (an agency really did retire a chunk of its network), so a
# real change can't wedge the nightly forever. `1` disables the guard for the whole run; otherwise a
# comma-separated list of published filenames exempts just those:
#   DEPARTLY_ALLOW_SHRINK=1   |   DEPARTLY_ALLOW_SHRINK=nlb_routes.json,tram_routes.json
_ALLOW_RAW = os.environ.get("DEPARTLY_ALLOW_SHRINK", "").strip()
ALLOW_ALL = _ALLOW_RAW == "1"
ALLOW = {s.strip() for s in _ALLOW_RAW.split(",") if s.strip() and s.strip() != "1"}


# --- deep counts ---------------------------------------------------------------------------------
# A top-level key count is BLIND to the second half of the tram corruption: every route present, but
# each gutted to a single stop. The timetable datasets have exactly that failure mode -- "3,383 routes,
# every `o` array truncated to [0]" passes a key count cheerfully while making every ride time wrong.
# So these files are ALSO measured by their payload, and the same 10% shrink rule is applied to both.
DEEP = {
    "stop_offsets.json":     lambda d: sum(len(v.get("o", [])) for v in d.get("r", {}).values()),
    "departures.json":       lambda d: sum(len(s.get("c", [])) + len(s.get("t", []))
                                           for r in d.get("r", {}).values() for s in r.values()),
    "service_calendar.json": lambda d: len(d.get("s", {})) + len(d.get("ph", [])),
    # Same blindness applies: "3,403 routes, every price array truncated to one entry" passes a
    # route count while making every downstream fare wrong.
    "fares_od.json":         lambda d: sum(len(v.get("o", [])) + len(v.get("x", {})) + (1 if "f" in v else 0)
                                           for v in d.get("r", {}).values()),
}


def row_count(blob, name):
    """Rows in a managed dataset, or None if it can't be measured.

    The managed files come in three shapes and all three need a comparable "size" number: JSON arrays
    (stop/route tables -> element count), JSON objects (mtr_fares / lrt_fares / bus_schedule, keyed by
    stop-pair or route -> key count) and CSV (-> non-empty line count). Byte size is deliberately NOT
    used: pretty-printing or a name-field change moves bytes without losing a single row.

    For the enveloped timetable files the count is the PAYLOAD's, not the 3-key `{v, generated, r}`
    envelope's -- which would otherwise report "3" forever and never move.
    """
    if name.endswith(".csv"):
        return len([l for l in blob.splitlines() if l.strip()])
    try:
        d = json.loads(blob)
    except Exception:
        return None                       # unparseable -> unmeasurable (main() treats this as fatal)
    if name in DEEP and isinstance(d, dict):
        return len(d.get("r", d.get("s", {})))
    return len(d) if isinstance(d, (list, dict)) else None


def deep_count(blob, name):
    """Payload size of a timetable dataset (offsets / departures / calendar entries), or None."""
    fn = DEEP.get(name)
    if fn is None:
        return None
    try:
        return fn(json.loads(blob))
    except Exception:
        return None


def git_head_count(src, name):
    """Row count of the last COMMITTED copy of the source file, or None if unavailable.

    This is a SECOND opinion on the baseline, and it is what makes the guard survive a repeat outage.
    The published copy alone is not enough: mtr_stations.json flapped 97 -> 5 three separate times in
    July 2026 and sat degraded on the live host for five days, so on the run after a corruption the
    published baseline is ITSELF 5 rows and a fresh 5-row file reads as "no change". Comparing against
    the larger of (published, last-committed) means the second bad night is refused like the first,
    and the abort message is the alarm that was missing while the bad file sat live.

    Best-effort by design — no git, not a checkout, or an untracked new dataset all degrade to None
    (the published copy then carries the check) rather than blocking a publish.
    """
    try:
        r = subprocess.run(["git", "-C", REPO, "show", f"HEAD:{BASELINE_PREFIX}/{src}"],
                           capture_output=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return row_count(r.stdout, name)


def git_head_deep(src, name):
    """Payload count of the last COMMITTED copy — the deep-count twin of `git_head_count`.

    Needed for the same dual-baseline reason: once a gutted timetable is live, the published copy is
    itself gutted and a repeat bad night reads as "no change" against it alone.
    """
    if name not in DEEP:
        return None
    try:
        r = subprocess.run(["git", "-C", REPO, "show", f"HEAD:{BASELINE_PREFIX}/{src}"],
                           capture_output=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return deep_count(r.stdout, name)


def main():
    missing = [src for src, _ in FILES if not os.path.isfile(os.path.join(RES, src))]
    if missing:
        raise SystemExit("ABORT publish: required baseline/output missing: " + ", ".join(missing))
    os.makedirs(SITE, exist_ok=True)
    # GUARD 1 — EMPTY (defense-in-depth for the 2026-07-09 incident): NEVER publish a blanked dataset. A generator
    # that failed upstream (e.g. Overpass down) could leave an empty [] / {}; publishing it plus its own
    # sha would pass the app's SHA-256 integrity check and silently drop a whole network (LRT was dropped
    # this way). Abort the entire publish if any managed file is empty, so bad data can't reach the OTA.
    #
    # GUARD 2 — SHRINK (the same failure one notch less obvious). An empty file is the easy case; the
    # expensive ones were files that came back SMALL but well-formed. build_mtr_stations.py rewrote
    # mtr_stations.json from 97 stations to 5 whenever both Overpass hosts were down (only the ~6
    # CURATED codes matched) and the nightly published it — three times in July 2026 (07-22/23/25) —
    # and the tram generator collapsed tram_stops.json 119 -> 2 / tram_routes.json 4 -> 2 on three
    # further nights that month (07-08/16/24). Each
    # of those is valid JSON with a valid sha, so the app's SHA-256 integrity check passes it happily.
    # Shipped apps survived only where a loader happened to declare a floor (BundledData.staticArray
    # minCount) — and MOST datasets go through plain `decode`, which has NO floor at all, so for those
    # a shrunken override is loaded verbatim. Refuse the publish instead of relying on the client.
    checked, drops, skipped = 0, [], []
    for src, name in FILES:
        p = os.path.join(RES, src)
        if not os.path.exists(p):
            continue
        blob = open(p, "rb").read()
        if blob.strip() in (b"[]", b"{}", b""):
            raise SystemExit(f"ABORT publish: {src} is EMPTY ({blob.strip()!r}) — refusing to publish a blanked "
                             f"dataset (would drop a whole network). Fix the generator / upstream source first.")
        new = row_count(blob, name)
        if new is None:
            raise SystemExit(f"ABORT publish: {src} is not parseable as JSON — a truncated/half-written "
                             f"file cannot be size-checked, so it must not be published. Re-run the generator.")
        prev = os.path.join(SITE, name)                      # read BEFORE the copy loop overwrites it
        prev_blob = open(prev, "rb").read() if os.path.exists(prev) else None
        base = row_count(prev_blob, name) if prev_blob is not None else None
        ref = max([c for c in (base, git_head_count(src, name)) if c], default=None)
        if not ref:
            skipped.append(name)                             # first publish / new dataset — nothing to compare
            continue
        if name in SHRINK_EXEMPT:
            print(f"  shrink-exempt {name}: {ref} -> {new} ({SHRINK_EXEMPT[name]})")
            continue
        checked += 1

        # Measure BOTH the key count and, for the enveloped timetable files, the payload — a route
        # count alone cannot see "every route present, every stop list gutted".
        pairs = [("rows", ref, new)]
        deep_new = deep_count(blob, name)
        if deep_new is not None:
            deep_ref = max([c for c in (deep_count(prev_blob, name) if prev_blob is not None else None,
                                        git_head_deep(src, name)) if c], default=None)
            if deep_ref:
                pairs.append(("entries", deep_ref, deep_new))

        for label, r_, n_ in pairs:
            floor = int(r_ * (1.0 - MAX_SHRINK_PCT / 100.0))
            if r_ > n_ >= floor:
                drops.append((f"{name} ({label})", r_, n_))  # within tolerance — log it, don't block
            if n_ < floor:
                msg = (f"{name}: {r_} -> {n_} {label} ({(1 - n_ / r_) * 100:.1f}% LOST, "
                       f"limit {MAX_SHRINK_PCT:.0f}%, floor {floor})")
                if ALLOW_ALL or name in ALLOW:
                    print(f"  ⚠️  SHRINK OVERRIDDEN via DEPARTLY_ALLOW_SHRINK — {msg}")
                else:
                    raise SystemExit(
                        f"ABORT publish: {msg}\n"
                        f"  A drop that large is almost always a failed upstream fetch, not a real network "
                        f"change (Overpass/agency outage). {SITE} left untouched — the currently published "
                        f"data stays live.\n"
                        f"  Fix the generator / retry when the source is reachable. If the shrink is GENUINE, "
                        f"re-run with DEPARTLY_ALLOW_SHRINK={name}")
    if drops:
        for name, ref, new in drops:
            print(f"  shrink-ok {name}: {ref} -> {new} ({(1 - new / ref) * 100:.2f}%)")
    print(f"== shrink check: {checked} file(s) verified vs baseline"
          + (f", {len(skipped)} without a baseline ({', '.join(skipped)})" if skipped else ""))

    entries, shas = [], []
    for src, name in FILES:
        p = os.path.join(RES, src)
        if not os.path.exists(p):
            print(f"  SKIP missing {src}"); continue
        data = open(p, "rb").read()
        sha = hashlib.sha256(data).hexdigest()
        shutil.copyfile(p, os.path.join(SITE, name))
        entries.append({"name": name, "sha": sha, "url": URL_BASE + name})
        shas.append(sha)
    version = hashlib.sha256("".join(shas).encode()).hexdigest()[:12]
    manifest = {"version": version, "files": entries}
    with open(os.path.join(SITE, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"== published {len(entries)} files to {SITE}")
    print(f"   manifest version {version}")

if __name__ == "__main__":
    main()
