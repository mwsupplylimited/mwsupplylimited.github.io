#!/usr/bin/env python3
"""Emit board->alight bus fares from the TD GTFS, keyed in APP route coordinates.

THE BUG THIS FIXES
`FareStore.busFareSet` matches `bus_fares.json` by route number + company + DESTINATION NAME STRING
and returns `FULL_FARE` — the fare for boarding at the route's ORIGIN. It has no idea where the
rider actually boards. But 68% of route-directions in this feed charge SECTIONAL fares (only 1,173
of 3,640 are flat), so a rider boarding downstream is quoted the full origin fare on ~2,467
route-directions. That is a silent OVERCHARGE, not an honest abstention — worse than the `$X+` the
app shows when it knows it doesn't know. And GMB / NLB / MTR Bus / tram / ferry are unpriced entirely.

THE DATA
`fare_attributes.txt` + `fare_rules.txt` carry 880,963 rules whose `fare_id` is
`routeId-dir-originSeq-destSeq`, so a fare is addressed by STOP SEQUENCE directly — no stop-id join
needed beyond the crosswalk this already consumes.

COMPRESSION
Price is origin-determined for the overwhelming majority of pairs, so each route-direction is stored
as a base array indexed by BOARD position plus an exception map for the pairs that differ. That is
what turns 880k rules into a few hundred KB.

OUTPUT  Departly/Resources/transit_index/fares_od.json
  { "v":1, "cur":"HKD", "r": {
      "KMB|1|O|1":   {"f": 6.7},                              # flat: one price from anywhere
      "KMB|68X|O|1": {"o": [23.8, 23.8, 15.4, ...],           # o[boardIdx] = usual price boarding there
                      "x": {"3-19": 21.2}}                    # exceptions, "boardIdx-alightIdx"
  } }
Indices are APP stop positions, so `JourneyFare` can use `leg.boardIndex` directly.
"""
import csv, io, json, os, sys, zipfile
from collections import Counter, defaultdict
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
IDX = os.path.join(HERE, "..", "Departly", "Resources", "transit_index")
CACHE = os.path.join(HERE, ".transit_cache")
XWALK = os.path.join(CACHE, "gtfs_crosswalk.json")
GTFS_ZIP = os.path.join(CACHE, "td_gtfs.zip")
OUT = os.path.join(IDX, "fares_od.json")

SOURCES = [("kmb_routes.json", "KMB"), ("ctb_routes.json", "CTB"), ("gmb_routes.json", "GMB"),
           ("nlb_routes.json", "NLB"), ("mtrbus_routes.json", "MTRB")]


def main():
    if not os.path.exists(XWALK):
        sys.exit(f"ABORT: {XWALK} missing — run build_gtfs_crosswalk.py first")
    xw = json.load(open(XWALK))["map"]
    z = zipfile.ZipFile(GTFS_ZIP)

    def rows(name):
        with z.open(name) as f:
            yield from csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"))

    # fare_id -> price. The id already encodes route-dir + origin/destination SEQUENCE, so
    # fare_rules.txt is not needed for the lookup itself.
    price = {}
    nonhkd = zerofare = 0
    for a in rows("fare_attributes.txt"):
        if (a.get("currency_type") or "HKD") != "HKD":
            nonhkd += 1
            continue
        try:
            p = float(a["price"])
        except (TypeError, ValueError):
            continue
        # A 0.00 fare is IN the feed (553 rows across the affected route-dirs) but is not credible for
        # a franchised HK bus — there are no free sections. Treated as UNKNOWN, not as free, so the app
        # renders `$X+` rather than an invented "$0.0". Same rule as everywhere else in this codebase:
        # never show a number we don't have.
        if p <= 0:
            zerofare += 1
            continue
        price[a["fare_id"]] = p
    print(f"fare_attributes: {len(price)} HKD prices"
          + (f", {nonhkd} non-HKD skipped" if nonhkd else "")
          + (f", {zerofare} zero-priced treated as UNKNOWN" if zerofare else ""))

    # route-dir -> {(origSeq, destSeq): price}
    by_rd = defaultdict(dict)
    for fid, p in price.items():
        parts = fid.split("-")
        if len(parts) != 4:
            continue
        rid, d, o, dst = parts
        try:
            by_rd[f"{rid}-{d}"][(int(o), int(dst))] = p
        except ValueError:
            continue
    print(f"route-directions with fares: {len(by_rd)}")

    out, stats = {}, Counter()
    for rfile, agency in SOURCES:
        for rv in json.load(open(os.path.join(IDX, rfile))):
            key = f'{agency}|{rv["route"]}|{rv["dir"]}|{rv.get("service","")}'
            ent = xw.get(key)
            if not ent:
                stats["no_crosswalk"] += 1
                continue
            table = by_rd.get(ent["g"])
            if not table:
                stats["no_fares"] += 1
                continue
            gidx = ent["idx"]                       # app position -> GTFS position (0-based), -1 if unmatched
            n = len(gidx)

            # Collect every priced app (board, alight) pair. GTFS sequences are 1-based.
            pairs = {}
            for b in range(n):
                if gidx[b] < 0:
                    continue
                for a in range(b + 1, n):
                    if gidx[a] < 0:
                        continue
                    p = table.get((gidx[b] + 1, gidx[a] + 1))
                    if p is not None:
                        pairs[(b, a)] = p
            if not pairs:
                stats["no_priced_pairs"] += 1
                continue

            distinct = {round(p, 2) for p in pairs.values()}
            if len(distinct) == 1:
                out[key] = {"f": round(next(iter(distinct)), 2)}
                stats["flat"] += 1
                continue

            # Sectional: the usual price boarding at each stop, plus the pairs that differ from it.
            base = [None] * n
            byboard = defaultdict(list)
            for (b, a), p in pairs.items():
                byboard[b].append(p)
            for b, ps in byboard.items():
                base[b] = round(Counter(round(p, 2) for p in ps).most_common(1)[0][0], 2)
            # Carry the last known price forward over stops with no priced pair of their own (a later
            # boarding point on the same section charges the same), so a lookup does not fall into a
            # hole mid-route.
            last = None
            for i in range(n):
                if base[i] is None:
                    base[i] = last
                else:
                    last = base[i]
            # Any stop STILL unpriced (nothing known at or before it) is emitted as -1, NEVER 0.
            # A zero would render as "$0.0" — an invented, and invitingly wrong, number. -1 means
            # "we don't know", and the app already has an honest rendering for that: `$X+`.
            exceptions = {f"{b}-{a}": round(p, 2) for (b, a), p in pairs.items()
                          if base[b] is None or round(p, 2) != base[b]}
            entry = {"o": [b if b is not None else -1 for b in base]}
            if exceptions:
                entry["x"] = exceptions
            out[key] = entry
            stats["sectional"] += 1
            stats["exception_pairs"] += len(exceptions)

    total = stats["flat"] + stats["sectional"]
    print(f"\npriced route-directions: {total}  (flat {stats['flat']}, sectional {stats['sectional']})")
    print(f"  exception pairs stored: {stats['exception_pairs']}")
    print(f"  skipped: no crosswalk {stats['no_crosswalk']}, no GTFS fares {stats['no_fares']}, "
          f"no priced pairs {stats['no_priced_pairs']}")

    # ---- QA gate: cross-check against the CURRENT source of truth. Boarding at position 0 must equal
    # bus_fares.json's FULL_FARE, because that is exactly what today's app quotes. A systematic
    # mismatch means the crosswalk polarity flipped and every fare would be wrong.
    try:
        legacy = json.load(open(os.path.join(IDX, "..", "bus_fares.json")))
    except Exception:
        legacy = None
    if legacy:
        # bus_fares.json rows look like {"r":"1","co":"KMB","d":"STAR FERRY","o":"CHUK YUEN ESTATE","f":6.7}
        # — `r` route number, `co` operator (may be joint, e.g. "KMB+CTB"), `f` the adult full fare.
        lookup = defaultdict(list)
        for row in (legacy if isinstance(legacy, list) else []):
            r = str(row.get("r") or "").strip()
            if r and row.get("f") is not None:
                lookup[r].append(row)
        agree = compared = 0
        worst = []
        for key, e in out.items():
            ag, route = key.split("|")[0], key.split("|")[1]
            if ag not in ("KMB", "CTB"):
                continue
            mine = e.get("f", (e.get("o") or [None])[0])
            if mine is None or mine < 0:            # -1 = genuinely unpriced, not comparable
                continue
            fulls = []
            for c in lookup.get(route, []):
                if ag not in str(c.get("co") or ""):
                    continue
                try:
                    fulls.append(float(c["f"]))
                except (TypeError, ValueError):
                    pass
            if not fulls:
                continue
            compared += 1
            if any(abs(mine - f) <= 0.1 for f in fulls):
                agree += 1
            elif len(worst) < 8:
                worst.append((key, mine, fulls))
        if worst:
            print("  sample disagreements (ours @ board 0 vs bus_fares full fare):")
            for k, mine, fulls in worst:
                print(f"     {k:<26} ours={mine}  legacy={sorted(set(fulls))}")
        if compared:
            share = agree / compared
            print(f"\nQA vs bus_fares.json: {agree}/{compared} agree at board index 0 ({share:.1%})")
            if share < 0.90:
                sys.exit(f"ABORT: only {share:.1%} agree with the existing fare table at the ORIGIN — "
                         f"a systematic mismatch means the crosswalk direction polarity is wrong")
        else:
            print("\nQA vs bus_fares.json: no comparable rows (schema differs) — skipped")

    if total < 2000:
        sys.exit(f"ABORT: only {total} priced route-directions (floor 2000)")

    json.dump({"v": 1, "generated": date.today().isoformat(), "cur": "HKD", "r": out},
              open(OUT, "w"), separators=(",", ":"))
    print(f"\nwrote {OUT} ({os.path.getsize(OUT)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
