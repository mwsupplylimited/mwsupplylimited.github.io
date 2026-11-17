#!/bin/bash
# Regenerate Departly's bundled transit datasets from the live open-data sources, then publish them
# to the GitHub Pages OTA host so the app picks up new routes / stations / fares without an App Store
# release. Run nightly (see the scheduled task). Generators are best-effort — a failed one leaves the
# last-good file in place, and publish_data.py always publishes the current Resources state.
set -u
cd "$(dirname "$0")" || exit 1

# Generator failures are non-fatal BY DESIGN (a failed one leaves the last-good file in place), but
# they must still be COUNTED, because "some generators failed" and "all generators succeeded" are very
# different nights and the exit code is the only thing a scheduler sees.
GEN_FAILS=0
run() {
  echo "→ $1"
  python3 "$1"
  local rc=$?
  if [ "$1" = "build_lrt_stations.py" ] && [ "$rc" -eq 2 ]; then
    # A failed two-file rollback means the working pair cannot be called last-good.
    # Refuse before any site worktree, commit or push; retained backups permit recovery.
    echo "RESULT: FAILED — Light Rail rollback incomplete; publish refused. Live data unchanged."
    exit 2
  fi
  if [ "$rc" -ne 0 ]; then
    echo "  WARN: $1 failed (keeping previous data)"
    GEN_FAILS=$((GEN_FAILS + 1))
  fi
}

run build_transit_index.py     # KMB / CTB / GMB / NLB + MTR Bus

# The timetable spine runs IMMEDIATELY after the route index, and this ordering is load-bearing.
# stop_offsets.json / fares_od.json are indexed by POSITION in the app's stop sequences, so if the
# index regenerates and these do not, every offset and every fare silently shifts by a stop -- valid
# JSON, valid sha, wrong answer, exactly the failure class CLAUDE.md 5 documents. Each route also
# carries a fingerprint the client re-checks at load, so a skew degrades to "no timetable" rather
# than to wrong numbers; keeping them adjacent here means that fallback is a backstop, not the norm.
# Unlike `run`, a crosswalk failure SKIPS the dependent generators rather than letting them build on
# a stale mapping.
echo "→ build_gtfs_crosswalk.py"
if python3 build_gtfs_crosswalk.py; then
  run build_timetable.py       # departures / stop_offsets / service_calendar (day-types + holidays)
  run build_fares_od.py        # board->alight sectional fares (fixes the origin-fare overcharge)
else
  echo "  WARN: build_gtfs_crosswalk.py failed — skipping timetable generators (keeping previous data)"
  GEN_FAILS=$((GEN_FAILS + 1))
fi

run build_mtr_stations.py      # MTR heavy-rail stations
run build_lrt_stations.py      # Light Rail stations
run build_tram_stations.py     # Hong Kong Tramways
run build_rail_fares.py        # MTR + Light Rail fares (per-category Octopus)
run make_fares.py              # KMB / Citybus / LWB bus fares (was missing → bus fares went stale)
run build_bus_schedule.py      # GTFS scheduled headway / first-departure (was missing → schedule went stale)
# Publish to the LIVE GitHub Pages repo: mwsupplylimited/mwsupplylimited.github.io, branch main
# (confirmed via `gh api repos/.../pages` as the live source of https://mwsupplylimited.github.io/).
# The old target (../../mwsupplylimited-site) pushed to a fork with NO Pages, so OTA updates silently
# never went live (the 2026-07-09 divergence). This repo is SHARED by many apps, so publish SURGICALLY
# from a detached origin/main worktree: fetch → write only departly/data → fast-forward push. This never
# touches local main (no divergence/stuck-push) and never force-pushes (can't clobber other apps' work).
TOOLS="$(pwd)"
# DEPARTLY_SITE_REPO lets a CI runner point at a clone it made itself; unset, this resolves the sibling
# working copy exactly as before, so running the script by hand is byte-identical to what it always did.
# ONE script, one code path — a separate CI copy of this logic would drift from the local one, and the
# guards below are the only thing standing between an upstream outage and a corrupted live dataset.
SITE_REPO="${DEPARTLY_SITE_REPO:-$(cd ../../mwsupplylimited.github.io 2>/dev/null && pwd)}"
if [ -z "$SITE_REPO" ] || [ ! -d "$SITE_REPO/.git" ]; then
  echo "ERROR: site repo not found (looked at '${SITE_REPO:-../../mwsupplylimited.github.io}') — NOT publishing."
  exit 1
fi
cd "$SITE_REPO" || exit 1
git fetch origin --quiet || { echo "ERROR: git fetch failed — NOT publishing."; exit 1; }
WT="$(mktemp -d)" || { echo "ERROR: cannot create a temporary worktree — NOT publishing."; exit 1; }
[ -n "$WT" ] && [ -d "$WT" ] || { echo "ERROR: invalid temporary worktree — NOT publishing."; exit 1; }
if ! git worktree add --detach "$WT" origin/main --quiet; then echo "ERROR: worktree add failed — NOT publishing."; rm -rf "$WT"; exit 1; fi
# publish_data.py materializes departly/data + manifest INTO the worktree. Its pre-flight guards abort
# non-zero — and write nothing — if any managed file is blank, unparseable, or has LOST >10% of its rows
# versus the larger of (currently published copy, last committed copy), so a dataset gutted by an
# upstream outage never gets committed/pushed. The worktree is checked out from origin/main, so that
# baseline is exactly what is live right now. Genuine large shrink: re-run with DEPARTLY_ALLOW_SHRINK.
PUBLISH_REFUSED=0
PUBLISH_FAILED=0
if python3 "$TOOLS/publish_data.py" "$WT/departly/data"; then
  # Confine all index/commit/push operations to the owned detached worktree. Every failure must
  # survive cleanup; a rejected or interrupted push is not evidence that live data is unchanged.
  (
    cd "$WT" || { echo "ERROR: cannot enter publication worktree."; exit 4; }
    git add -- departly/data || { echo "ERROR: could not stage OTA data."; exit 4; }
    git diff --cached --quiet -- departly/data
    diff_rc=$?
    case "$diff_rc" in
      0) echo "No data changes to publish." ;;
      1)
        git commit -m "Auto-update Departly transit data ($(date +%Y-%m-%d))" \
          || { echo "ERROR: OTA commit failed."; exit 4; }
        git push origin HEAD:main \
          || { echo "ERROR: OTA push failed; inspect the remote before retrying."; exit 4; }
        echo "Published OTA data update to mwsupplylimited/main."
        ;;
      *) echo "ERROR: could not inspect staged OTA changes."; exit 4 ;;
    esac
    if [ -n "${DEPARTLY_PUBLISHED_COMMIT_FILE:-}" ]; then
      git rev-parse HEAD > "$DEPARTLY_PUBLISHED_COMMIT_FILE" \
        || { echo "ERROR: could not record publication commit."; exit 4; }
    fi
  ) || PUBLISH_FAILED=1
else
  echo "publish_data.py failed (empty / unparseable / shrink guard tripped?) — NOT publishing."
  PUBLISH_REFUSED=1
fi
git worktree remove --force "$WT" 2>/dev/null; rm -rf "$WT" 2>/dev/null

# EXIT CODE MUST REFLECT THE REAL OUTCOME.
#
# This script used to end on `rm -rf … 2>/dev/null`, which always succeeds — so a night where the
# shrink guard REFUSED to publish exited 0 and read as healthy. That is exactly the failure CLAUDE.md
# §6 warns about ("a green exit code does not mean the artifact is correct"), and it was sitting in the
# very script whose guards exist because a corrupted dataset once sat live for five days. Anything that
# schedules this — launchd, cron, CI — can only see the exit code, so the guard was mute where it
# mattered most.
if [ "$PUBLISH_REFUSED" -ne 0 ]; then
  echo "RESULT: FAILED — publish refused by the pre-flight guards. Live data unchanged; investigate the generator/source before retrying."
  exit 2
fi
if [ "$PUBLISH_FAILED" -ne 0 ]; then
  echo "RESULT: PUBLISH FAILED — publication did not complete successfully. Remote state may be uncertain after a push failure; inspect it before retrying."
  exit 4
fi
if [ "$GEN_FAILS" -gt 0 ]; then
  echo "RESULT: DEGRADED — $GEN_FAILS generator(s) failed; published the remaining datasets with last-good copies for the rest."
  exit 3
fi
echo "RESULT: OK — all generators succeeded and the publish completed."
exit 0
