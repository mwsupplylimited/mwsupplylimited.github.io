"""Real LRT generator CLI with synthetic public-provider responses; never uses live HTTP."""
import copy
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


def fixture_sources():
    names = [("MIK", "200", "Ming Kum", "鳴琴"), ("TSP", "250", "Hoi Wong Road", "海皇路"),
             ("THN", "210", "Tai Hing (North)", "大興(北)"), ("THS", "220", "Tai Hing (South)", "大興(南)"),
             ("SKN", "180", "Shan King (North)", "山景(北)"), ("SKS", "190", "Shan King (South)", "山景(南)")]
    names += [("X" + chr(65+i//26) + chr(65+i%26), str(1000+i), "Fixture " + str(i), "測試" + str(i)) for i in range(62)]
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(["Line Code", "Direction", "Stop Code", "Stop ID", "Chinese Name", "English Name", "Sequence"])
    for direction, items in (("1", names), ("2", list(reversed(names)))):
        for seq, (code, numeric_id, english, chinese) in enumerate(items, 1):
            writer.writerow(["505", direction, code, numeric_id, chinese, english, str(seq)])
    features = []
    for i, (code, numeric_id, english, chinese) in enumerate(names):
        features.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [113.96+i/10000, 22.37+i/1000]},
                         "properties": {"GEONAMEID": 500000+i, "CLASS": "TRS", "TYPE": "LRA",
                                        "ENGLISHNAME": "LR-"+english, "CHINESENAME": "輕鐵－"+chinese, "REV_DATE": "24/6/2026"}})
    return stream.getvalue().encode("utf-8-sig"), {"type": "FeatureCollection", "features": features}


def zipped(geo):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("iGeoCOM_POI.geojson", json.dumps(geo, ensure_ascii=False))
    return stream.getvalue()


class LRTSourceTests(unittest.TestCase):
    def run_generator(self, mode="valid"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        tools = root / "tools"
        resources = root / "Departly/Resources"
        tools.mkdir()
        resources.mkdir(parents=True)
        script = tools / "build_lrt_stations.py"
        shutil.copy2(Path(__file__).with_name(script.name), script)
        shutil.copy2(Path(__file__).with_name("hkmap_tls.py"), tools / "hkmap_tls.py")
        shutil.copytree(Path(__file__).with_name("certs"), tools / "certs")
        csv_bytes, geo = fixture_sources()
        current_csv = csv_bytes
        if mode == "missing_station":
            geo["features"].pop()
        elif mode == "floor_49":
            geo["features"] = geo["features"][:49]
        elif mode == "duplicate_name":
            duplicate = copy.deepcopy(geo["features"][0])
            duplicate["properties"]["GEONAMEID"] = 999999
            geo["features"].append(duplicate)
        elif mode == "chinese_disagreement":
            geo["features"][0]["properties"]["CHINESENAME"] = "輕鐵－別處"
        elif mode == "reversed_coordinate":
            geo["features"][0]["geometry"]["coordinates"].reverse()
        elif mode == "nonfinite_coordinate":
            geo["features"][0]["geometry"]["coordinates"][0] = float("nan")
        elif mode == "wrong_geometry":
            geo["features"][0]["geometry"]["type"] = "LineString"
        elif mode == "duplicate_coordinate":
            geo["features"][3]["geometry"]["coordinates"] = geo["features"][2]["geometry"]["coordinates"][:]
        elif mode == "conflicting_code":
            current_csv = csv_bytes.replace(b"MIK,200", b"MIK,999", 1)
        elif mode == "missing_csv_code":
            current_csv = b"\n".join(line for line in csv_bytes.splitlines() if b",MIK," not in line) + b"\n"
        elif mode == "malformed_csv":
            current_csv = b"unexpected,headers\n1,2\n"
        elif mode == "changed_crs":
            geo["crs"] = {"type": "name", "properties": {"name": "EPSG:2326"}}
        baseline_csv = resources / "light_rail_routes_and_stops.csv"
        baseline_lrt = resources / "lrt_stations.json"
        # A genuine rename changes display text but retains TSP / numeric station250.
        baseline_csv.write_bytes(csv_bytes.replace("海皇路".encode(), "屯門泳池".encode()).replace(b"Hoi Wong Road", b"Tuen Mun Swimming Pool"))
        baseline_lrt.write_bytes(b'[{"code":"MIK","nameEn":"Ming Kum","lat":22.37,"lng":113.96}]\n')
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in (baseline_csv, baseline_lrt)}
        fixture_csv = root / "mtr-source.csv"
        fixture_zip = root / "lands-source.zip"
        fixture_csv.write_bytes(current_csv)
        fixture_zip.write_bytes(b"not a ZIP" if mode == "malformed_zip" else zipped(geo))
        runner = r'''
import io, json, os, pathlib, runpy, ssl, sys, urllib.error
from unittest.mock import patch
script, fixture_csv, fixture_zip, mode = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).parent))
calls = []
class Response(io.BytesIO):
    status = 200
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url
    def geturl(self):
        return self.url
def response(request, **kwargs):
    url = request.full_url
    calls.append(url)
    if "overpass" in url:
        raise urllib.error.HTTPError(url, 502, "Synthetic Overpass outage", {}, None)
    if url == "https://opendata.mtr.com.hk/data/light_rail_routes_and_stops.csv":
        assert "context" not in kwargs, "MTR must retain its default TLS context"
        return Response(pathlib.Path(fixture_csv).read_bytes(), url)
    if url == "https://open.hkmapservice.gov.hk/OpenData/directDownload?productFormat=GEOJSON&productName=iGeoCom&sheetName=iGeoCom":
        context = kwargs.get("context")
        assert isinstance(context, ssl.SSLContext) and context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        assert not context.verify_flags & getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
        if mode == "tls_error":
            raise urllib.error.URLError(ssl.SSLCertVerificationError("Synthetic untrusted peer"))
        if mode == "lands_outage":
            raise urllib.error.HTTPError(url, 502, "Synthetic LandsD outage", {}, None)
        return Response(pathlib.Path(fixture_zip).read_bytes(), url)
    raise AssertionError("Unexpected provider URL: " + url)
replace = os.replace
replace_failed = False
def replacement(source, destination):
    global replace_failed
    if mode in {"replace_once", "rollback_failure", "rollback_cleanup_failure"}:
        if pathlib.Path(destination).name == "lrt_stations.json" and not replace_failed:
            replace_failed = True
            raise OSError("Synthetic second output failure")
        if mode.startswith("rollback_") and pathlib.Path(source).name.startswith(".lrt-backup-"):
            raise OSError("Synthetic persistent restore failure")
    return replace(source, destination)
unlink = pathlib.Path.unlink
def cleanup(path, **kwargs):
    if mode == "rollback_cleanup_failure":
        raise OSError("Synthetic cleanup failure")
    return unlink(path, **kwargs)
with patch("urllib.request.urlopen", side_effect=response), patch("os.replace", side_effect=replacement), patch("pathlib.Path.unlink", autospec=True, side_effect=cleanup):
    try:
        runpy.run_path(script, run_name="__main__")
    finally:
        print("MOCK_REQUESTS=" + json.dumps(calls))
'''
        result = subprocess.run([sys.executable, "-c", runner, str(script), str(fixture_csv), str(fixture_zip), mode],
                                capture_output=True, text=True, timeout=15)
        return result, baseline_csv, baseline_lrt, before, current_csv

    def assert_preserved(self, case):
        result, csv_path, lrt_path, before, _ = self.run_generator(case)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for path in (csv_path, lrt_path):
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before[path.name])
        self.assertEqual(sorted(p.name for p in csv_path.parent.iterdir()), sorted(before))

    def test_official_sources_replace_overpass_and_write_complete_pair(self):
        result, csv_path, lrt_path, before, expected_csv = self.run_generator()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(csv_path.read_bytes(), expected_csv)
        rows = json.loads(lrt_path.read_bytes())
        self.assertEqual(len(rows), 68)
        by_code = {r["code"]: r for r in rows}
        self.assertEqual(len(by_code), 68)
        self.assertEqual(by_code["TSP"]["nameEn"], "Hoi Wong Road")
        self.assertEqual(by_code["MIK"]["nameEn"], "Ming Kum")
        self.assertNotIn("200", by_code)
        self.assertNotIn("250", by_code)
        self.assertEqual(by_code["MIK"]["lat"], 22.37)
        self.assertNotEqual(by_code["THN"]["lat"], by_code["THS"]["lat"])
        self.assertNotEqual(by_code["SKN"]["lat"], by_code["SKS"]["lat"])
        calls = json.loads(result.stdout.split("MOCK_REQUESTS=", 1)[1])
        self.assertEqual(len(calls), 2)
        self.assertTrue(all("overpass" not in url for url in calls))

    def test_source_outage_preserves_both_baselines(self):
        self.assert_preserved("lands_outage")

    def test_untrusted_tls_peer_preserves_both_baselines(self):
        self.assert_preserved("tls_error")

    def test_cli_distinguishes_unsafe_rollback_from_retained_baseline(self):
        self.assert_preserved("replace_once")
        result, csv_path, lrt_path, before, expected_csv = self.run_generator("rollback_failure")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("PublicationRecoveryError", result.stdout)
        self.assertIn("outputs may be mixed", result.stdout)
        self.assertEqual(csv_path.read_bytes(), expected_csv)
        self.assertEqual((lrt_path.read_bytes(), lrt_path.stat().st_mtime_ns), before[lrt_path.name])
        backups = list(csv_path.parent.glob(".lrt-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0].read_bytes(), backups[0].stat().st_mtime_ns), before[csv_path.name])
        self.assertIn(str(backups[0]), result.stdout)

    def test_cleanup_error_cannot_mask_unsafe_cli_exit(self):
        result, csv_path, lrt_path, before, _ = self.run_generator("rollback_cleanup_failure")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("PublicationRecoveryError", result.stdout)
        self.assertIn("Synthetic cleanup failure", result.stdout)
        self.assertEqual((lrt_path.read_bytes(), lrt_path.stat().st_mtime_ns), before[lrt_path.name])
        recovered = [p for p in csv_path.parent.glob(".lrt-backup-*") if p.read_bytes() == before[csv_path.name][0]]
        self.assertEqual(len(recovered), 1)
        self.assertIn(str(recovered[0]), result.stdout)

    def test_missing_station_and_floor_refusal_preserve_both_baselines(self):
        for case in ("missing_station", "floor_49", "missing_csv_code"):
            with self.subTest(case=case):
                self.assert_preserved(case)

    def test_duplicate_names_and_conflicting_codes_are_rejected(self):
        for case in ("duplicate_name", "conflicting_code", "chinese_disagreement"):
            with self.subTest(case=case):
                self.assert_preserved(case)

    def test_invalid_coordinates_geometry_or_crs_are_rejected(self):
        for case in ("reversed_coordinate", "nonfinite_coordinate", "wrong_geometry", "duplicate_coordinate", "changed_crs"):
            with self.subTest(case=case):
                self.assert_preserved(case)

    def test_malformed_sources_preserve_both_baselines(self):
        for case in ("malformed_csv", "malformed_zip"):
            with self.subTest(case=case):
                self.assert_preserved(case)

    def test_second_output_failure_rolls_back_first_output(self):
        spec = importlib.util.spec_from_file_location("lrt_publisher_fixture", Path(__file__).with_name("build_lrt_stations.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "routes.csv", root / "stations.json"
            first.write_bytes(b"old csv")
            second.write_bytes(b"old coordinates")
            before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (first, second)}
            replace = module.os.replace
            failed = [False]
            def fail_once(source, destination):
                if Path(destination) == second and not failed[0]:
                    failed[0] = True
                    raise OSError("Synthetic second-output replacement failure")
                return replace(source, destination)
            with patch.object(module.os, "replace", side_effect=fail_once):
                with self.assertRaisesRegex(OSError, "Synthetic"):
                    module.publish_pair([(first, b"new csv"), (second, b"new coordinates")])
            self.assertTrue(failed[0])
            for path in (first, second):
                self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before[path])
            self.assertEqual(set(root.iterdir()), {first, second})

    def test_persistent_rollback_failure_retains_recovery_backup(self):
        spec = importlib.util.spec_from_file_location("lrt_rollback_fixture", Path(__file__).with_name("build_lrt_stations.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "routes.csv", root / "stations.json"
            first.write_bytes(b"old csv")
            second.write_bytes(b"old coordinates")
            old_mtime = first.stat().st_mtime_ns
            replace = module.os.replace
            def fail_publication_and_rollback(source, destination):
                if Path(destination) == second or Path(source).name.startswith(".lrt-backup-"):
                    raise OSError("Persistent synthetic filesystem failure")
                return replace(source, destination)
            with patch.object(module.os, "replace", side_effect=fail_publication_and_rollback):
                with self.assertRaisesRegex(module.PublicationRecoveryError, "rollback incomplete; outputs may be mixed") as raised:
                    module.publish_pair([(first, b"new csv"), (second, b"new coordinates")])
            self.assertEqual(first.read_bytes(), b"new csv")
            self.assertEqual(second.read_bytes(), b"old coordinates")
            backups = list(root.glob(".lrt-backup-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual((backups[0].read_bytes(), backups[0].stat().st_mtime_ns), (b"old csv", old_mtime))
            self.assertIn(str(backups[0]), str(raised.exception))
            self.assertEqual(set(root.iterdir()), {first, second, backups[0]})


if __name__ == "__main__":
    unittest.main(verbosity=2)
