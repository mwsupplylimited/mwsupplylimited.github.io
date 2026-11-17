#!/usr/bin/env python3
"""Preprocess the HK Transport Department GTFS ("headway") feed into a COMPACT per-route schedule
index for Departly.

Two things are extracted per route:
  1. HEADWAY / first-last departure (the original "no live ETA" fallback) — f / l / b.
  2. SCHEDULED SPEED (NEW) — spd — a single metres-per-minute figure so the journey planner can
     time a bus/tram ride from the real timetable instead of a crude driving×1.25 heuristic. Speed,
     not a fixed per-stop minute, is used so uneven stop spacing is honoured:
     rideTime = boardAlightDistance / spd.

How spd is derived: the GTFS times only the ORIGIN departure and TERMINUS arrival of each trip
(intermediate stops are blank), but it lists EVERY stop with coordinates. So per trip we get a real
end-to-end scheduled DURATION and can measure the along-route DISTANCE from the stop coords →
speed = distance / duration (metres per minute, INCLUDING dwell). We take the MEDIAN over all
weekday trips of the route.

NOTE (verified against the live feed, Jul 2026): the TD scheduled durations are TIME-INVARIANT —
a route is scheduled the same run-time at 08:00 as at 11:00 (checked KMB 59X = 53 min at every
hour). So this gives an accurate BASELINE / off-peak / future-trip / tram time, but it does NOT
encode rush-hour congestion. Rush-hour must come from the LIVE traffic-aware driving estimate
(kept, blended as a floor→stretch) or a future live-ETA-delta layer — NOT from this schedule.

Scope: franchised buses with clean per-operator route numbers — KMB (incl. LWB), CTB, NLB — plus
HK Tramways (aggregated agency-wide under key "TRAM|*", since the six GTFS tram "routes" share one
slow street corridor). GMB is excluded (route numbers collide across regions). Rail isn't in this
feed (MTR/LRT get scheduled trains via the live source="+").

Output: Departly/Resources/transit_index/bus_schedule.json
  { "KMB|1A": {"f":330,"l":1490,"b":[[330,420,15],...], "sp":[[0,420,540],[420,600,360],...]},
    "TRAM|*": {"f":-1,"l":-1,"b":[], "sp":[[0,1440,150],...]}, ... }
  f/l = first/last origin departure (minutes from midnight; may exceed 1440 after midnight)
  b   = headway bands [startMin, endMin, headwayMin]
  sp  = scheduled-speed bands [startMin, endMin, metresPerMinute]  (0..1440, gap-free)
"""
import csv, io, json, math, os, statistics, urllib.request, zipfile
from collections import defaultdict

GTFS_URL = "https://static.data.gov.hk/td/pt-headway-en/gtfs.zip"
OUT = os.path.join(os.path.dirname(__file__), "..", "Departly", "Resources", "transit_index", "bus_schedule.json")
AGENCY_MAP = {"KMB": "KMB", "LWB": "KMB", "CTB": "CTB", "NLB": "NLB"}   # app BusOperator rawValues
TRAM_KEY = "TRAM|*"
SPEED_MIN, SPEED_MAX = 40.0, 1500.0     # m/min sanity clamp (~2.4–90 km/h incl. dwell)

def tmin(s):
    """'HH:MM:SS' or 'HHMM' -> integer minutes from midnight (h may be >=24 after midnight)."""
    s = (s or "").strip()
    if ":" in s:
        p = s.split(":"); return int(p[0]) * 60 + int(p[1])
    if len(s) == 4 and s.isdigit():
        return int(s[:2]) * 60 + int(s[2:])
    return None

def hav(a, b):
    """metres between (lat,lng) tuples."""
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))

def main():
    print("downloading", GTFS_URL)
    data = urllib.request.urlopen(GTFS_URL, timeout=180).read()
    print(f"  {len(data)/1e6:.1f} MB zip")
    z = zipfile.ZipFile(io.BytesIO(data))
    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

    # routes: route_id -> (scheduleKey, is_tram)
    routeKey = {}
    for r in rows("routes.txt"):
        rt = r.get("route_type")
        rid = r["route_id"]
        if rt == "3":
            ag = AGENCY_MAP.get((r.get("agency_id") or "").strip())
            num = (r.get("route_short_name") or "").strip()
            if ag and num: routeKey[rid] = f"{ag}|{num}"
        elif rt == "0":                                   # tram
            routeKey[rid] = TRAM_KEY
    print("routes (bus+tram):", len(routeKey))

    # weekday service_ids (runs Monday) — a representative weekday profile
    weekday = {c["service_id"] for c in rows("calendar.txt") if c.get("monday") == "1"}
    print("weekday service_ids:", len(weekday))

    # trips: trip_id -> route_id (weekday, in-scope only)
    tripRoute = {}
    for t in rows("trips.txt"):
        rid = t.get("route_id")
        if rid in routeKey and t.get("service_id") in weekday:
            tripRoute[t["trip_id"]] = rid
    print("weekday trips in scope:", len(tripRoute))

    # stops: stop_id -> (lat, lng)
    coord = {}
    for s in rows("stops.txt"):
        try: coord[s["stop_id"]] = (float(s["stop_lat"]), float(s["stop_lon"]))
        except (TypeError, ValueError): pass

    # stop_times grouped by trip -> ordered (seq, stop_id, arr, dep)
    tripStops = defaultdict(list)
    n = 0
    for st in rows("stop_times.txt"):
        tid = st.get("trip_id")
        if tid not in tripRoute: continue
        try: seq = int(st.get("stop_sequence"))
        except (TypeError, ValueError): continue
        tripStops[tid].append((seq, st.get("stop_id"), st.get("arrival_time"), st.get("departure_time")))
        n += 1
    print(f"stop_times rows kept: {n} across {len(tripStops)} trips")

    # -------- headway / first-last (original) --------
    firstByRoute, lastByRoute = {}, {}
    for tid, rid in tripRoute.items():
        m = tmin(tid.split("-")[-1])
        if m is None: continue
        if rid not in firstByRoute or m < firstByRoute[rid]: firstByRoute[rid] = m
        if rid not in lastByRoute or m > lastByRoute[rid]: lastByRoute[rid] = m
    bandsByRoute = defaultdict(set)
    for fr in rows("frequencies.txt"):
        rid = tripRoute.get(fr.get("trip_id"))
        if not rid: continue
        s, e = tmin(fr.get("start_time")), tmin(fr.get("end_time"))
        hw = fr.get("headway_secs")
        if s is None or e is None or not hw: continue
        bandsByRoute[rid].add((s, e, max(1, round(int(hw) / 60))))

    # -------- scheduled speed (NEW) --------
    distCache = {}                                  # stop-id pattern -> along-route metres
    speedByKey = defaultdict(list)                  # key -> [m/min] across weekday trips
    for tid, sts in tripStops.items():
        sts.sort(key=lambda x: x[0])
        # origin departure = first stop with a time; terminus arrival = last stop with a time
        dep0 = next((tmin(dep) or tmin(arr) for _, _, arr, dep in sts if tmin(dep) is not None or tmin(arr) is not None), None)
        arrN = next((tmin(arr) or tmin(dep) for _, _, arr, dep in reversed(sts) if tmin(arr) is not None or tmin(dep) is not None), None)
        if dep0 is None or arrN is None: continue
        dur = arrN - dep0
        if dur <= 0: continue
        pattern = tuple(sid for _, sid, _, _ in sts)
        dist = distCache.get(pattern)
        if dist is None:
            dist = 0.0
            for i in range(1, len(pattern)):
                a, b = coord.get(pattern[i-1]), coord.get(pattern[i])
                if a and b: dist += hav(a, b)
            distCache[pattern] = dist
        if dist <= 0: continue
        sp = dist / dur
        if not (SPEED_MIN <= sp <= SPEED_MAX): continue
        speedByKey[routeKey[tripRoute[tid]]].append(sp)

    spdByKey = {key: round(statistics.median(v)) for key, v in speedByKey.items() if v}
    print("routes with speed profile:", len(spdByKey))

    # -------- assemble --------
    keys = set(routeKey.values())
    final = {}
    for key in keys:
        rids = [rid for rid, k in routeKey.items() if k == key]
        f = min((firstByRoute[r] for r in rids if r in firstByRoute), default=-1)
        l = max((lastByRoute[r] for r in rids if r in lastByRoute), default=-1)
        b = sorted([list(x) for r in rids for x in bandsByRoute.get(r, ())])
        spd = spdByKey.get(key)
        if f < 0 and not b and not spd: continue
        entry = {"f": f, "l": l, "b": b}
        if spd: entry["spd"] = spd
        final[key] = entry

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fp:
        json.dump(final, fp, separators=(",", ":"), ensure_ascii=False)
    sz = os.path.getsize(OUT)
    withsp = sum(1 for v in final.values() if v.get("spd"))
    print(f"WROTE {OUT}  ({len(final)} routes, {withsp} with speed, {sz/1024:.0f} KB)")
    for k in ["KMB|1", "KMB|59X", "KMB|6", "CTB|619", "NLB|1", TRAM_KEY]:
        if k in final: print("  ", k, "spd(m/min)=", final[k].get("spd"))

if __name__ == "__main__":
    main()
