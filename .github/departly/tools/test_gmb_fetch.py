"""GMB requests use injected HTTP/time and temporary files; never contact live providers."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


class Clock:
    def __init__(self):
        self.now = 0.0
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.now += seconds


class Response(io.BytesIO):
    pass


def stop(latitude=22.5):
    return {"type": "Stop", "data": {"coordinates": {"wgs84": {"latitude": latitude, "longitude": 114.0}}}}


def route_stops():
    # The repeated final stop is a legitimate circular arrival, not a duplicate sequence error.
    return {"type": "Route-Stop", "data": {"route_stops": [
        {"stop_seq": i, "stop_id": sid, "name_en": "Fixture", "name_tc": "測試"}
        for i, sid in enumerate((20001, 20002, 20001), 1)]}}


class GMBFetchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source = Path(__file__).with_name("build_transit_index.py")
        spec = importlib.util.spec_from_file_location("fixture_gmb", source)
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.mod.CACHE = str(self.root)
        self.clock = Clock()
        self.mod.GMB_GATE = self.mod.GMBRequestGate(self.clock.read, self.clock.sleep)
        self.url = "https://data.etagmb.gov.hk/stop/20001"
        self.calls = []

    def cache(self, url=None):
        return self.root / (hashlib.sha1((url or self.url).encode()).hexdigest() + ".json")

    def request_sequence(self, values):
        values = iter(values)
        def request(req, *, timeout, context):
            self.assertEqual(timeout, 30)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            self.assertEqual(req.get_header("User-agent"), "Departly-index/1.0")
            self.calls.append((req.full_url, self.clock.read()))
            item = next(values)
            if isinstance(item, Exception):
                raise item
            return Response(item if isinstance(item, bytes) else json.dumps(item).encode())
        return patch.object(self.mod.urllib.request, "urlopen", side_effect=request)

    def error(self, code, retry_after=None):
        return urllib.error.HTTPError(self.url, code, "Fixture", {} if retry_after is None else {"Retry-After": retry_after}, io.BytesIO(b""))

    def test_valid_body_and_valid_cache_use_verified_tls_without_extra_request(self):
        with self.request_sequence([stop()]):
            self.assertEqual(self.mod.get(self.url), stop())
            self.assertEqual(self.mod.get(self.url), stop())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.cache().read_text()), stop())
        self.assertFalse(list(self.root.glob("*.tmp")))
        self.assertEqual(self.mod.FETCH_FAILURES, 0)

    def test_permanent_403_and_404_never_retry_or_overwrite_previous_cache(self):
        for code in (403, 404):
            with self.subTest(code=code):
                self.cache().write_text("previous raw bytes")
                with self.request_sequence([self.error(code)]):
                    self.assertIsNone(self.mod.get(self.url))
                self.assertEqual(self.cache().read_text(), "previous raw bytes")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.mod.FETCH_FAILURES, 2)

    def test_http_diagnostic_escapes_commands_bounds_body_and_excludes_private_headers(self):
        body = b"blocked\n::error::not a workflow command\n##[error]legacy command\n" + b"x" * 600
        error = urllib.error.HTTPError(self.url, 403, "Fixture", {
            "Date": "Tue, 17 Nov 2026 12:00:00 GMT", "Server": "fixture\n::error::fake ##[error]legacy",
            "X-Cache": "Error from cloudfront", "Set-Cookie": "secret-cookie",
            "Authorization": "secret-authorization"}, io.BytesIO(body))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.request_sequence([error]):
            self.assertIsNone(self.mod.get(self.url))
        self.assertEqual(len(self.calls), 1)
        lines = output.getvalue().splitlines()
        diagnostic_lines = [line for line in lines if line.startswith("GMB_HTTP_ERROR ")]
        self.assertEqual(len(diagnostic_lines), 1)
        self.assertFalse(any(line.startswith("::") for line in lines))
        self.assertNotIn("##[", output.getvalue())
        self.assertNotIn("secret-cookie", output.getvalue())
        self.assertNotIn("secret-authorization", output.getvalue())
        diagnostic = json.loads(diagnostic_lines[0].split(" ", 1)[1])
        self.assertEqual(diagnostic["bodyPrefix"], body[:350].decode())
        self.assertEqual(set(diagnostic["headers"]), {"Date", "Server", "X-Cache"})
        self.assertEqual(diagnostic["headers"]["Server"], "fixture\n::error::fake ##[error]legacy")

    def test_network_server_and_429_retries_share_global_start_pace(self):
        sequence = [urllib.error.URLError("fixture network outage"), self.error(503), self.error(429, "5"), stop(), stop()]
        with self.request_sequence(sequence):
            self.assertEqual(self.mod.get(self.url), stop())
            self.assertEqual(self.mod.get(self.url + "2"), stop())
        times = [t for _, t in self.calls]
        self.assertEqual(len(times), 5)
        self.assertTrue(all(b - a >= 1 for a, b in zip(times, times[1:])))
        self.assertGreaterEqual(times[3] - times[2], 5)
        self.assertEqual(self.mod.FETCH_FAILURES, 0)

    def test_server_retry_after_applies_to_following_urls_too(self):
        with self.request_sequence([self.error(503, "12"), stop()]):
            self.assertIsNone(self.mod.get(self.url, tries=1))
            self.assertEqual(self.mod.get(self.url + "2"), stop())
        self.assertGreaterEqual(self.calls[1][1] - self.calls[0][1], 12)

    def test_retry_after_http_date_is_normalized_and_bounded(self):
        with patch.object(self.mod.time, "time", return_value=0):
            self.assertEqual(self.mod._gmb_retry_after("Thu, 01 Jan 1970 00:00:30 GMT"), 30)
            self.assertEqual(self.mod._gmb_retry_after("0"), 0)
        for value in ("999999", "not a date"):
            with self.subTest(value=value):
                self.mod.GMB_GATE = self.mod.GMBRequestGate(self.clock.read, self.clock.sleep)
                before = len(self.calls)
                with self.request_sequence([self.error(429, value)]):
                    self.assertIsNone(self.mod.get(self.url))
                    self.assertIsNone(self.mod.get(self.url + "2"))
                self.assertEqual(len(self.calls) - before, 1)

    def test_four_attempt_limit_and_certificate_failure_do_not_disable_tls(self):
        with self.request_sequence([self.error(500) for _ in range(4)]):
            self.assertIsNone(self.mod.get(self.url, tries=99))
        self.assertEqual(len(self.calls), 4)
        with self.request_sequence([urllib.error.URLError(ssl.SSLCertVerificationError("fixture invalid certificate"))]):
            self.assertIsNone(self.mod.get(self.url + "2"))
        self.assertEqual(len(self.calls), 5)

    def test_malformed_success_is_not_retried_or_cached(self):
        invalid = [b"not JSON", {"type": "Stop", "data": {}}, stop(math.nan), stop(True), stop(0), stop(91)]
        for value in invalid:
            with self.subTest(value=value), self.request_sequence([value]):
                self.assertIsNone(self.mod.get(self.url))
                self.assertFalse(self.cache().exists())
        self.assertEqual(len(self.calls), len(invalid))
        self.assertEqual(self.mod.FETCH_FAILURES, len(invalid))

    def test_invalid_fresh_cache_is_refetched_and_atomic_failure_preserves_old_bytes(self):
        self.cache().write_text('{"type":"Stop","data":{}}')
        with self.request_sequence([stop()]):
            self.assertEqual(self.mod.get(self.url), stop())
        old = self.cache().read_bytes()
        os.utime(self.cache(), (0, 0))
        with self.request_sequence([stop(22.6)]), patch.object(self.mod.os, "replace", side_effect=OSError("fixture disk failure")):
            self.assertIsNone(self.mod.get(self.url))
        self.assertEqual(self.cache().read_bytes(), old)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_endpoint_id_direction_and_sequence_validation_preserves_circular_routes(self):
        listing = {"type": "Routes-All", "data": {"routes": {"HKI": ["1"], "KLN": ["1"], "NT": ["618"]}}}
        metadata = {"type": "Route", "data": [{"route_id": 2007438, "region": "NT", "route_code": "618", "directions": [{"route_seq": 1}, {"route_seq": 2}]}]}
        self.mod.validate_gmb("https://data.etagmb.gov.hk/route", listing)
        self.mod.validate_gmb("https://data.etagmb.gov.hk/route/NT/618", metadata)
        url = "https://data.etagmb.gov.hk/route-stop/2007438/2"
        self.mod.validate_gmb(url, route_stops())
        bad = copy.deepcopy(metadata); bad["data"][0]["region"] = "HKI"
        with self.assertRaises(ValueError):
            self.mod.validate_gmb("https://data.etagmb.gov.hk/route/NT/618", bad)
        bad = route_stops(); bad["data"]["route_stops"][1]["stop_seq"] = 1
        with self.assertRaises(ValueError):
            self.mod.validate_gmb(url, bad)
        bad = route_stops(); bad["data"]["route_stops"][0]["stop_id"] = True
        with self.assertRaises(ValueError):
            self.mod.validate_gmb(url, bad)
        with self.assertRaises(ValueError):
            self.mod.validate_gmb(url, {"type": "Route-Stop", "data": {"route_stops": []}})

    def test_invalid_route_response_triggers_existing_last_good_merge(self):
        self.mod.OUT = str(self.root)
        previous_stops = [{"id": "20001", "nameEn": "First", "nameTc": "甲", "lat": 22.5, "lng": 114.0},
                          {"id": "20002", "nameEn": "Last", "nameTc": "乙", "lat": 22.6, "lng": 114.1}]
        previous_routes = [{"route": "618", "dir": "2007438-2", "service": "NT", "stops": ["20001", "20002"]}]
        for kind, rows in (("stops", previous_stops), ("routes", previous_routes)):
            (self.root / ("gmb_" + kind + ".json")).write_text(json.dumps(rows))
        with self.request_sequence([{"type": "Route-Stop", "data": {"route_stops": []}}]):
            self.assertIsNone(self.mod.get("https://data.etagmb.gov.hk/route-stop/2007438/2"))
        self.mod.save("gmb", [], [])
        self.assertEqual(json.loads((self.root / "gmb_routes.json").read_text()), previous_routes)
        self.assertEqual(json.loads((self.root / "gmb_stops.json").read_text()), previous_stops)

    def test_cooldown_or_stop_can_arrive_while_another_worker_waits(self):
        for stop_waiter in (False, True):
            with self.subTest(stop_waiter=stop_waiter):
                clock = Clock()
                gate = self.mod.GMBRequestGate(clock.read, clock.sleep)
                self.assertEqual(gate.wait(), 0)
                extended = []
                def sleep(delay):
                    if not extended:
                        extended.append(True)
                        gate.stop("fixture cooldown stop") if stop_waiter else gate.defer(10)
                    clock.sleep(delay)
                gate.sleep = sleep
                outcomes = []
                def wait():
                    try: outcomes.append(gate.wait())
                    except RuntimeError as error: outcomes.append(str(error))
                worker = threading.Thread(target=wait, daemon=True)
                worker.start(); worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), "Gate held its lock while sleeping")
                self.assertEqual(outcomes, ["fixture cooldown stop"] if stop_waiter else [10])

    def test_concurrent_workers_cannot_reserve_more_than_one_start_per_second(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            starts = sorted(pool.map(lambda _: self.mod.GMB_GATE.wait(), range(12)))
        self.assertTrue(all(b - a >= 1 for a, b in zip(starts, starts[1:])))

    def test_worker_configuration_cannot_disable_or_exceed_clamp(self):
        source = Path(__file__).with_name("build_transit_index.py")
        for setting, expected in (("0", 1), ("2", 2), ("99", 3)):
            with self.subTest(setting=setting), patch.dict(os.environ, {"IDX_GMB_WORKERS": setting}):
                spec = importlib.util.spec_from_file_location("fixture_gmb_workers", source)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                self.assertEqual(module.GMB_WORKERS, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
