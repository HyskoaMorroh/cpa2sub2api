"""Local-only proof for bounded admin calibration, without source probes."""
import contextlib
import copy
import io
import pathlib
import socketserver
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool


class FlushedOutput(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def flush(self):
        self.flushes += 1
        return super().flush()


def account(aid):
    return {"id": aid, "name": "fixture-%03d" % aid, "notes": tool.NOTE_SENTINEL,
            "platform": "openai", "status": "active", "schedulable": True,
            "credentials": {"base_url": "https://fixture.invalid",
                            "model_mapping": {"gpt-6": "gpt-6"}}}


class PlansApi:
    def __init__(self, count=147, failure=None, dangerous=False):
        self.count = count
        self.failure = failure
        self.calls = 0
        self.plan_reads = 0
        self.writes = []
        self.plans = {1: {"id": 9, "enabled": True, "auto_recover": True,
                          "cron_expression": "*/30 * * * *"}} if dangerous else {}

    def _call(self, method, path, body=None, **kwargs):
        if method == "GET":
            self.calls += 1
            if self.failure is not None:
                raise self.failure
            return account(int(path.rsplit("/", 1)[-1]))
        self.writes.append(copy.deepcopy(body))
        self.plans[1].update(body)
        return {}

    def list_test_plans(self, aid):
        self.plan_reads += 1
        return [copy.deepcopy(self.plans[aid])] if aid in self.plans else []


class FixtureHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server = self.server
        self.request.settimeout(2)
        if server.mode == "tls":
            self.request.recv(1)
            server.stop.wait(2)
            return
        reader = self.request.makefile("rb")
        line = reader.readline(65536)
        headers = {}
        while True:
            part = reader.readline(65536)
            if part in (b"\r\n", b"\n", b""):
                break
            key, _, value = part.decode("ascii", "replace").partition(":")
            headers[key.lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        request_body = reader.read(length) if length else b""
        with server.lock:
            server.received.append((line.split(b" ", 1)[0].decode(), headers))
            server.request_bodies.append(request_body)
            index = len(server.received)
        try:
            if server.mode == "headers":
                raw = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n[]"
                for byte in raw:
                    self.request.sendall(bytes([byte]))
                    if server.stop.wait(.025):
                        break
            elif server.mode == "body":
                body = b" " * 45 + b"[]"
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " +
                                     str(len(body)).encode() + b"\r\n\r\n")
                for byte in body:
                    self.request.sendall(bytes([byte]))
                    if server.stop.wait(.025):
                        break
            elif server.mode == "redirect":
                self.request.sendall(b"HTTP/1.1 302 Found\r\nLocation: " +
                    server.redirect_to.encode() + b"\r\nContent-Length: 0\r\n\r\n")
            elif server.mode == "large":
                self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 99999999\r\n\r\n")
            else:
                if server.mode == "slow":
                    server.stop.wait(.55)
                code = 401 if server.mode == "unauthorized" else 429 if server.mode == "limited" else 500 if server.mode == "retry" and index == 1 else 200
                body = b"private-token-and-domain.fixture.invalid" if code != 200 else b"[]"
                if server.mode == "unicode":
                    body = '{"notes": "中文配置 😀"}'.encode("utf-8")
                retry = b"Retry-After: 60\r\n" if code == 429 else b""
                self.request.sendall(b"HTTP/1.1 " + str(code).encode() +
                    b" Fixture\r\n" + retry + b"Content-Length: " +
                    str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            pass


@contextlib.contextmanager
def fixture(mode):
    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
    server = Server(("127.0.0.1", 0), FixtureHandler)
    server.mode = mode
    server.stop = threading.Event()
    server.lock = threading.Lock()
    server.received = []
    server.request_bodies = []
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .025}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.stop.set()
        server.shutdown()
        server.server_close()
        thread.join(1)


def start_calibration(api, cfg, label="fixture", total=1):
    # The fallback reproduces the original silent/unbounded implementation.
    starter = getattr(tool, "_start_admin_calibration", None)
    return starter(api, cfg, label, total) if starter else None


class AdminCalibrationTests(unittest.TestCase):
    def test_actual_api_successful_account_reads_do_not_reset_failed_plan_items(self):
        import admin_io
        cfg = {"sub2api_base_url": "http://127.0.0.1:9",
               "sub2api_admin_key": "synthetic", "http_retries": 2}
        api = tool.Sub2Api(cfg)
        calls = {"accounts": 0, "plans": 0}

        def wire(method, url, headers, body, timeout, deadline, origin, progress=None):
            if "/scheduled-test-plans" in url or "/test-plans" in url:
                calls["plans"] += 1
                raise admin_io.Failure("http", 503)
            calls["accounts"] += 1
            return 200, tool.json.dumps(account(int(url.rsplit("/", 1)[-1])))

        with patch.object(admin_io, "request", side_effect=wire), \
             patch.object(tool, "load_settings", return_value=cfg), \
             patch.object(tool.time, "sleep"), contextlib.redirect_stdout(FlushedOutput()):
            _, failures = tool.ensure_test_plans(api, {
                "fixture-%03d" % aid: aid for aid in range(1, 11)}, {})
        self.assertEqual(calls, {"accounts": 3, "plans": 6})
        self.assertEqual(len(failures), 10)
        self.assertTrue(all("未校准" in reason for _, reason in failures[3:]))

    def test_actual_api_completed_plan_item_resets_consecutive_failures(self):
        import admin_io
        cfg = {"sub2api_base_url": "http://127.0.0.1:9",
               "sub2api_admin_key": "synthetic", "http_retries": 2}
        api = tool.Sub2Api(cfg)
        calls = {"accounts": 0, "plans": 0}

        def wire(method, url, headers, body, timeout, deadline, origin, progress=None):
            aid = int(url.split("/accounts/", 1)[1].split("/", 1)[0])
            if "/scheduled-test-plans" in url or "/test-plans" in url:
                calls["plans"] += 1
                if aid != 3:
                    raise admin_io.Failure("http", 503)
                return 200, "[]"
            calls["accounts"] += 1
            return 200, tool.json.dumps(account(aid))

        with patch.object(admin_io, "request", side_effect=wire), \
             patch.object(tool, "load_settings", return_value=cfg), \
             patch.object(tool.time, "sleep"), contextlib.redirect_stdout(FlushedOutput()):
            _, failures = tool.ensure_test_plans(api, {
                "fixture-%03d" % aid: aid for aid in range(1, 11)}, {})
        self.assertEqual(calls, {"accounts": 6, "plans": 11})
        self.assertEqual(len(failures), 9)
        self.assertNotIn("fixture-003", {name for name, _ in failures})

    def test_all_off_147_targets_have_flushed_progress_without_upstream(self):
        api = PlansApi()
        output = FlushedOutput()
        with contextlib.redirect_stdout(output):
            result = tool.ensure_test_plans(api, {"fixture-%03d" % i: i for i in range(1, 148)}, {})
        self.assertEqual(result, (0, []))
        self.assertEqual((api.calls, api.plan_reads), (147, 147))
        self.assertIn("[1/147]", output.getvalue())
        self.assertIn("[147/147]", output.getvalue())
        self.assertGreaterEqual(output.flushes, 147)
        self.assertEqual(api.writes, [])

    def test_three_connection_failures_stop_and_mark_all_remaining_unverified(self):
        api = PlansApi(failure=tool.ApiError(0, "private-token-and-domain.fixture.invalid", "<redacted>"))
        output = FlushedOutput()
        with contextlib.redirect_stdout(output):
            count, failures = tool.ensure_test_plans(api, {"fixture-%03d" % i: i for i in range(1, 148)}, {})
        self.assertEqual(count, 0)
        self.assertEqual(len(failures), 147)
        self.assertEqual(api.calls, 3)
        self.assertIn("144", output.getvalue())
        self.assertNotIn("private-token-and-domain", output.getvalue() + str(failures))

    def test_401_and_403_stop_without_hiding_the_status(self):
        for status in (401, 403):
            api = PlansApi(failure=tool.ApiError(status, "private-token-and-domain.fixture.invalid", "<redacted>"))
            output = FlushedOutput()
            with self.subTest(status=status), contextlib.redirect_stdout(output):
                count, failures = tool.ensure_test_plans(api, {"fixture-%03d" % i: i for i in range(1, 6)}, {})
            self.assertEqual((count, api.calls, len(failures)), (0, 1, 5))
            self.assertIn(str(status), output.getvalue() + str(failures))
            self.assertNotIn("private-token-and-domain", output.getvalue() + str(failures))

    def test_off_target_still_disables_dangerous_plan_and_reads_back(self):
        api = PlansApi(count=1, dangerous=True)
        with contextlib.redirect_stdout(FlushedOutput()):
            result = tool.ensure_test_plans(api, {"fixture-001": 1}, {})
        self.assertEqual(result, (1, []))
        self.assertFalse(api.plans[1]["enabled"])
        self.assertFalse(api.plans[1]["auto_recover"])
        self.assertEqual(api.plans[1]["cron_expression"].split()[1:], ["3", "*", "*", "0"])
        self.assertGreaterEqual(api.calls, 2)
        self.assertGreaterEqual(api.plan_reads, 3)

    def test_health_three_failures_stop_and_do_not_claim_verified_success(self):
        class Api:
            def __init__(self):
                self.writes = 0
            def update_account(self, aid, body):
                self.writes += 1
                raise tool.ApiError(0, "private-token", "<redacted>")
        api = Api()
        records = [{"name": "fixture-%03d" % i, "new_priority": 3} for i in range(1, 8)]
        have = {r["name"]: {"id": i, "priority": 8} for i, r in enumerate(records, 1)}
        output = FlushedOutput()
        with contextlib.redirect_stdout(output):
            ok, same, failures = tool.sync_health_ranking(api, records, have)
        self.assertEqual((ok, same, len(failures)), (0, 0, 7))
        self.assertEqual(api.writes, 3)
        self.assertIn("[1/7]", output.getvalue())
        self.assertNotIn("private-token", output.getvalue() + str(failures))

    def test_failed_health_calibration_forces_nonzero_summary(self):
        stat = {"success": ["fixture"], "failed": [], "skipped": [], "total_planned": 1,
                "stage2_rerank": {"ran": True, "updated": 0, "failed": 1}}
        with contextlib.redirect_stdout(FlushedOutput()):
            self.assertEqual(tool.print_import_summary(stat), 1)

    def test_drip_headers_and_body_obey_total_deadline(self):
        for mode in ("headers", "body"):
            with self.subTest(mode=mode), fixture(mode) as server:
                base = "http://127.0.0.1:%d" % server.server_address[1]
                cfg = {"sub2api_base_url": base, "sub2api_admin_key": "synthetic", "http_timeout": .35, "http_retries": 1}
                api = tool.Sub2Api(cfg)
                api.timeout = .1
                start_calibration(api, cfg)
                started = time.monotonic()
                failed = False
                with patch.object(tool, "load_settings", return_value=cfg), contextlib.redirect_stdout(FlushedOutput()):
                    try:
                        api.list_test_plans(7)
                    except tool.ApiError:
                        failed = True
                elapsed = time.monotonic() - started
                self.assertTrue(failed, "drip response completed beyond the operation deadline")
                self.assertLess(elapsed, .85)

    def test_tls_stall_and_401_are_bounded_and_sanitized(self):
        for mode in ("tls", "unauthorized"):
            with self.subTest(mode=mode), fixture(mode) as server:
                base = ("https" if mode == "tls" else "http") + "://127.0.0.1:%d" % server.server_address[1]
                # The 401 assertion needs the actual HTTP response, not a race
                # against Windows process startup. TLS still proves the short
                # operation deadline; drip header/body tests keep their bound.
                budget = .35 if mode == "tls" else 2
                cfg = {"sub2api_base_url": base, "sub2api_admin_key": "synthetic", "http_timeout": budget, "http_retries": 1}
                api = tool.Sub2Api(cfg)
                api.timeout = .1
                start_calibration(api, cfg)
                with patch.object(tool, "load_settings", return_value=cfg), contextlib.redirect_stdout(FlushedOutput()):
                    with self.assertRaises(tool.ApiError) as caught:
                        api.list_test_plans(7)
                if mode == "unauthorized":
                    self.assertEqual(caught.exception.status, 401)
                    self.assertNotIn("private-token-and-domain", str(caught.exception))

    def test_admin_child_refuses_any_non_admin_path_before_network(self):
        with fixture("ok") as server:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": base, "sub2api_admin_key": "synthetic"}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            with contextlib.redirect_stdout(FlushedOutput()), self.assertRaises(tool.ApiError):
                api._call("GET", "/v1/models")
            self.assertEqual(server.received, [])

    def test_stopped_phase_preserves_http_status_and_sends_nothing(self):
        with fixture("ok") as server:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": base, "sub2api_admin_key": "synthetic"}
            api = tool.Sub2Api(cfg)
            calibration = start_calibration(api, cfg)
            calibration.stop("fixture stop", 401)
            with self.assertRaises(tool.ApiError) as caught:
                api.list_test_plans(7)
            self.assertEqual(caught.exception.status, 401)
            self.assertEqual(server.received, [])

    def test_deadline_kills_and_joins_child_with_no_credentials_in_argv(self):
        import admin_io
        with fixture("body") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            real_popen = admin_io.subprocess.Popen
            children, arguments = [], []
            def create(args, **kwargs):
                arguments.append(args)
                child = real_popen(args, **kwargs)
                children.append(child)
                return child
            with patch.object(admin_io.subprocess, "Popen", side_effect=create):
                with self.assertRaises(admin_io.Failure):
                    admin_io.request("GET", origin + "/api/v1/admin/accounts/7",
                        {"x-api-key": "synthetic-secret"}, None, .3,
                        time.monotonic() + .3, origin)
            self.assertTrue(children)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertNotIn("synthetic-secret", str(arguments))
            self.assertNotIn(origin, str(arguments))

    def test_keyboard_interrupt_kills_and_joins_before_reraising(self):
        import admin_io
        class Child:
            def __init__(self):
                self.killed, self.communications = False, 0
            def communicate(self, *args, **kwargs):
                self.communications += 1
                if self.communications == 1:
                    raise KeyboardInterrupt()
                return b"", b""
            def poll(self):
                return None
            def kill(self):
                self.killed = True
        child = Child()
        with patch.object(admin_io.subprocess, "Popen", return_value=child):
            with self.assertRaises(KeyboardInterrupt):
                admin_io.request("GET", "http://127.0.0.1/api/v1/admin/accounts/7",
                    {"x-api-key": "synthetic"}, None, 1, time.monotonic() + 1,
                    "http://127.0.0.1")
        self.assertTrue(child.killed)
        self.assertEqual(child.communications, 2)

    def test_guard_rechecked_before_each_actual_retry_and_same_idem(self):
        with fixture("retry") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic",
                   "http_timeout": 4, "http_retries": 2}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            checks = []
            with patch.object(tool, "load_settings", return_value=cfg), \
                 tool._guarded_admin_write(lambda: checks.append(True)), \
                 contextlib.redirect_stdout(FlushedOutput()):
                api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1})
            self.assertEqual(len(server.received), 2)
            self.assertGreaterEqual(len(checks), 3)
            identities = [headers.get("idempotency-key") for _, headers in server.received]
            self.assertTrue(identities[0])
            self.assertEqual(identities[0], identities[1])

    def test_revocation_before_retry_prevents_second_actual_put(self):
        with fixture("retry") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic",
                   "http_timeout": 4, "http_retries": 2}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            checks = []
            def guard():
                checks.append(True)
                if server.received:
                    raise tool.ProbeAuthorityRevoked("fixture revoked")
            with patch.object(tool, "load_settings", return_value=cfg), \
                 tool._guarded_admin_write(guard), contextlib.redirect_stdout(FlushedOutput()):
                with self.assertRaises(tool.ProbeAuthorityRevoked):
                    api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1})
            self.assertEqual(len(server.received), 1)

    def test_long_429_cooldown_is_not_shortened_to_budget(self):
        with fixture("limited") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            # Obtain the actual 429 even when interpreter startup is slow.
            # Retry-After is still 60s, well beyond this request budget; the
            # no-sleep and exactly-one-request assertions below stay intact.
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic",
                   "http_timeout": 2, "http_retries": 2}
            api = tool.Sub2Api(cfg)
            calibration = start_calibration(api, cfg)
            # A wire attempt is bounded by2s; the60s cooldown is compared
            # against the absolute phase budget, which has only30s left here.
            calibration.phase_deadline = time.monotonic() + 30
            with patch.object(tool.time, "sleep") as sleep, \
                 patch.object(tool, "load_settings", return_value=cfg), \
                 contextlib.redirect_stdout(FlushedOutput()):
                with self.assertRaises(tool.ApiError) as caught:
                    api.list_test_plans(7)
            self.assertEqual(caught.exception.status, 429)
            self.assertTrue(calibration.stopped)
            self.assertEqual(calibration.stop_status, 429)
            sleep.assert_not_called()
            self.assertEqual(len(server.received), 1)

    def test_redirect_cannot_forward_admin_key_to_any_other_endpoint(self):
        with fixture("ok") as target, fixture("redirect") as source:
            source.redirect_to = "http://127.0.0.1:%d/v1/models" % target.server_address[1]
            origin = "http://127.0.0.1:%d" % source.server_address[1]
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic", "http_retries": 1}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            with patch.object(tool, "load_settings", return_value=cfg), contextlib.redirect_stdout(FlushedOutput()):
                with self.assertRaises(tool.ApiError) as caught:
                    api.list_test_plans(7)
            self.assertEqual(caught.exception.status, 302)
            self.assertEqual(len(source.received), 1)
            self.assertEqual(target.received, [])

    def test_declared_large_response_is_rejected_without_body_read(self):
        with fixture("large") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic", "http_retries": 1}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            with patch.object(tool, "load_settings", return_value=cfg), contextlib.redirect_stdout(FlushedOutput()):
                with self.assertRaises(tool.ApiError) as caught:
                    api.list_test_plans(7)
            self.assertIn("大小上限", caught.exception.body)
            self.assertEqual(len(server.received), 1)

    def test_nested_guard_reads_share_the_outer_request_deadline(self):
        with fixture("slow") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic",
                   "http_timeout": .9, "http_retries": 1}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            def guard():
                api._call("GET", "/api/v1/admin/accounts/7")
            with patch.object(tool, "load_settings", return_value=cfg), \
                 contextlib.redirect_stdout(FlushedOutput()), tool._guarded_admin_write(guard):
                started = time.monotonic()
                with self.assertRaises(tool.ApiError):
                    api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1})
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.12)
            self.assertFalse(any(method == "PUT" for method, _ in server.received))

    def _run_lifecycle_import(self, api, action, fail_record=False):
        try:
            from tests.test_partial_import import PartialImportTests
        except ImportError:
            from test_partial_import import PartialImportTests
        import tempfile
        case = PartialImportTests()
        isolated = tempfile.TemporaryDirectory(prefix="admin-calibration-fixture-")
        try:
            with patch.dict(tool.DEFAULTS, {"request_relay_enabled": False,
                    "request_relay_state_dir": str(pathlib.Path(isolated.name) / "relay")}), \
                 patch.object(tool, "OUT_DIR", str(pathlib.Path(isolated.name) / "build")):
                case.setUp()
            selected = tool.select_import_hosts(case.plan, ["alpha.example"])
            records = case.plan["accounts_raw"]
            rows = [dict(tool.to_account(r, case.plan["config"], {"Claude": 1}, {}, {}),
                         id=i + 1, status="active", schedulable=True) for i, r in enumerate(records)]
            have = {r["name"]: rows[i] for i, r in enumerate(records)}
            values = {
                "load_settings": case.settings,
                "OUT_DIR": str(pathlib.Path(case.tmp.name) / "out"),
                "ensure_groups": ({"Claude": 1}, []),
                "ensure_proxies": {},
                "existing_account_names": (set(have), None),
                "existing_accounts_by_name": (have, None),
                "sync_existing_accounts": (1, 0, []),
                "_fetch_all_accounts": (rows, None),
                "probe_import_recovery": ({}, []),
                "reclaim_schedulable": (0, 0, None),
                "revive_proven_inactive": (0, 0, None),
                "find_stale_accounts": ([], None),
                "prune_stale_accounts": ([], []),
            }
            with contextlib.ExitStack() as stack:
                for name, value in values.items():
                    if name == "OUT_DIR":
                        stack.enter_context(patch.object(tool, name, value))
                    else:
                        stack.enter_context(patch.object(tool, name, return_value=value))
                stack.enter_context(patch.object(tool, "ensure_test_plans", side_effect=action))
                stack.enter_context(patch.object(tool, "apply_fallback_chain",
                    side_effect=AssertionError("unexpected global mutation")))
                stack.enter_context(contextlib.redirect_stdout(FlushedOutput()))
                if fail_record:
                    stack.enter_context(patch.object(tool.io, "open", side_effect=OSError("fixture record failure")))
                return tool.do_import(api, selected)
        finally:
            case.doCleanups()
            isolated.cleanup()

    def test_import_cleanup_on_interrupt_exception_and_same_api_retry(self):
        for exception in (KeyboardInterrupt, RuntimeError):
            api = PlansApi(count=1)
            def interrupted(api, *args, **kwargs):
                api._admin_calibration.stop("fixture stopped")
                raise exception("fixture")
            with self.subTest(exception=exception):
                with self.assertRaises(exception):
                    self._run_lifecycle_import(api, interrupted)
                self.assertFalse(hasattr(api, "_admin_calibration"))
                result = self._run_lifecycle_import(api, lambda *args, **kwargs: (0, []))
                self.assertFalse(result.get("calibration_failed"))
                self.assertFalse(hasattr(api, "_admin_calibration"))

    def test_import_cleanup_after_return_and_record_write_failure(self):
        for fail_record in (False, True):
            api = PlansApi(count=1)
            with self.subTest(fail_record=fail_record):
                result = self._run_lifecycle_import(api, lambda *args, **kwargs: (0, []), fail_record)
                self.assertFalse(result.get("calibration_failed"))
                self.assertFalse(hasattr(api, "_admin_calibration"))

    def test_import_cleanup_preserves_preexisting_outer_calibration(self):
        import admin_io
        api = PlansApi(count=1)
        outer = admin_io.Calibration()
        api._admin_calibration = outer
        self._run_lifecycle_import(api, lambda *args, **kwargs: (0, []))
        self.assertIs(api._admin_calibration, outer)

    def test_offline_snapshot_orders_denied_then_revoked_before_allowed(self):
        import hashlib
        import inspect
        import probe_policy
        class Api(PlansApi):
            def __init__(self):
                super().__init__(failure=tool.ApiError(0, "fixture", "<redacted>"))
                self.ids = []
            def _call(self, method, path, body=None, **kwargs):
                self.ids.append(int(path.rsplit("/", 1)[-1]))
                return super()._call(method, path, body, **kwargs)
        api = Api()
        snapshots = [account(i) for i in range(1, 148)]
        for row in snapshots:
            row["credentials"]["base_url"] = "https://allowed.fixture.invalid"
        snapshots[145]["credentials"]["base_url"] = "https://denied.fixture.invalid"
        snapshots[146]["credentials"]["base_url"] = "https://revoked.fixture.invalid"
        snapshots[1]["credentials"]["base_url"] = ""
        cfg = {"model_probe_enabled": True, "online_probe_hosts": {
            "allowed.fixture.invalid": True, "denied.fixture.invalid": True,
            "revoked.fixture.invalid": False}}
        kwargs = {"account_snapshot": snapshots} if "account_snapshot" in inspect.signature(tool.ensure_test_plans).parameters else {}
        names = {"a-allowed-%03d" % i: i for i in range(1, 146)}
        names.update({"z-denied": 146, "z-revoked": 147})
        digest = hashlib.sha256(b"denied.fixture.invalid").hexdigest()
        with patch.object(probe_policy, "DENIED_HOST_DIGESTS", {digest}), \
             contextlib.redirect_stdout(FlushedOutput()):
            count, failures = tool.ensure_test_plans(api, names, cfg, **kwargs)
        self.assertEqual((count, len(failures)), (0, 147))
        self.assertEqual(api.ids, [146, 147, 2])

    def test_stop_reason_keeps_its_origin_phase(self):
        import admin_io
        calibration = admin_io.Calibration()
        calibration.phase("first", 147)
        calibration.stop("fixture failure", 401)
        calibration.phase("second", 9)
        self.assertEqual(calibration.stop_phase, "first")
        self.assertEqual(calibration.stop_status, 401)

    def test_windows_child_protocol_preserves_unicode_request_and_response(self):
        import json
        with fixture("unicode") as server:
            origin = "http://127.0.0.1:%d" % server.server_address[1]
            cfg = {"sub2api_base_url": origin, "sub2api_admin_key": "synthetic", "http_retries": 1}
            api = tool.Sub2Api(cfg)
            start_calibration(api, cfg)
            expected = {"notes": "中文配置 😀"}
            with patch.object(tool, "load_settings", return_value=cfg), contextlib.redirect_stdout(FlushedOutput()):
                result = api._call("PUT", "/api/v1/admin/accounts/7", expected)
            self.assertEqual(result, expected)
            self.assertEqual(json.loads(server.request_bodies[0].decode("utf-8")), expected)


if __name__ == "__main__":
    unittest.main()
