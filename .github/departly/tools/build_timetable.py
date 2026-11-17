#!/usr/bin/env python3
"""Emit Departly's timetable spine from the TD GTFS, keyed in APP coordinates.

WHAT THIS REPLACES
`build_bus_schedule.py` downloads this same feed and keeps three things: first/last departure,
coarse per-ROUTE headway bands, and one median speed -- after filtering `calendar.txt` to
`monday == "1"`, which DISCARDS 43 % of all trips (35,720) and every route that only runs at
weekends (24 within its own scope, 89 feed-wide). This emits the parts that were thrown away.

THE STRUCTURAL CHOICE: FACTORIZE, DON'T MATERIALIZE
A full `stop_times` equivalent -- arrival at every stop of every trip, with frequency envelopes
expanded -- is 4.67 M rows / 25-40 MB. Unshippable against a 17 MB bundle and a 3 s cold launch.
But each (route_id, direction) in this feed has EXACTLY ONE stop pattern (verified: 3,637
route-dirs -> 3,637 distinct patterns), and run times are 100 % time-invariant (measured: spread
max-min = 0.0 min across 1,508 route-dirs with >=8 trips). So

    stopTime(trip, k) = departure(trip) + offset(pattern, k)

is LOSSLESS, not an approximation, and costs ~1.1 MB instead of 25-40 MB.

FREQUENCY TRIPS STAY ENVELOPES -- FOR HONESTY, NOT SIZE
`frequencies.txt` has NO `exact_times` column, which per the GTFS spec means departures are
"approximately every N seconds" and the PHASE IS UNKNOWABLE: `start_time` is a window boundary,
not a departure. Expanding "roughly every 9 min from 07:12" into 07:12 / 07:21 / 07:30 fabricates
times the operator never published. Only the 6,153 genuinely-timed trips (7 %) may ever be shown
as a clock, so the two shapes are kept DISTINCT and the client returns a tagged value:
    `c` = headway cutpoints (frequency-based -> render "~ every N min")
    `t` = exact departures      (real trips     -> may render "14:32")

OUTPUTS (Departly/Resources/transit_index/)
  service_calendar.json  ~2 KB   services x weekly mask + public-holiday mask + PH dates
  departures.json        ~800 KB per app route-dir x service: first departure, cutpoints, exacts
  stop_offsets.json      ~290 KB per app route-dir: cumulative run time at EVERY app stop
"""
import csv, io, json, math, os, statistics, sys, zipfile
from collections import defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
IDX = os.path.join(HERE, "..", "Departly", "Resources", "transit_index")
CACHE = os.path.join(HERE, ".transit_cache")
XWALK = os.path.join(CACHE, "gtfs_crosswalk.json")
GTFS_ZIP = os.path.join(CACHE, "td_gtfs.zip")
PH_ICAL = "https://www.1823.gov.hk/common/ical/en.ics"

OUT_CAL = os.path.join(IDX, "service_calendar.json")
OUT_DEP = os.path.join(IDX, "departures.json")
OUT_OFF = os.path.join(IDX, "stop_offsets.json")

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SOURCES = [("kmb_routes.json", "kmb_stops.json", "KMB"), ("ctb_routes.json", "ctb_stops.json", "CTB"),
           ("gmb_routes.json", "gmb_stops.json", "GMB"), ("nlb_routes.json", "nlb_stops.json", "NLB"),
           ("mtrbus_routes.json", "mtrbus_stops.json", "MTRB")]


def hav(a, b):
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    dla, dlo = la2 - la1, lo2 - lo1
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def tmin(s):
    """'HH:MM:SS' -> minutes from midnight; hours may exceed 24 for an after-midnight trip."""
    s = (s or "").strip()
    if ":" not in s:
        return None
    p = s.split(":")
    try:
        return int(p[0]) * 60 + int(p[1])
    except ValueError:
        return None


def main():
    if not os.path.exists(XWALK):
        sys.exit(f"ABORT: {XWALK} missing -- run build_gtfs_crosswalk.py first")
    xw = json.load(open(XWALK))["map"]
    z = zipfile.ZipFile(GTFS_ZIP)

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

    # ---------------------------------------------------------------- calendar
    cal = {c["service_id"]: c for c in rows("calendar.txt")}
    exceptions = defaultdict(dict)                      # date -> service_id -> 1 add / 2 remove
    for r in rows("calendar_dates.txt"):
        exceptions[r["date"]][r["service_id"]] = r["exception_type"]
    ph_dates = sorted(exceptions)

    # PUBLIC HOLIDAYS ARE PER-WEEKDAY, NOT "PH => SUNDAY". Replaying calendar_dates shows the
    # Saturday-only service is left untouched on a Thursday holiday but DOES run on a Saturday
    # holiday -- a flat "use Sunday" rule gets that wrong. So for each weekday w we record whether
    # the service runs on a holiday falling on w. Six bytes, and exact.
    ph_by_dow = defaultdict(lambda: defaultdict(list))  # service -> dow -> [runs?]
    for d in ph_dates:
        try:
            dow = date(int(d[:4]), int(d[4:6]), int(d[6:8])).weekday()
        except ValueError:
            continue
        for sid, c in cal.items():
            ex = exceptions[d].get(sid)
            runs = c[DAYS[dow]] == "1"
            if ex == "1":
                runs = True
            elif ex == "2":
                runs = False
            ph_by_dow[sid][dow].append(runs)

    # ---------------------------------------------------------------- trips
    trip_route, trip_service = {}, {}
    for t in rows("trips.txt"):
        trip_route[t["trip_id"]] = t["route_id"]
        trip_service[t["trip_id"]] = t["service_id"]

    freq = defaultdict(list)                            # trip_id -> [(start, end, headwayMin)]
    for f in rows("frequencies.txt"):
        s, e = tmin(f["start_time"]), tmin(f["end_time"])
        try:
            h = max(1, int(f["headway_secs"]) // 60)
        except ValueError:
            continue
        if s is not None and e is not None:
            freq[f["trip_id"]].append((s, e, h))

    # Per trip: origin departure + terminus arrival. That is all the GTFS times (12.1 % of rows).
    trip_dep, trip_dur = {}, {}
    cur, first_t, last_t = None, None, None
    for st in rows("stop_times.txt"):
        tid = st["trip_id"]
        if tid != cur:
            if cur is not None and first_t is not None:
                trip_dep[cur] = first_t
                if last_t is not None:
                    trip_dur[cur] = last_t - first_t
            cur, first_t, last_t = tid, None, None
        if first_t is None:
            first_t = tmin(st["departure_time"])
        a = tmin(st["arrival_time"])
        if a is not None:
            last_t = a
    if cur is not None and first_t is not None:
        trip_dep[cur] = first_t
        if last_t is not None:
            trip_dur[cur] = last_t - first_t

    # GTFS route-dir -> trips
    by_rd = defaultdict(list)
    for tid, rid in trip_route.items():
        parts = tid.split("-")
        if len(parts) >= 2:
            by_rd[f"{parts[0]}-{parts[1]}"].append(tid)

    used_services = set()

    # ---------------------------------------------------------------- per app route
    offsets, departures = {}, {}
    stats = defaultdict(int)
    for rfile, sfile, agency in SOURCES:
        routes = json.load(open(os.path.join(IDX, rfile)))
        stops = {s["id"]: (s["lat"], s["lng"]) for s in json.load(open(os.path.join(IDX, sfile)))}
        for rv in routes:
            key = f'{agency}|{rv["route"]}|{rv["dir"]}|{rv.get("service","")}'
            ent = xw.get(key)
            if not ent:
                stats["no_crosswalk"] += 1
                continue
            trips = by_rd.get(ent["g"], [])
            durs = [trip_dur[t] for t in trips if t in trip_dur and 0 < trip_dur[t] < 600]
            if not durs:
                stats["no_duration"] += 1
                continue
            total = statistics.median(durs)

            # ---- offsets: spread the scheduled end-to-end duration along the APP stop sequence by
            # cumulative inter-stop distance, so the offsets are already in app coordinates and the
            # client needs no crosswalk at runtime. Tenths of a minute, so per-segment deltas survive
            # subtraction on dense urban routes.
            pts = [stops.get(s) for s in rv["stops"]]
            if any(p is None for p in pts) or len(pts) < 2:
                stats["missing_stop_coords"] += 1
                continue
            cum = [0.0]
            for i in range(1, len(pts)):
                cum.append(cum[-1] + hav(pts[i - 1], pts[i]))
            span = cum[-1]
            if span <= 0:
                stats["degenerate_geometry"] += 1
                continue
            o = [int(round(total * (c / span) * 10)) for c in cum]
            for i in range(1, len(o)):                 # enforce monotonic non-decreasing
                if o[i] < o[i - 1]:
                    o[i] = o[i - 1]

            offsets[key] = {
                "g": ent["g"],
                "d": int(round(total * 10)),
                # Integrity fingerprint. build_transit_index.py and this script run independently in
                # regenerate_and_publish.sh; if the index regenerated and the timetable did not, every
                # offset would silently shift by a stop -- valid JSON, valid sha, wrong answer, exactly
                # the tram-corruption failure class. The client drops any entry whose fingerprint does
                # not match its loaded GraphRoute.
                "k": f'{rv["stops"][0]}|{rv["stops"][-1]}|{len(rv["stops"])}',
                "o": o,
            }

            # ---- departures, per service
            per_service = defaultdict(lambda: {"exact": [], "bands": []})
            for t in trips:
                sid = trip_service.get(t)
                if sid is None or t not in trip_dep:
                    continue
                used_services.add(sid)
                if t in freq:
                    per_service[sid]["bands"].extend(freq[t])
                else:
                    per_service[sid]["exact"].append(trip_dep[t])

            svc_out = {}
            for sid, v in per_service.items():
                bands = sorted(set(v["bands"]))
                exact = sorted(set(v["exact"]))
                if not bands and not exact:
                    continue
                entry = {}
                starts = [b[0] for b in bands] + exact
                entry["s"] = min(starts)
                if bands:
                    # Cutpoint encoding: bands are contiguous, so store [bandEnd, headway] and let the
                    # first band start at `s`. ~30 % smaller than [start, end, headway] triples.
                    entry["c"] = [[e, h] for (_, e, h) in bands]
                if exact:
                    entry["t"] = exact
                svc_out[sid] = entry
            if svc_out:
                departures[key] = svc_out
            stats["ok"] += 1

    # ---------------------------------------------------------------- emit calendar
    services = {}
    for sid in sorted(used_services):
        c = cal.get(sid)
        if not c:
            continue
        m = "".join("1" if c[d] == "1" else "0" for d in DAYS)
        # For each weekday, does this service run on a public holiday falling on that weekday?
        #
        # The `else m[dow]` fallback is LOAD-BEARING. The feed's 17 holiday dates cover Mon-Sat but
        # NOT Sunday, so `any([])` on an unobserved weekday returned False and the mask claimed
        # nothing runs on a Sunday public holiday -- which would have FALSELY CLOSED routes, the one
        # direction this data must never fail in. With no observation for a weekday we fall back to
        # normal behaviour for that weekday: over-show, never hide (the same contract
        # gmbServiceWindow / macauServiceWindow already follow).
        h = ""
        for dow in range(7):
            obs = ph_by_dow[sid].get(dow, [])
            h += ("1" if any(obs) else "0") if obs else m[dow]
        services[sid] = {"m": m, "h": h}

    iso_ph = sorted({f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in ph_dates})
    cal_out = {"v": 1, "generated": date.today().isoformat(), "s": services, "ph": iso_ph}

    # ---------------------------------------------------------------- QA gates
    if len(services) < 5:
        sys.exit(f"ABORT: only {len(services)} services in scope")
    if len(iso_ph) < 10:
        sys.exit(f"ABORT: only {len(iso_ph)} public-holiday dates -- the feed's calendar_dates looks wrong")
    if not any(d > date.today().isoformat() for d in iso_ph):
        print("WARNING: every public-holiday date is in the past -- the PH list needs refreshing "
              "(the client falls back to the plain weekly mask past coverage, over-showing rather "
              "than falsely closing a route)")
    total_off = sum(len(v["o"]) for v in offsets.values())
    if total_off < 40000:
        sys.exit(f"ABORT: only {total_off} stop offsets emitted (floor 40000)")

    for path, blob in ((OUT_CAL, cal_out),
                       (OUT_DEP, {"v": 1, "generated": date.today().isoformat(), "r": departures}),
                       (OUT_OFF, {"v": 1, "generated": date.today().isoformat(),
                                  "src": "td-gtfs", "r": offsets})):
        json.dump(blob, open(path, "w"), separators=(",", ":"))
        print(f"wrote {os.path.basename(path):<24} {os.path.getsize(path)/1024:>8.0f} KB")

    print(f"\nroute variants timetabled: {stats['ok']}")
    print(f"  no crosswalk {stats['no_crosswalk']}  no duration {stats['no_duration']}  "
          f"missing coords {stats['missing_stop_coords']}  degenerate {stats['degenerate_geometry']}")
    print(f"services in use: {len(services)} (of {len(cal)} in the feed)   PH dates: {len(iso_ph)}")
    print(f"stop offsets: {total_off}   departure entries: "
          f"{sum(len(v.get('c', [])) + len(v.get('t', [])) for r in departures.values() for v in r.values())}")


if __name__ == "__main__":
    main()
