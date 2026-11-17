#!/usr/bin/env python3
"""Generate slim adult-Octopus fare tables for MTR heavy rail + Light Rail from MTR open data.

  mtr_fares.json : { "NORMNAME|NORMNAME": fare }   (keyed by normalized station English-name pair)
  lrt_fares.json : { "fromID-toID": fare }          (keyed by numeric LRT station-id pair)

MTR is keyed by NAME (the journey graph uses station codes→names) and LRT by id (the graph maps
code→numeric station_id via MTRStore). Adult Octopus fare only (OCT_ADT_FARE / fare_octo_adult).
"""
import csv, io, json, os, re, ssl, urllib.request

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Departly", "Resources", "transit_index")
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE

def norm(s): return re.sub(r"[^a-z0-9]", "", s.lower())

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Departly-fares/1.0"})
    with urllib.request.urlopen(req, timeout=60, context=_ctx) as r:
        return r.read().decode("utf-8-sig")

def _f(r, key):
    try: return round(float(r.get(key, "0")), 1)
    except ValueError: return 0.0

def build_mtr():
    rows = list(csv.DictReader(io.StringIO(fetch("https://opendata.mtr.com.hk/data/mtr_lines_fares.csv"))))
    out = {}
    for r in rows:
        src, dst = r.get("SRC_STATION_NAME", ""), r.get("DEST_STATION_NAME", "")
        a = _f(r, "OCT_ADT_FARE")
        if not src or not dst or a <= 0: continue
        # per-category Octopus fares: adult / child / elderly / student (all published in the CSV)
        out[f"{norm(src)}|{norm(dst)}"] = {"a": a, "c": _f(r, "OCT_CON_CHILD_FARE"),
                                           "e": _f(r, "OCT_CON_ELDERLY_FARE"), "s": _f(r, "OCT_STD_FARE")}
    return out

def build_lrt():
    rows = list(csv.DictReader(io.StringIO(fetch("https://opendata.mtr.com.hk/data/light_rail_fares.csv"))))
    out = {}
    for r in rows:
        a, b = r.get("from_station_id", ""), r.get("to_station_id", "")
        adult = _f(r, "fare_octo_adult")
        if not a or not b or adult <= 0: continue
        out[f"{a}-{b}"] = {"a": adult, "c": _f(r, "fare_octo_child"),
                           "e": _f(r, "fare_octo_elderly"), "s": _f(r, "fare_octo_student")}
    return out

def main():
    os.makedirs(OUT, exist_ok=True)
    for name, obj in [("mtr_fares.json", build_mtr()), ("lrt_fares.json", build_lrt())]:
        p = os.path.join(OUT, name)
        json.dump(obj, open(p + ".tmp", "w"), ensure_ascii=False, separators=(",", ":")); os.replace(p + ".tmp", p)
        print(f"== {name}: {len(obj)} fares")

if __name__ == "__main__":
    main()
