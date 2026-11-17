#!/usr/bin/env python3
"""Bounded, read-only provider diagnostics. No credentials, cache or publication."""
import argparse
import csv
import datetime
import hashlib
import io
import json
import platform
import ssl
import time
import urllib.error
import urllib.request
import zipfile

GMB = "https://data.etagmb.gov.hk/"
GMB_PATHS = (
    "route", "route-stop/2003051/1", "route-stop/2007438/2",
    "stop/20000004", "stop/20023197", "last-update/stop",
)
LRT_URLS = (
    "https://opendata.mtr.com.hk/data/light_rail_routes_and_stops.csv",
    "https://open.hkmapservice.gov.hk/OpenData/directDownload?productFormat=GEOJSON&productName=iGeoCom&sheetName=iGeoCom",
)
MAX_BYTES = 8 * 1024 * 1024
HEADERS = frozenset(("date", "content-type", "content-length", "server", "via", "x-cache",
                     "x-amz-cf-pop", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                     "x-ratelimit-reset", "cache-control", "age"))


def summarize(body, kind):
    if kind == "gmb":
        data = json.loads(body)
        payload = data.get("data")
        summary = {"type": data.get("type"), "generatedTimestamp": data.get("generated_timestamp"),
                   "dataShape": type(payload).__name__}
        if isinstance(payload, list):
            summary["rows"] = len(payload)
        elif isinstance(payload, dict):
            summary["sourceTimestamp"] = payload.get("data_timestamp")
            if isinstance(payload.get("route_stops"), list):
                summary["routeStops"] = len(payload["route_stops"])
            if isinstance(payload.get("routes"), dict):
                summary["routeCounts"] = {key: len(value) for key, value in payload["routes"].items()}
        return summary
    if kind == "lrt_csv":
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        codes = {row[2].strip() for row in rows if len(row) >= 7 and row[0] != "Line Code" and row[2].strip()}
        return {"csvRowsIncludingHeader": len(rows), "uniqueStopCodes": len(codes)}
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = [item for item in archive.infolist() if item.filename.lower().endswith(".geojson")]
        if len(members) != 1 or members[0].file_size > 64 * 1024 * 1024:
            raise ValueError("Expected one bounded GeoJSON member")
        geo = json.loads(archive.read(members[0]))
    features = geo.get("features", [])
    return {"geoJSONFeatures": len(features), "lightRailFeatures": sum(
        feature.get("properties", {}).get("TYPE") == "LRA" for feature in features)}


def collect(include_lrt=False, *, open_url=urllib.request.urlopen, sleep=time.sleep):
    targets = [(GMB + path, "gmb", "Departly-index/1.0") for path in GMB_PATHS]
    if include_lrt:
        targets.extend([(LRT_URLS[0], "lrt_csv", "Departly-lrt/1.0"),
                        (LRT_URLS[1], "lrt_geojson", "Departly-lrt/1.0")])
    context = ssl.create_default_context()
    results = []
    gmb_stop_reason = None
    attempted = 0
    for url, kind, agent in targets:
        record = {"url": url, "method": "GET", "attempts": 1, "verifiedTLS": True,
                  "requestUserAgent": agent, "startedUTC": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if kind == "gmb" and gmb_stop_reason:
            record.update({"attempts": 0, "skippedReason": gmb_stop_reason})
            results.append(record)
            print(json.dumps(record, ensure_ascii=True, sort_keys=True), flush=True)
            continue
        if attempted:
            sleep(3)
        attempted += 1
        record["startedUTC"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        start = time.monotonic()
        try:
            request = urllib.request.Request(url, method="GET", headers={"User-Agent": agent})
            try:
                response = open_url(request, timeout=20, context=context)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                record["status"] = response.code
                record["finalURL"] = response.geturl()
                record["headers"] = {key: value[:300] for key, value in response.headers.items()
                                     if key.lower() in HEADERS}
                retry_after = next((value for key, value in record["headers"].items()
                                    if key.lower() == "retry-after"), "")
                if kind == "gmb" and (response.code == 429 or retry_after.strip()):
                    # Stop this host's remaining diagnostics instead of retrying or exceeding
                    # a requested cooldown. Independent official LRT providers can still run.
                    gmb_stop_reason = "GMB requested rate limiting (HTTP429 or Retry-After); no further GMB requests"
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("Response exceeds eight MiB diagnostic limit")
            record.update({"bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
            if record["status"] == 200:
                record["summary"] = summarize(body, kind)
            else:
                # JSON escaping prevents a provider body from injecting workflow commands.
                record["errorBodyPrefix"] = body[:700].decode("utf-8", errors="replace")
        except Exception as error:
            record["error"] = type(error).__name__ + ": " + str(error)
        record["elapsedSeconds"] = round(time.monotonic() - start, 3)
        results.append(record)
        print(json.dumps(record, ensure_ascii=True, sort_keys=True), flush=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-lrt", action="store_true", help="Add two official LRT source GETs")
    args = parser.parse_args()
    results = collect(args.include_lrt)
    passed = all(row.get("status") == 200 and "error" not in row for row in results)
    print(json.dumps({"result": "PASS" if passed else "PROVIDER_FAILURE_OBSERVED",
                      "requests": sum(row["attempts"] for row in results), "targets": len(results),
                      "python": platform.python_version(), "scope": "Single-attempt sparse diagnostics, no persistence or publication",
                      "limitations": "Sparse success does not establish sustained request limits or full source coverage"}, sort_keys=True))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
