#!/usr/bin/env python3
"""Generate Departly/Resources/transit_index/tram_{stops,routes}.json for Hong Kong Tramways.

The bundled tram_stops_{en,tc}.csv (data.gov.hk) list stops per Traveling Direction (Westbound /
Eastbound main line + Northbound/Southbound Happy Valley spur) with codes + names but NO coordinates;
we add coordinates from OpenStreetMap (railway=tram_stop on HK Island), matched by English name.

Stop ids are direction-prefixed ("W:02W") because the code "T" is reused for different termini
(Shau Kei Wan vs Kennedy Town) and would otherwise collide. Output mirrors the other operator
indexes so it folds into TransitGraph as agency "TRAM".

Usage:  python3 build_tram_stations.py
"""
import json, os, re, ssl, csv as _csv, urllib.request, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "..", "Departly", "Resources")
OUT = os.path.join(RES, "transit_index")
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
DIR_LETTER = {"Westbound": "W", "Eastbound": "E", "Northbound": "N", "Southbound": "S"}
CURATED = {                       # nameEn -> (lat, lng) for stops OSM lacks
    "Man Wah Lane": (22.28649, 114.15093),       # Queen's Road Central, Sheung Wan
    "Colonial Cemetery": (22.26999, 114.18443),  # Wong Nai Chung Rd, Happy Valley spur
}

def norm(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\b(tram|terminus|stop|station)\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)

def rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [r for r in _csv.reader(f)][1:]

def overpass():
    q = """[out:json][timeout:90];
( node["railway"="tram_stop"](22.20,114.10,22.30,114.26); );
out body;"""
    for host in ("https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"):
        try:
            req = urllib.request.Request(host, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "Departly-tram/1.0"})
            with urllib.request.urlopen(req, timeout=120, context=_ctx) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("  overpass fail", host, e)
    return {}

def main():
    en = rows(os.path.join(RES, "tram_stops_en.csv"))
    tc = rows(os.path.join(RES, "tram_stops_tc.csv"))
    # The two files are row-aligned translations, so join by row INDEX — NOT by (direction, code),
    # because the terminus code "T" repeats within a direction and would collide.
    tcNames = [r[2] if len(r) >= 3 else "" for r in tc]
    assert len(en) == len(tc), f"EN/TC tram CSVs misaligned: {len(en)} vs {len(tc)}"

    osm = {}
    for e in overpass().get("elements", []):
        t = e.get("tags", {}); lat, lng = e.get("lat"), e.get("lon")
        if lat is None or lng is None: continue
        for k in ("name:en", "name", "alt_name:en"):
            if t.get(k): osm.setdefault(norm(t[k]), (float(lat), float(lng)))

    stops, routes, missing, seqByDir = {}, {}, [], {}
    for idx, r in enumerate(en):
        if len(r) < 3: continue
        direction, code, nameEn = r[0], r[1], r[2]
        nameTc = tcNames[idx] if idx < len(tcNames) and tcNames[idx] else nameEn
        dl = DIR_LETTER.get(direction)
        if not dl or not code: continue
        # The code "T" (terminus) repeats within a direction, so a code-based id collides; use a
        # per-direction running sequence instead (codes aren't needed — trams have no live-ETA API).
        coord = CURATED.get(nameEn) or osm.get(norm(nameEn))
        if not coord:
            cand = [v for k, v in osm.items() if norm(nameEn) and norm(nameEn) in k]
            coord = cand[0] if cand else None
        # Drop coordless stops entirely (don't emit them or list them in a route): keeping such a stop
        # in the route's id sequence would leave a gap the graph drops, inflating index-based hop counts.
        if not coord:
            missing.append(nameEn); continue
        seq = seqByDir.get(dl, 0); seqByDir[dl] = seq + 1
        sid = f"{dl}:{seq:03d}"
        stops[sid] = {"id": sid, "nameEn": nameEn, "nameTc": nameTc,
                      "lat": coord[0], "lng": coord[1]}
        routes.setdefault(dl, []).append(sid)

    # The CSV lists every direction in the same geographic order (east→west), so Eastbound ends up
    # reversed vs. how an eastbound tram travels. Orient each route's stop order to travel direction
    # by coordinate (W=toward min lng, E=toward max lng, N=toward max lat, S=toward min lat) so the
    # engine's board-before-alight ordering is correct both ways along the corridor.
    def coord(sid): s = stops[sid]; return (s["lat"], s["lng"])
    def want_reverse(dl, ids):
        valid = [i for i in ids if stops[i]["lat"] != 0]
        if len(valid) < 2: return False
        (alat, alng), (blat, blng) = coord(valid[0]), coord(valid[-1])
        if dl == "W": return blng > alng     # should end further west
        if dl == "E": return blng < alng     # should end further east
        if dl == "N": return blat < alat     # should end further north
        if dl == "S": return blat > alat     # should end further south
        return False
    for dl, ids in routes.items():
        if want_reverse(dl, ids): ids.reverse()
    routeList = [{"route": "Tram", "dir": dl, "service": "", "stops": ids} for dl, ids in routes.items()]

    # COVERAGE GUARD — abort BEFORE writing if this run is not credible. Coordinates come only from
    # Overpass, so when both hosts fail (504 / dropped connection) `osm` is empty and every stop but
    # the 2 hard-coded CURATED ones is dropped as coordless. That still produced a well-formed,
    # non-blank 2-stop file, which slipped past BOTH safety nets as they stood then
    # (regenerate_and_publish.sh only catches a non-zero exit; publish_data.py only caught a BLANK
    # file — it gained a >10% row-shrink guard on 2026-07-31, which would now stop this too) and
    # published a live OTA update that cut tram coverage 119 stops -> 2 (seen 2026-07-24, and twice
    # more on 07-08 / 07-16). Exiting non-zero here makes
    # run() print its WARN and keep the previous good file, which is the documented contract.
    if not osm:
        raise SystemExit("ABORT: Overpass returned no tram stops (all hosts failed) — "
                         "refusing to write a coordinate-less dataset; keeping previous good file.")
    prev = os.path.join(OUT, "tram_stops.json")
    if os.path.exists(prev):
        try:
            prevCount = len(json.load(open(prev, encoding="utf-8")))
        except Exception:
            prevCount = 0
        if prevCount and len(stops) < prevCount * 0.8:
            raise SystemExit(f"ABORT: tram stops collapsed {prevCount} -> {len(stops)} "
                             f"(<80% of previous) — keeping previous good file.")

    os.makedirs(OUT, exist_ok=True)
    for name, obj in [("tram_stops.json", list(stops.values())), ("tram_routes.json", routeList)]:
        p = os.path.join(OUT, name); json.dump(obj, open(p + ".tmp", "w"), ensure_ascii=False); os.replace(p + ".tmp", p)
    coords = sum(1 for s in stops.values() if s["lat"] != 0)
    print(f"== tram: {len(stops)} stops ({coords} with coords), {len(routeList)} directional routes")
    if missing: print(f"NO-COORD ({len(set(missing))}): " + ", ".join(sorted(set(missing))))

if __name__ == "__main__":
    main()
