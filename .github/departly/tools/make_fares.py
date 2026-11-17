#!/usr/bin/env python3
"""Build a slimmed bus-fares table from the TD "Routes & Fares" open dataset.

Downloads ROUTE_BUS.xml (1.6 MB) and emits Departly/Resources/bus_fares.json — one record
per route-direction with the FULL_FARE (adult, boarding at origin). Looked up at runtime by
route number + company + destination name. Re-run when fares change (monthly-ish).
"""
import json, os, urllib.request, xml.etree.ElementTree as ET

URL = "https://static.data.gov.hk/td/routes-fares-xml/ROUTE_BUS.xml"
OUT = os.path.join(os.path.dirname(__file__), "..", "Departly", "Resources", "bus_fares.json")

def t(node, tag):
    return (node.findtext(tag) or "").strip()

def main():
    with urllib.request.urlopen(URL, timeout=60) as r:
        data = r.read()
    root = ET.fromstring(data)
    rows = []
    for rec in root.findall("ROUTE"):
        co = t(rec, "COMPANY_CODE")
        # Only operators Departly surfaces (KMB/LWB + Citybus; joint routes contain both).
        if not ("KMB" in co or "CTB" in co or "LWB" in co):
            continue
        fare = t(rec, "FULL_FARE")
        if not fare:
            continue
        rows.append({
            "r": t(rec, "ROUTE_NAMEE"),
            "co": co,
            "d": t(rec, "LOC_END_NAMEE"),
            "o": t(rec, "LOC_START_NAMEE"),
            "f": float(fare),
            "sp": int(t(rec, "SPECIAL_TYPE") or "0"),
            "m": t(rec, "SERVICE_MODE") or "R",
        })
    os.makedirs(os.path.dirname(os.path.normpath(OUT)), exist_ok=True)
    with open(os.path.normpath(OUT), "w") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {len(rows)} fare rows -> {os.path.normpath(OUT)} ({os.path.getsize(os.path.normpath(OUT))} bytes)")

if __name__ == "__main__":
    main()
