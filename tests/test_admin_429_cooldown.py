"""Phase-budget cooldown proof through actual Sub2Api request/guard transport."""
import contextlib
import io
import pathlib
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import admin_io
import tool


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.sleeps = []
        self.wires = []
        self.cfg = {"sub2api_base_url": "http://127.0.0.1:9",
                    "sub2api_admin_key": "synthetic", "http_timeout": 30, "http_retries": 1}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(tool.time, "monotonic", side_effect=lambda: self.now))
        self.stack.enter_context(patch.object(tool.time, "sleep", side_effect=self.sleep))
        self.stack.enter_context(patch.object(tool, "load_settings", return_value=self.cfg))
        self.api = tool.Sub2Api(self.cfg)
        self.cal = admin_io.Calibration(self.api.base, 30, 600, 900)
        self.cal.phase("fixture", 1)
        self.api._admin_calibration = self.cal

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def transport(self, handler):
        def send(method, url, headers, body, timeout, deadline, origin, progress=None):
            self.wires.append({"method": method, "url": url, "start": self.now,
                               "deadline": deadline, "idem": headers.get("Idempotency-Key")})
            return handler(method, url)
        self.stack.enter_context(patch.object(admin_io, "request", side_effect=send))

    def test_retry_after60_uses_phase_budget_and_fresh_attempt_deadline(self):
        count = [0]
        def reply(method, url):
            count[0] += 1
            if count[0] == 1:
                raise admin_io.Failure("http", 429, 60)
            return 200, "{}"
        self.transport(reply)
        guards = []
        with tool._guarded_admin_write(lambda: guards.append(self.now)):
            self.assertEqual(self.api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1}), {})
        self.assertEqual(sum(self.sleeps), 60)
        self.assertTrue(all(wait <= 5 for wait in self.sleeps))
        self.assertEqual(len(self.wires), 2)
        self.assertEqual([w["deadline"] - w["start"] for w in self.wires], [30, 30])
        self.assertEqual(self.wires[1]["start"] - self.wires[0]["start"], 60)
        self.assertTrue(self.wires[0]["idem"])
        self.assertEqual(self.wires[0]["idem"], self.wires[1]["idem"])
        self.assertIn(1060, guards)
        self.assertEqual(self.cal.deadline, 1900)
        self.assertEqual(self.cal.phase_deadline, 1600)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())

    def test_nested_guard429_bubbles_then_waits_once_and_rechecks(self):
        limited = [False]
        guards = []
        def reply(method, url):
            if method == "GET" and not limited[0]:
                limited[0] = True
                raise admin_io.Failure("http", 429, 60)
            return 200, "{}"
        self.transport(reply)
        def guard():
            guards.append(self.now)
            self.api._call("GET", "/api/v1/admin/accounts/7")
        token = tool._ADMIN_WRITE_GUARD.set(guard)
        try:
            self.assertEqual(self.api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1}), {})
        finally:
            tool._ADMIN_WRITE_GUARD.reset(token)
        self.assertEqual(sum(self.sleeps), 60)
        self.assertEqual(len([w for w in self.wires if w["method"] == "PUT"]), 1)
        self.assertEqual(self.wires[0]["deadline"], 1030)
        self.assertTrue(all(w["deadline"] <= w["start"] + 30 for w in self.wires))
        self.assertEqual(self.cal.consecutive_failures, 0)
        self.assertIn(1060, guards)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())

    def test_real_loopback_guard429_cools60_then_sends_one_put(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def reply(self, status):
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "60")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
            def do_GET(self):
                seen.append(("GET", self.headers.get("Idempotency-Key")))
                self.reply(429 if len(seen) == 1 else 200)
            def do_PUT(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                seen.append(("PUT", self.headers.get("Idempotency-Key")))
                self.reply(200)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.api.base = "http://127.0.0.1:%d" % server.server_port
        self.cfg["sub2api_base_url"] = self.api.base
        guards = []
        def guard():
            guards.append(self.now)
            self.api._call("GET", "/api/v1/admin/accounts/7")
        token = tool._ADMIN_WRITE_GUARD.set(guard)
        try:
            self.assertEqual(self.api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1}), {})
        finally:
            tool._ADMIN_WRITE_GUARD.reset(token)
        self.assertEqual(sum(self.sleeps), 60)
        self.assertEqual(seen[0][0], "GET")
        self.assertEqual(len([method for method, _ in seen if method == "PUT"]), 1)
        self.assertTrue(seen[-1][1])
        self.assertIn(1060, guards)
        self.assertEqual(self.cal.consecutive_failures, 0)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())

    def test_insufficient_phase_budget_stops_without_shortening_or_next_request(self):
        self.cal.phase_deadline = self.now + 50
        self.transport(lambda method, url: (_ for _ in ()).throw(admin_io.Failure("http", 429, 60)))
        with self.assertRaises(tool.ApiError) as caught:
            self.api._call("GET", "/api/v1/admin/accounts/7")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(self.cal.stopped)
        self.assertEqual(self.cal.stop_status, 429)
        self.cal.phase("next phase", 2)
        with self.assertRaises(tool.ApiError):
            self.api._call("GET", "/api/v1/admin/accounts/8")
        self.assertEqual(len(self.wires), 1)

    def test_three429_attempts_stop_without_fourth_or_network_failure_label(self):
        self.transport(lambda method, url: (_ for _ in ()).throw(admin_io.Failure("http", 429, 60)))
        with self.assertRaises(tool.ApiError) as caught:
            self.api._call("GET", "/api/v1/admin/accounts/7")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(len(self.wires), 3)
        self.assertEqual(sum(self.sleeps), 120)
        self.assertTrue(self.cal.stopped)
        self.assertEqual(self.cal.consecutive_failures, 0)
        self.assertNotIn("网络", self.cal.stop_reason)

    def test_total_budget_and_expiry_during_pause_cannot_be_renewed(self):
        self.cal.deadline = self.now + 50
        self.transport(lambda method, url: (_ for _ in ()).throw(admin_io.Failure("http", 429, 60)))
        with self.assertRaises(tool.ApiError) as caught:
            self.api._call("GET", "/api/v1/admin/accounts/7")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(self.sleeps, [])
        self.assertTrue(self.cal.stopped)
        self.assertEqual(len(self.wires), 1)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())

    def test_phase_expires_during_pause_without_second_request(self):
        self.cal.phase_deadline = self.now + 70
        self.transport(lambda method, url: (_ for _ in ()).throw(admin_io.Failure("http", 429, 60)))
        def overslept(seconds):
            self.sleeps.append(seconds)
            self.now += 80
        with patch.object(tool.time, "sleep", side_effect=overslept):
            with self.assertRaises(tool.ApiError):
                self.api._call("GET", "/api/v1/admin/accounts/7")
        self.assertEqual(len(self.wires), 1)
        self.assertTrue(self.cal.stopped)
        self.assertEqual(self.cal.deadline, 1900)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())

    def test_nested_guard_uses_remaining_attempt_then_fresh30_after_pause(self):
        limited = [False]
        def reply(method, url):
            if method == "GET" and not limited[0]:
                limited[0] = True
                self.now += 25
                raise admin_io.Failure("http", 429, 60)
            return 200, "{}"
        self.transport(reply)
        token = tool._ADMIN_WRITE_GUARD.set(lambda: self.api._call("GET", "/api/v1/admin/accounts/7"))
        try:
            self.assertEqual(self.api._call("PUT", "/api/v1/admin/accounts/7", {"priority": 1}), {})
        finally:
            tool._ADMIN_WRITE_GUARD.reset(token)
        self.assertEqual(sum(self.sleeps), 60)
        self.assertEqual(self.wires[0]["deadline"], 1030)
        self.assertEqual(self.wires[-1]["start"], 1085)
        self.assertEqual(self.wires[-1]["deadline"], 1115)
        self.assertEqual(self.cal.phase_deadline, 1600)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())

    def test_authentication_and_three_network_errors_keep_their_stop_policy(self):
        for status in (401, 403):
            cal = admin_io.Calibration()
            cal.record_error(tool.ApiError(status, "synthetic", "<redacted>"))
            self.assertTrue(cal.stopped)
            self.assertEqual(cal.stop_status, status)
        cal = admin_io.Calibration()
        for index in range(1, 4):
            cal.item(index, 3)
            cal.record_error(tool.ApiError(0, "synthetic", "<redacted>"))
        self.assertTrue(cal.stopped)
        self.assertEqual(cal.consecutive_failures, 3)
        self.assertIn("网络", cal.stop_reason)

    def test_cancellation_during_cooldown_does_not_send_second_request(self):
        self.transport(lambda method, url: (_ for _ in ()).throw(admin_io.Failure("http", 429, 60)))
        def cancel(seconds):
            raise KeyboardInterrupt()
        with patch.object(tool.time, "sleep", side_effect=cancel):
            with self.assertRaises(KeyboardInterrupt):
                self.api._call("GET", "/api/v1/admin/accounts/7")
        self.assertEqual(len(self.wires), 1)
        self.assertIsNone(admin_io.REQUEST_DEADLINE.get())


if __name__ == "__main__":
    unittest.main()
