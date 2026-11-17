#!/usr/bin/env python3
"""Refresh all Light Rail coordinates from official free MTR and Lands Department data.

MTR's route CSV supplies stable alphabetic Stop Code, numeric Stop ID and names.
LandsD iGeoCom supplies 68 TYPE=LRA WGS84 points, including Hoi Wong Road. Join
unique English AND Chinese names without discarding North/South qualifiers.
Both outputs are validated in memory before replacement; failures retain the
prior CSV/coordinates and exit nonzero. No Overpass, credentials or paid API.

Sources:
https://data.gov.hk/en-data/dataset/mtr-data-routes-fares-barrier-free-facilities
https://data.gov.hk/en-data/dataset/hk-landsd-openmap-development-hkms-digital-geocom
"""
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile

import hkmap_tls

ROOT = Path(__file__).resolve().parent
RES = ROOT.parent / "Departly/Resources"
CSV = RES / "light_rail_routes_and_stops.csv"
OUT = RES / "lrt_stations.json"
MTR_URL = "https://opendata.mtr.com.hk/data/light_rail_routes_and_stops.csv"
LANDSD_URL = "https://open.hkmapservice.gov.hk/OpenData/directDownload?productFormat=GEOJSON&productName=iGeoCom&sheetName=iGeoCom"
MIN_STATIONS = 50
MAX_ZIP_BYTES = 16 * 1024 * 1024
MAX_GEOJSON_BYTES = 96 * 1024 * 1024


class SourceError(ValueError):
    pass


class PublicationRecoveryError(SourceError):
    """Output pair could not be restored; caller must refuse publication (exit 2)."""


def fetch_bytes(url, limit):
    request = urllib.request.Request(url, headers={"User-Agent": "Departly-official-lrt/1.0"})
    # LandsD currently omits an intermediate. Its pinned chain material completes
    # verification against system roots; this does not grant partial-chain trust.
    # Other sources use the unchanged default HTTPS context.
    options = {"context": hkmap_tls.verified_context()} if url == LANDSD_URL else {}
    with urllib.request.urlopen(request, timeout=30, **options) as response:
        final = urllib.parse.urlsplit(response.geturl())
        initial = urllib.parse.urlsplit(url)
        if final.scheme != "https" or final.hostname != initial.hostname:
            raise SourceError("Unexpected source redirect")
        body = response.read(limit + 1)
    if not body or len(body) > limit:
        raise SourceError("Empty or oversized source response")
    return body


def english_key(text):
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def chinese_key(text):
    return re.sub(r"[\W_]+", "", text)


def parse_mtr_csv(raw):
    required = {"Line Code", "Direction", "Stop Code", "Stop ID", "Chinese Name", "English Name", "Sequence"}
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not required.issubset(reader.fieldnames or []):
        raise SourceError("MTR CSV required headers missing")
    stations, numeric_codes, paths = {}, {}, {}
    for row in reader:
        if None in row or any(row.get(key) is None for key in required):
            raise SourceError("Malformed MTR CSV row")
        values = {key: row[key].strip() for key in required}
        code, numeric = values["Stop Code"], values["Stop ID"]
        if not re.fullmatch(r"[A-Z]{3}", code) or not re.fullmatch(r"[0-9]+", numeric):
            raise SourceError("Invalid alphabetic stop code or numeric station ID")
        numeric = str(int(numeric))
        name_en, name_tc = values["English Name"], values["Chinese Name"]
        if not english_key(name_en) or not chinese_key(name_tc):
            raise SourceError("Missing station name")
        item = {"code": code, "numericID": numeric, "nameEn": name_en, "nameTc": name_tc}
        if code in stations and stations[code] != item:
            raise SourceError("Conflicting MTR identity for " + code)
        if numeric in numeric_codes and numeric_codes[numeric] != code:
            raise SourceError("One numeric station ID maps to multiple alphabetic codes")
        stations[code], numeric_codes[numeric] = item, code
        line, direction = values["Line Code"], values["Direction"]
        if not re.fullmatch(r"[0-9]{3}[A-Z]?", line) or direction not in {"1", "2"}:
            raise SourceError("Invalid MTR line/direction")
        try:
            sequence = float(values["Sequence"])
        except ValueError:
            raise SourceError("Invalid MTR route sequence")
        if not math.isfinite(sequence) or not sequence.is_integer() or sequence < 1:
            raise SourceError("Invalid MTR route sequence")
        path = paths.setdefault((line, direction), {})
        if int(sequence) in path:
            raise SourceError("Duplicate MTR route position")
        path[int(sequence)] = code
    if len(stations) < MIN_STATIONS:
        raise SourceError("MTR station count below existing 50-stop floor")
    for path in paths.values():
        if len(path) < 2 or set(path) != set(range(1, len(path)+1)):
            raise SourceError("Incomplete MTR route sequence")
    for field, normalize in (("nameEn", english_key), ("nameTc", chinese_key)):
        if len({normalize(row[field]) for row in stations.values()}) != len(stations):
            raise SourceError("Ambiguous MTR station names")
    return stations


def reject_json_constant(value):
    raise SourceError("Non-finite JSON value: " + value)


def parse_landsd_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [member for member in archive.infolist() if member.filename.lower().endswith(".geojson")]
        if len(members) != 1 or members[0].file_size > MAX_GEOJSON_BYTES:
            raise SourceError("Expected one bounded iGeoCom GeoJSON member")
        # Read in memory; never extract untrusted ZIP paths into the filesystem.
        with archive.open(members[0]) as stream:
            body = stream.read(MAX_GEOJSON_BYTES + 1)
        if len(body) > MAX_GEOJSON_BYTES:
            raise SourceError("Oversized iGeoCom GeoJSON")
    data = json.loads(body, parse_constant=reject_json_constant)
    if data.get("type") != "FeatureCollection" or not isinstance(data.get("features"), list) or data.get("crs") is not None:
        raise SourceError("Expected WGS84 GeoJSON FeatureCollection without a CRS override")
    by_name, chinese_names, coordinate_pairs, identities = {}, set(), set(), set()
    for feature in data["features"]:
        properties = feature.get("properties") or {}
        if properties.get("TYPE") != "LRA":
            continue
        geometry = feature.get("geometry") or {}
        point = geometry.get("coordinates")
        if properties.get("CLASS") != "TRS" or geometry.get("type") != "Point" or not isinstance(point, list) or len(point) != 2:
            raise SourceError("Malformed Light Rail point")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point):
            raise SourceError("Invalid Light Rail coordinate")
        lng, lat = point
        # A broad NW New Territories envelope catches reversed axes, HK80 metres,
        # zeros and wrong-region points; it does not invent or move any coordinate.
        if not (22.35 <= lat <= 22.50 and 113.90 <= lng <= 114.08):
            raise SourceError("Light Rail coordinate outside NW Hong Kong")
        name_en, name_tc = properties.get("ENGLISHNAME"), properties.get("CHINESENAME")
        if not isinstance(name_en, str) or not name_en.startswith("LR-") or not isinstance(name_tc, str) or not name_tc.startswith("輕鐵－"):
            raise SourceError("Missing or unexpected LandsD Light Rail name")
        key, tc_key = english_key(name_en[3:]), chinese_key(name_tc[3:])
        identity = properties.get("GEONAMEID")
        if not key or not tc_key or identity is None:
            raise SourceError("Missing LandsD station identity")
        if key in by_name or tc_key in chinese_names or (lat, lng) in coordinate_pairs or identity in identities:
            raise SourceError("Ambiguous/duplicate LandsD station name, identity or coordinate")
        by_name[key] = {"nameTcKey": tc_key, "lat": lat, "lng": lng, "geonameID": identity}
        chinese_names.add(tc_key)
        coordinate_pairs.add((lat, lng))
        identities.add(identity)
    if len(by_name) < MIN_STATIONS:
        raise SourceError("LandsD station count below existing 50-stop floor")
    return by_name


def build_outputs(csv_bytes, zip_bytes, previous_csv=None):
    stations = parse_mtr_csv(csv_bytes)
    points = parse_landsd_zip(zip_bytes)
    if previous_csv is not None:
        old_codes = set(parse_mtr_csv(previous_csv))
        if not old_codes.issubset(stations):
            raise SourceError("MTR source lost previously known station codes")
    if {english_key(station["nameEn"]) for station in stations.values()} != set(points):
        raise SourceError("Incomplete/extra station coverage between MTR and LandsD")
    out = []
    for code, station in sorted(stations.items()):
        point = points[english_key(station["nameEn"])]
        if chinese_key(station["nameTc"]) != point["nameTcKey"]:
            raise SourceError("English/Chinese station identity disagrees for " + code)
        out.append({"code": code, "nameEn": station["nameEn"], "lat": point["lat"], "lng": point["lng"]})
    return out


def publish_pair(outputs):
    """Stage both files before replacement; roll back any ordinary write/replace failure."""
    staged, backups, replaced, recovery_backups = {}, {}, [], set()
    rollback_incomplete = False
    try:
        for destination, payload in outputs:
            destination = Path(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".lrt-stage-", delete=False) as stream:
                staged[destination] = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if destination.exists():
                with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".lrt-backup-", delete=False) as stream:
                    backups[destination] = Path(stream.name)
                shutil.copy2(destination, backups[destination])
            else:
                backups[destination] = None
        for destination, payload in outputs:
            destination = Path(destination)
            os.replace(staged[destination], destination)
            replaced.append(destination)
    except BaseException as publication_error:
        rollback_errors = []
        for destination in reversed(replaced):
            try:
                if backups[destination] is None:
                    destination.unlink()
                else:
                    os.replace(backups[destination], destination)
            except OSError as rollback_error:
                backup = backups[destination]
                if backup is not None:
                    recovery_backups.add(backup)
                rollback_errors.append(f"{destination}: {rollback_error}; recovery backup: {backup}")
        if rollback_errors:
            rollback_incomplete = True
            raise PublicationRecoveryError("Publication failed and rollback incomplete; outputs may be mixed. " + "; ".join(rollback_errors)) from publication_error
        raise
    finally:
        for temporary in list(staged.values()) + [p for p in backups.values() if p is not None]:
            try:
                if temporary not in recovery_backups and temporary.exists():
                    temporary.unlink()
            except OSError as cleanup_error:
                if not rollback_incomplete:
                    raise
                # Cleanup must never hide the unsafe-pair exception/exit status.
                print(f"!! WARNING: retained temporary {temporary}: {cleanup_error}")


def main():
    csv_bytes = fetch_bytes(MTR_URL, 2 * 1024 * 1024)
    zip_bytes = fetch_bytes(LANDSD_URL, MAX_ZIP_BYTES)
    previous_csv = CSV.read_bytes() if CSV.exists() else None
    rows = build_outputs(csv_bytes, zip_bytes, previous_csv)
    output = (json.dumps(rows, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    publish_pair([(CSV, csv_bytes), (OUT, output)])
    print(f"MTR CSV SHA256 {hashlib.sha256(csv_bytes).hexdigest()}")
    print(f"LandsD iGeoCom ZIP SHA256 {hashlib.sha256(zip_bytes).hexdigest()}")
    print(f"== matched all {len(rows)} MTR Light Rail codes to unique official WGS84 points; CSV and coordinates updated")


if __name__ == "__main__":
    try:
        main()
    except PublicationRecoveryError as error:
        print(f"!! ABORT: unsafe LRT output pair: {type(error).__name__}: {error}")
        raise SystemExit(2)
    except Exception as error:
        print(f"!! ABORT: official LRT sources failed validation or publication: {type(error).__name__}: {error}")
        raise SystemExit(1)
