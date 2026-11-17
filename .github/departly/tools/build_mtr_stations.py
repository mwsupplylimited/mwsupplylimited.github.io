#!/usr/bin/env python3
"""Generate Departly/Resources/mtr_stations.json = MTR station code -> coordinate.

The bundled mtr_lines_and_stations.csv (opendata.mtr.com.hk) has station codes/names but NO
coordinates; the multi-modal journey planner needs them to snap an endpoint to nearby stations and
to render rail legs. We fetch heavy-rail station coordinates from OpenStreetMap (Overpass) and match
them to the CSV by normalized English name. Output is bundled (static; stations don't move).

Usage:  python3 build_mtr_stations.py
Reports matched/unmatched so coverage is verifiable (unmatched stations simply aren't rail-routable).
"""
import json, os, re, ssl, csv as _csv, urllib.request, urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(ROOT, "..", "Departly", "Resources")
CSV = os.path.join(RES, "mtr_lines_and_stations.csv")
OUT = os.path.join(RES, "mtr_stations.json")
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE

# Authoritative coordinates (WGS84) for stations whose OSM English name doesn't exact-match the
# MTR CSV name. These take PRECEDENCE over OSM so fuzzy mismatches can't slip in. Sourced from the
# stations' known locations and spot-verified.
CURATED = {
    "HOK": (22.28488, 114.15829),   # Hong Kong (Central, AEL/TCL terminus)
    "DIH": (22.33983, 114.20165),   # Diamond Hill (KTL/TML)
    "RAC": (22.40100, 114.20320),   # Racecourse (EAL, race-day only)
    "AWE": (22.32150, 113.94340),   # AsiaWorld-Expo (AEL)
    "LHP": (22.29650, 114.26860),   # LOHAS Park (TKL)
    "OCP": (22.24850, 114.17460),   # Ocean Park (SIL)
}

def norm(s):
    s = s.lower()
    s = s.replace("&", "and")
    s = re.sub(r"\(.*?\)", "", s)                       # drop "(...)"
    s = re.sub(r"\b(mtr|station|stn|platform|terminus)\b", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)                     # keep alnum only
    return s

def csv_stations():
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
  node["railway"="station"](22.15,113.80,22.57,114.45);
  way["railway"="station"](22.15,113.80,22.57,114.45);
  node["railway"="halt"](22.15,113.80,22.57,114.45);
);
out center tags;"""
    for host in ("https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter"):
        try:
            req = urllib.request.Request(host, data=urllib.parse.urlencode({"data": q}).encode(),
                                         headers={"User-Agent": "Departly-mtr/1.0"})
            with urllib.request.urlopen(req, timeout=120, context=_ctx) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            print("  overpass fail", host, e)
    return {}

def main():
    stations = csv_stations()
    print(f"CSV stations: {len(stations)}")
    data = overpass()
    elems = data.get("elements", [])
    print(f"OSM elements: {len(elems)}")

    # normalized-name -> (lat, lng), preferring name:en
    osm = {}
    for e in elems:
        t = e.get("tags", {})
        lat = e.get("lat") or (e.get("center") or {}).get("lat")
        lng = e.get("lon") or (e.get("center") or {}).get("lon")
        if lat is None or lng is None:
            continue
        for key in ("name:en", "name", "alt_name:en", "official_name:en"):
            if t.get(key):
                osm.setdefault(norm(t[key]), (float(lat), float(lng)))

    out, missing, src = [], [], {}
    for code, name in sorted(stations.items()):
        if code in CURATED:                            # curated takes precedence (no fuzzy slip-in)
            coord = CURATED[code]; src[code] = "curated"
        else:
            coord = osm.get(norm(name))                # STRICT exact normalized-name match only
            if coord: src[code] = "osm"
        if coord:
            out.append({"code": code, "nameEn": name, "lat": coord[0], "lng": coord[1]})
        else:
            missing.append(f"{code} ({name})")

    out.sort(key=lambda s: s["code"])

    # FLOOR GUARD (added 2026-07-30 after a real incident). When BOTH Overpass hosts fail, `osm` is
    # empty and only the ~6 CURATED codes match — this script then happily overwrote the good 97-row
    # file with 5 rows, and the nightly publish pushed that to the OTA host. Shipped apps survived
    # only because BundledData.staticArray("mtr_stations", minCount: 30) rejected the short override
    # and fell back to the bundle; an archive cut from that tree would have shipped a 5-station
    # bundle and dropped MTR heavy rail out of the planner entirely (loadMTR only creates GraphStops
    # for stations in this table). Refuse to write a degraded file — keep the last good one.
    MIN_STATIONS = 80
    if len(out) < MIN_STATIONS:
        raise SystemExit(
            f"ABORT: only matched {len(out)}/{len(stations)} stations (< {MIN_STATIONS}) — "
            f"almost certainly an Overpass outage, not a real change. "
            f"{OUT} left untouched. Re-run when Overpass is reachable."
        )

    tmp = OUT + ".tmp"
    json.dump(out, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, OUT)
    print(f"\n== matched {len(out)}/{len(stations)} -> {OUT}")
    if missing:
        print(f"UNMATCHED ({len(missing)}): " + ", ".join(missing))
    # Spot-check a few well-known stations.
    known = {"CEN": (22.2819, 114.1582), "ADM": (22.2796, 114.1652),
             "MOK": (22.3193, 114.1694), "TST": (22.2978, 114.1722)}
    print("\nSPOT-CHECK (code: got vs expected):")
    by = {s["code"]: s for s in out}
    for c, (elat, elng) in known.items():
        s = by.get(c)
        if s:
            dlat, dlng = abs(s["lat"] - elat), abs(s["lng"] - elng)
            ok = "OK" if dlat < 0.01 and dlng < 0.01 else "‼️ OFF"
            print(f"  {c}: ({s['lat']:.4f},{s['lng']:.4f}) vs ({elat},{elng}) {ok}")
        else:
            print(f"  {c}: MISSING")

if __name__ == "__main__":
    main()
