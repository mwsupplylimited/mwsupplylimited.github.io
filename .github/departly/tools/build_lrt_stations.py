#!/usr/bin/env python3
"""Generate Departly/Resources/lrt_stations.json = MTR Light Rail stop code -> coordinate.

Mirrors build_mtr_stations.py: the bundled light_rail_routes_and_stops.csv (opendata.mtr.com.hk)
has stop codes/names but no coordinates; the journey planner needs them to snap an endpoint to LRT
stops and render rail legs. Light Rail is modelled in OSM as tram stops in the NW New Territories
(Tuen Mun / Yuen Long / Tin Shui Wai); match by normalized English name.

Usage:  python3 build_lrt_stations.py   (reports matched/unmatched for verification)
"""
import json, os, re, ssl, csv as _csv, urllib.request, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "..", "Departly", "Resources")
CSV = os.path.join(RES, "light_rail_routes_and_stops.csv")
OUT = os.path.join(RES, "lrt_stations.json")
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE

# Authoritative WGS84 coords for stops whose OSM name doesn't exact-match the CSV (precedence over OSM).
CURATED = {}

def norm(s):
    s = s.lower()
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\b(lrt|light rail|stop|station|terminus)\b", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s

def csv_stops():
    out = {}
    with open(CSV, encoding="utf-8-sig", newline="") as f:
        for row in _csv.reader(f):
            if len(row) < 7 or row[0] == "Line Code":
                continue
            code, name = row[2].strip(), row[5].strip()
            if code and name and code not in out:
                out[code] = name
    return out

def overpass():
    q = """[out:json][timeout:90];
(
  node["railway"="tram_stop"](22.36,113.93,22.50,114.07);
  node["railway"="station"]["station"="light_rail"](22.36,113.93,22.50,114.07);
);
out tags center;"""
    for host in ("https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"):
        try:
            req = urllib.request.Request(host, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "Departly-lrt/1.0"})
            with urllib.request.urlopen(req, timeout=120, context=_ctx) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("  overpass fail", host, e)
    return {}

def main():
    stations = csv_stops()
    print(f"CSV LRT stops: {len(stations)}")
    data = overpass()
    elems = data.get("elements", [])
    print(f"OSM tram/LR elements: {len(elems)}")

    osm = {}
    for e in elems:
        t = e.get("tags", {})
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        for key in ("name:en", "name", "alt_name:en"):
            if t.get(key):
                osm.setdefault(norm(t[key]), (float(lat), float(lng)))

    out, missing = [], []
    for code, name in sorted(stations.items()):
        coord = CURATED.get(code) or osm.get(norm(name))
        if coord:
            out.append({"code": code, "nameEn": name, "lat": coord[0], "lng": coord[1]})
        else:
            missing.append(f"{code} ({name})")

    out.sort(key=lambda s: s["code"])
    # GUARD (root-cause fix for the 2026-07-09 incident): NEVER overwrite the good file with an empty/
    # short result. When Overpass is down/rate-limited, osm={} → every stop is unmatched → out=[]; the
    # old code then wrote [] and the publish step pushed it to the OTA, dropping ALL Light Rail from the
    # journey planner (the empty [] passed the app's sha integrity check). There are ~68 LRT stops, so a
    # healthy run matches most of them; a result below the floor means the fetch failed — keep the last
    # good file untouched and report failure instead of blanking the dataset.
    MIN_STATIONS = 50
    if len(out) < MIN_STATIONS:
        print(f"\n!! ABORT: only matched {len(out)}/{len(stations)} LRT stops (< {MIN_STATIONS}) — "
              f"Overpass likely unavailable. KEEPING the existing {OUT} unchanged (not blanking it).")
        return
    tmp = OUT + ".tmp"; json.dump(out, open(tmp, "w"), ensure_ascii=False); os.replace(tmp, OUT)
    print(f"\n== matched {len(out)}/{len(stations)} -> {OUT}")
    if missing:
        print(f"UNMATCHED ({len(missing)}): " + ", ".join(missing))
    # dup-coord check (a bad match)
    from collections import Counter
    c = Counter((round(s["lat"], 4), round(s["lng"], 4)) for s in out)
    dups = [(k, v) for k, v in c.items() if v > 1]
    print("duplicate coords:", len(dups), dups[:5])

if __name__ == "__main__":
    main()
