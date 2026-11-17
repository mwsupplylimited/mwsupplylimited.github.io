#!/usr/bin/env python3
"""Join the app's bundled route/stop index to the HK Transport Department GTFS feed.

WHY THIS EXISTS
The TD GTFS is the only published source of HK bus SCHEDULES (per-trip departures, real headway
bands, day-types and public holidays), but it uses its OWN stop ids: overlap with the app's
operator stop ids (KMB 16-hex, CTB/GMB/NLB numeric) is EXACTLY ZERO. Nothing joins them but
geometry. This script builds that join once, and `build_timetable.py` / `build_fares_od.py`
consume it, so the expensive alignment is done in one place with one QA gate.

WHY SEQUENCE ALIGNMENT, NOT NEAREST-NEIGHBOUR
Nearest-GTFS-stop is tempting (measured: median 13 m, p90 32 m, 96 % within 50 m) but UNSAFE on
its own -- the two direction poles of the same road sit ~20-30 m apart, so a naive join silently
maps outbound stops onto the inbound pole. We instead align the whole ORDERED sequence with a
monotone Needleman-Wunsch, which cannot cross itself and therefore cannot swap directions.

DIRECTION POLARITY IS DETECTED, NEVER ASSUMED
GTFS direction 1/2 does not map consistently onto the app's outbound/inbound: measured on a
300-route KMB sample the polarity map is (O,1)=161, (I,2)=116, (I,1)=15, (O,2)=8 -- 7.7 % invert.
So every candidate is scored in BOTH GTFS directions and the better one wins.

GMB IS MATCHED BY GEOMETRY, NOT BY NUMBER
Green-minibus route numbers COLLIDE across regions (HKI/KLN/NT all have a "1"), which is exactly
why `build_bus_schedule.py` excludes GMB. Every same-number GTFS candidate is therefore scored and
the best alignment wins, rather than trusting the number.

OUTPUT  tools/.transit_cache/gtfs_crosswalk.json   (gitignored -- a build artifact, not shipped)
  { "generated": "...", "stats": {...},
    "map": { "<appRouteKey>": {"g": "<gtfsRouteId>-<dir>", "score": 0.98,
                               "idx": [gtfsSeqIdx | -1, ...]   # one entry per APP stop, in order
                              } } }
`idx` is what lets the timetable generator carry a GTFS per-trip time onto an APP stop position.
"""
import csv, io, json, math, os, sys, urllib.request, zipfile
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
IDX = os.path.join(HERE, "..", "Departly", "Resources", "transit_index")
CACHE = os.path.join(HERE, ".transit_cache")
OUT = os.path.join(CACHE, "gtfs_crosswalk.json")
STATS = os.path.join(CACHE, "gtfs_crosswalk_stats.json")
GTFS_URL = "https://static.data.gov.hk/td/pt-headway-en/gtfs.zip"
GTFS_ZIP = os.path.join(CACHE, "td_gtfs.zip")

# Alignment tuning. ACCEPT is what counts as "the same physical stop"; GAP is the cost of skipping
# a stop on either side. GAP > ACCEPT so the aligner prefers matching over skipping, but a genuinely
# missing stop (route variants differ slightly between the two sources) is still cheaper than a
# nonsense long-distance pairing.
ACCEPT_M = 120.0
GAP_M = 250.0
MIN_SCORE = 0.70          # below this the route keeps today's headway-band treatment
GATE_MATCHED = 0.85       # abort the build if fewer than this fraction align (see __main__)

# App agency -> the GTFS agency_id values that can serve it. Joint routes appear under a combined
# agency_id, so both operators must accept them.
AGENCIES = {
    "KMB":  {"KMB", "LWB", "KMB+CTB", "LWB+CTB"},
    "CTB":  {"CTB", "KMB+CTB", "LWB+CTB"},
    "GMB":  {"GMB"},
    "NLB":  {"NLB"},
    "MTRB": {"LRTFeeder"},
}
# (app routes file, app stops file, app agency)
SOURCES = [
    ("kmb_routes.json",    "kmb_stops.json",    "KMB"),
    ("ctb_routes.json",    "ctb_stops.json",    "CTB"),
    ("gmb_routes.json",    "gmb_stops.json",    "GMB"),
    ("nlb_routes.json",    "nlb_stops.json",    "NLB"),
    ("mtrbus_routes.json", "mtrbus_stops.json", "MTRB"),
]


def hav(a, b):
    """Metres between (lat, lng) pairs."""
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def load_gtfs():
    """Download (and cache) the feed, returning route-dir -> ordered [(stop_id, lat, lng)]."""
    os.makedirs(CACHE, exist_ok=True)
    if not os.path.exists(GTFS_ZIP):
        print("downloading", GTFS_URL)
        blob = urllib.request.urlopen(GTFS_URL, timeout=300).read()
        with open(GTFS_ZIP, "wb") as f:
            f.write(blob)
        print(f"  cached {len(blob)/1e6:.1f} MB -> {GTFS_ZIP}")
    else:
        print(f"using cached feed {GTFS_ZIP} ({os.path.getsize(GTFS_ZIP)/1e6:.1f} MB)")

    z = zipfile.ZipFile(GTFS_ZIP)

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

    coord = {}
    for s in rows("stops.txt"):
        try:
            coord[s["stop_id"]] = (float(s["stop_lat"]), float(s["stop_lon"]))
        except (TypeError, ValueError):
            pass

    routes = {}                       # route_id -> (agency_id, short_name)
    for r in rows("routes.txt"):
        routes[r["route_id"]] = ((r.get("agency_id") or "").strip(),
                                 (r.get("route_short_name") or "").strip())

    # Every (route_id, direction) has exactly ONE stop pattern in this feed (verified: 3,637
    # route-dirs -> 3,637 distinct patterns), so one representative trip per route-dir is enough.
    seen, seq = set(), {}
    cur, rows_ = None, []
    for st in rows("stop_times.txt"):
        tid = st["trip_id"]
        if tid != cur:
            if cur is not None:
                key = "-".join(cur.split("-")[:2])
                if key not in seen:
                    seen.add(key)
                    seq[key] = [(sid, *coord[sid]) for sid in rows_ if sid in coord]
            cur, rows_ = tid, []
        rows_.append(st["stop_id"])
    if cur is not None:
        key = "-".join(cur.split("-")[:2])
        if key not in seen:
            seq[key] = [(sid, *coord[sid]) for sid in rows_ if sid in coord]

    by_name = defaultdict(list)       # (agency_id, short_name) -> [route-dir keys]
    for k in seq:
        rid = k.split("-")[0]
        if rid in routes:
            by_name[routes[rid]].append(k)
    print(f"GTFS: {len(coord)} stops, {len(routes)} routes, {len(seq)} route-directions")
    return seq, by_name


def align(app_pts, g_pts):
    """Monotone Needleman-Wunsch over two ordered coordinate lists.

    Returns (matched_fraction, idx) where idx[i] is the GTFS index paired with app stop i, or -1.
    Monotonicity is the whole point: it makes a direction-inverted pairing structurally impossible,
    which a nearest-neighbour join cannot guarantee (opposite-direction poles are ~25 m apart).
    """
    n, m = len(app_pts), len(g_pts)
    if n == 0 or m == 0:
        return 0.0, []
    # dp[i][j] = best cost aligning the first i app stops with the first j GTFS stops.
    INF = float("inf")
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    bt = [[0] * (m + 1) for _ in range(n + 1)]      # 0 = match, 1 = skip app, 2 = skip gtfs
    dp[0][0] = 0.0
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + GAP_M
        bt[i][0] = 1
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + GAP_M
        bt[0][j] = 2
    for i in range(1, n + 1):
        ai = app_pts[i - 1]
        row, prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            d = hav(ai, g_pts[j - 1])
            c_match = prev[j - 1] + (d if d <= ACCEPT_M else GAP_M * 2)
            c_skip_a = prev[j] + GAP_M
            c_skip_g = row[j - 1] + GAP_M
            best = c_match
            k = 0
            if c_skip_a < best:
                best, k = c_skip_a, 1
            if c_skip_g < best:
                best, k = c_skip_g, 2
            row[j], bt[i][j] = best, k

    idx = [-1] * n
    i, j, matched = n, m, 0
    while i > 0 or j > 0:
        k = bt[i][j]
        if i > 0 and j > 0 and k == 0:
            if hav(app_pts[i - 1], g_pts[j - 1]) <= ACCEPT_M:
                idx[i - 1] = j - 1
                matched += 1
            i, j = i - 1, j - 1
        elif i > 0 and (k == 1 or j == 0):
            i -= 1
        else:
            j -= 1
    return matched / n, idx


def main():
    seq, by_name = load_gtfs()
    out, stats = {}, defaultdict(lambda: defaultdict(int))
    worst = []

    for rfile, sfile, agency in SOURCES:
        routes = json.load(open(os.path.join(IDX, rfile)))
        stops = {s["id"]: (s["lat"], s["lng"]) for s in json.load(open(os.path.join(IDX, sfile)))}
        wanted = AGENCIES[agency]
        for rv in routes:
            key = f'{agency}|{rv["route"]}|{rv["dir"]}|{rv.get("service","")}'
            app_pts = [stops[s] for s in rv["stops"] if s in stops]
            stats[agency]["variants"] += 1
            if len(app_pts) < 2:
                stats[agency]["too_short"] += 1
                continue
            cands = [k for (ag, num), keys in by_name.items()
                     if ag in wanted and num == rv["route"] for k in keys]
            if not cands:
                stats[agency]["no_gtfs_route"] += 1
                continue
            # Score EVERY candidate in the feed's own direction split -- polarity is detected here,
            # never assumed, because 7.7 % of route-dirs invert.
            best = (0.0, None, None)
            for c in cands:
                g_pts = [(la, ln) for _, la, ln in seq[c]]
                score, idx = align(app_pts, g_pts)
                if score > best[0]:
                    best = (score, c, idx)
            score, gkey, idx = best
            if gkey is None or score < MIN_SCORE:
                stats[agency]["weak"] += 1
                worst.append((score, key, gkey))
                continue
            stats[agency]["matched"] += 1
            stats[agency]["ge90" if score >= 0.90 else "ge70"] += 1
            out[key] = {"g": gkey, "score": round(score, 3), "idx": idx}

    total = sum(s["variants"] for s in stats.values())
    matched = sum(s["matched"] for s in stats.values())
    print(f"\n{'agency':<8}{'variants':>10}{'matched':>9}{'>=90%':>8}{'70-90%':>8}{'weak':>7}{'noRoute':>9}")
    for ag in stats:
        s = stats[ag]
        print(f"{ag:<8}{s['variants']:>10}{s['matched']:>9}{s['ge90']:>8}{s['ge70']:>8}"
              f"{s['weak']:>7}{s['no_gtfs_route']:>9}")
    share = matched / total if total else 0.0
    print(f"\nmatched {matched}/{total} = {share:.1%}")

    worst.sort()
    if worst:
        print("weakest 10 (kept on today's headway-band treatment):")
        for sc, k, g in worst[:10]:
            print(f"   {sc:.2f}  {k}  -> {g}")

    # ---- QA gates. A silently-degraded crosswalk would corrupt EVERY downstream time and fare,
    # so refuse to emit rather than let the next stage build on it (CLAUDE.md 5: integrity != plausibility).
    prev = None
    if os.path.exists(STATS):
        try:
            prev = json.load(open(STATS)).get("share")
        except Exception:
            prev = None
    if share < GATE_MATCHED:
        sys.exit(f"ABORT: only {share:.1%} of app route variants aligned (floor {GATE_MATCHED:.0%})")
    if prev and share < prev * 0.90:
        sys.exit(f"ABORT: matched share fell {prev:.1%} -> {share:.1%} (>10% relative drop)")

    os.makedirs(CACHE, exist_ok=True)
    json.dump({"generated": __import__("datetime").date.today().isoformat(),
               "share": share, "map": out},
              open(OUT, "w"), separators=(",", ":"))
    json.dump({"share": share, "matched": matched, "total": total},
              open(STATS, "w"), indent=1)
    print(f"\nwrote {OUT}  ({os.path.getsize(OUT)/1e6:.1f} MB, {len(out)} route variants)")


if __name__ == "__main__":
    main()
