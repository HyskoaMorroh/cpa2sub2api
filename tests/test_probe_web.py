"""Offline Web consent tests; no production admin or upstream is contacted."""
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tool
import probe_web
import request_relay


class ProbeWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "settings.json"
        self.config = Path(self.tmp.name) / "config.yaml"
        self.config.write_text("claude-api-key:\n- api-key: synthetic-secret-never-return\n"
                               "  base-url: https://alpha.example/v1\n"
                               "  models: [{name: claude-opus-9}]\n"
                               "codex-api-key:\n- api-key: another-synthetic-secret\n"
                               "  base-url: https://beta.example/v1\n"
                               "  models: [{name: gpt-6}]\n", encoding="utf-8")
        self.path.write_text(json.dumps({"unrelated": "preserve", "sub2api_admin_key": "stored-secret"}), encoding="utf-8")
        self.stack = []
        for p in [patch.object(tool, "SETTINGS_PATH", str(self.path)),
                  patch.object(tool, "CONFIG_YAML", str(self.config)),
                  patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "", "MODEL_PROBE_HARD_DISABLED": "", "ONLINE_PROBE_HOSTS": "",
                                          "SUB2API_ADMIN_KEY": "environment-secret"}),
                  patch.object(tool, "_http", side_effect=AssertionError("network forbidden"))]:
            self.stack.append(p)
            p.start()
            self.addCleanup(p.stop)
        # Observe the owning helper while exercising its real lock/atomic write.
        self.writes = []
        real_update = tool.update_saved_settings
        def update(mutator):
            def observed(saved):
                result = mutator(saved)
                self.writes.append(copy.deepcopy(result))
                return result
            return real_update(observed)
        self.write_patch = patch.object(tool, "update_saved_settings", side_effect=update)
        self.write_patch.start()
        self.addCleanup(self.write_patch.stop)
        self.service = probe_web.ProbeService()

    def test_listing_is_offline_and_secret_free(self):
        state = self.service.snapshot()
        self.assertEqual([r["host"] for r in state["sites"]], ["alpha.example", "beta.example"])
        self.assertTrue(all(not r["desired"] and not r["effective"] for r in state["sites"]))
        rendered = json.dumps(state)
        for secret in ("synthetic-secret", "stored-secret", "environment-secret", "api_key", "base_url"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(state["sites"][0]["categories"], ["Claude"])

    def test_enable_saves_only_requested_keys_and_never_reconciles(self):
        with patch.object(self.service, "_reconcile") as reconcile:
            response = self.service.update({"hosts": ["alpha.example"], "enabled": True})
        reconcile.assert_not_called()
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["unrelated"], "preserve")
        self.assertEqual(saved["sub2api_admin_key"], "stored-secret")
        self.assertTrue(saved["online_probe_hosts"]["alpha.example"])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(response["applyStatus"], "saved")
        self.assertNotIn("environment-secret", self.path.read_text(encoding="utf-8"))

    def test_environment_hard_gate_and_allowlist_are_visible(self):
        self.service.update({"master": True})
        self.service.update({"hosts": ["alpha.example"], "enabled": True})
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "false", "MODEL_PROBE_HARD_DISABLED": "true",
                                     "ONLINE_PROBE_HOSTS": "beta.example"}):
            state = self.service.snapshot()
        self.assertTrue(state["master"]["desired"])
        self.assertFalse(state["master"]["effective"])
        self.assertTrue(state["master"]["environmentOverride"])
        self.assertTrue(state["master"]["hardDisabled"])
        self.assertIn("MODEL_PROBE_HARD_DISABLED", state["master"]["hardDisableReason"])
        alpha = state["sites"][0]
        self.assertTrue(alpha["desired"])
        self.assertFalse(alpha["effective"])
        self.assertTrue(alpha["environmentOverride"])

    def test_disable_reports_saved_not_applied_when_admin_fails(self):
        self.service.update({"hosts": ["alpha.example"], "enabled": True})
        with patch.object(self.service, "_reconcile", return_value={"ok": False, "count": 0, "failed": 1}):
            result = self.service.update({"hosts": ["alpha.example"], "enabled": False})
        self.assertTrue(result["saved"])
        self.assertEqual(result["applyStatus"], "saved_not_applied")
        self.assertFalse(json.loads(self.path.read_text(encoding="utf-8"))["online_probe_hosts"]["alpha.example"])

    def test_disable_is_attempted_even_when_setting_was_already_off(self):
        with patch.object(self.service, "_reconcile", return_value={"ok": True, "count": 2, "failed": 0}) as rec:
            result = self.service.update({"hosts": ["alpha.example"], "enabled": False})
        self.assertEqual(rec.call_args.kwargs["hosts"], {"alpha.example"})
        self.assertEqual(result["applyStatus"], "applied")

    def test_master_disable_includes_stale_managed_accounts(self):
        with patch.object(self.service, "_reconcile", return_value={"ok": True, "count": 2, "failed": 0}) as rec:
            result = self.service.update({"master": False})
        self.assertIsNone(rec.call_args.kwargs["hosts"])
        self.assertEqual(result["applyStatus"], "applied")

    def test_explicit_disable_revokes_even_environment_opt_in(self):
        with patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "true", "ONLINE_PROBE_HOSTS": "alpha.example"}), \
             patch.object(self.service, "_reconcile", return_value={"ok": True, "count": 0, "failed": 0}):
            result = self.service.update({"hosts": ["alpha.example"], "enabled": False})
        self.assertEqual(result["applyStatus"], "applied")
        self.assertFalse(result["state"]["sites"][0]["effective"])

    def test_invalid_or_unknown_hosts_cannot_expand_authority(self):
        for payload in ({"hosts": ["evil.example"], "enabled": True},
                        {"hosts": ["https://alpha.example"], "enabled": True},
                        {"hosts": ["alpha.example"], "enabled": "true"},
                        {"hosts": [], "enabled": True},
                        {"master": True, "sub2api_admin_key": "injected"}):
            with self.subTest(payload=payload), self.assertRaises(probe_web.UiError):
                self.service.update(payload)
        self.assertEqual(self.writes, [])

    def test_permanent_deny_cannot_be_enabled_by_batch(self):
        original = tool.probe_policy.permanently_denied
        with patch.object(tool.probe_policy, "permanently_denied", side_effect=lambda h: h == "beta.example" or original(h)):
            state = self.service.snapshot()
            self.assertTrue(state["sites"][1]["locked"])
            with self.assertRaises(probe_web.UiError):
                self.service.update({"hosts": ["alpha.example", "beta.example"], "enabled": True})
        self.assertEqual(self.writes, [])

    def test_corrupt_settings_refuses_to_overwrite(self):
        self.path.write_text("{broken-secret-body", encoding="utf-8")
        with self.assertRaises(probe_web.UiError) as error:
            self.service.snapshot()
        self.assertNotIn("broken-secret", str(error.exception))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken-secret-body")

    def test_reconciliation_only_targets_revoked_hosts_and_redacts_failures(self):
        rows = [{"id": 1, "name": "one", "notes": tool.NOTE_SENTINEL,
                 "credentials": {"base_url": "https://alpha.example", "api_key": "never-return"}},
                {"id": 2, "name": "two", "notes": tool.NOTE_SENTINEL,
                 "credentials": {"base_url": "https://beta.example"}}]
        with patch.object(tool, "Sub2Api"), patch.object(tool, "_fetch_all_accounts", return_value=(rows, None)), \
             patch.object(tool, "ensure_test_plans", return_value=(1, [])) as ensure:
            result = self.service._reconcile(hosts={"alpha.example"})
        self.assertTrue(result["ok"])
        self.assertEqual(set(ensure.call_args.args[1].values()), {1})
        self.assertNotIn("never-return", json.dumps(result))

    def relay_account(self):
        document = tool._yaml().safe_load(self.config.read_text(encoding="utf-8"))
        records, _ = tool.collect(document)
        record = next(row for row in records if "alpha.example" in row["base_url"])
        bundle = request_relay.compile_bundle([record], document, "web-fixture")
        account = {"id": 7, "name": "relay-source", "notes": tool.NOTE_SENTINEL,
                   "credentials": {"base_url": record["base_url"], "api_key": record["api_key"]}, "extra": {}}
        return request_relay.adapt_account(account, record, bundle, "http://request-relay:8380")

    def test_relay_plan_filter_uses_original_source_not_internal_relay_host(self):
        account = self.relay_account()
        self.assertIn("request-relay", account["credentials"]["base_url"])
        with patch.object(tool, "Sub2Api"), patch.object(tool, "_fetch_all_accounts", return_value=([account], None)), \
             patch.object(tool, "ensure_test_plans", return_value=(1, [])) as ensure:
            result = self.service._reconcile(hosts={"alpha.example"})
        self.assertTrue(result["ok"])
        self.assertEqual(set(ensure.call_args.args[1].values()), {7})

    def test_malformed_relay_provenance_cannot_fall_back_to_relay_host(self):
        account = self.relay_account()
        account["extra"][request_relay.RELAY_METADATA]["base_url"] = "https://different.example"
        with patch.object(tool, "Sub2Api"), patch.object(tool, "_fetch_all_accounts", return_value=([account], None)), \
             patch.object(tool, "ensure_test_plans") as ensure:
            result = self.service._reconcile(hosts={"request-relay"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], 1)
        ensure.assert_not_called()
        self.assertNotIn("different.example", json.dumps(result))


class ProbeHttpTests(unittest.TestCase):
    TOKEN = "Synthetic-token-for-offline-tests-0123456789"

    def setUp(self):
        helper = patch.object(tool, "update_saved_settings", create=True)
        helper.start()
        self.addCleanup(helper.stop)
        self.service = unittest.mock.Mock()
        self.service.snapshot.return_value = {"master": {}, "sites": []}
        self.service.update.return_value = {"saved": True, "applyStatus": "saved", "state": {"sites": []}}
        self.server = probe_web.create_server("127.0.0.1", 0, self.TOKEN, service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.origin = "http://127.0.0.1:%d" % self.port

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        merged = {"Origin": self.origin, "Content-Type": "application/json"}
        merged.update(headers or {})
        connection.request(method, path, None if body is None else json.dumps(body), merged)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def login(self):
        status, headers, body = self.request("POST", "/api/session", {"token": self.TOKEN})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = json.loads(body)["csrf"]
        return headers

    def test_auth_cookie_csrf_and_security_headers(self):
        headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        status, _, body = self.request("GET", "/api/state", headers={"Cookie": self.cookie})
        self.assertEqual(status, 200)
        self.assertNotIn(self.TOKEN.encode(), body)

    def test_anonymous_state_and_mutations_are_rejected(self):
        for method, path in (("GET", "/api/state"), ("PATCH", "/api/settings"), ("POST", "/api/reconciliation")):
            self.assertEqual(self.request(method, path, {})[0], 401)
        self.service.update.assert_not_called()

    def test_exact_origin_host_and_csrf_protect_every_mutation(self):
        self.login()
        base = {"Cookie": self.cookie, "X-CSRF-Token": self.csrf}
        for headers in ({**base, "Origin": "https://evil.example"},
                        {**base, "Origin": "null"}, {**base, "Host": "evil.example"},
                        {"Cookie": self.cookie}, {**base, "Content-Type": "text/plain"}):
            with self.subTest(headers=list(headers)):
                status = self.request("PATCH", "/api/settings", {"master": True}, headers)[0]
                self.assertIn(status, (403, 415))
        self.service.update.assert_not_called()
        self.assertEqual(self.request("PATCH", "/api/settings", {"master": True}, base)[0], 200)

    def test_token_in_query_and_static_traversal_are_rejected(self):
        self.assertEqual(self.request("GET", "/?token=" + self.TOKEN)[0], 400)
        self.assertEqual(self.request("GET", "/..%2fsettings.json")[0], 404)

    def test_login_rate_limit_and_wrong_token(self):
        statuses = [self.request("POST", "/api/session", {"token": "wrong"})[0] for _ in range(7)]
        self.assertEqual(statuses[0], 401)
        self.assertEqual(statuses[-1], 429)

    def test_rejected_request_consumes_delayed_bounded_body_before_closing(self):
        # http.client sends headers and body separately. Closing after rejecting
        # the headers can reset that legitimate upload before its 403 is read.
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            body = b'{"master":true}'
            connection.sendall(("PATCH /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                                "Origin: https://evil.example\r\nContent-Type: application/json\r\n"
                                "Content-Length: %d\r\n\r\n" % (self.port, len(body))).encode("ascii"))
            connection.settimeout(0.05)
            with self.assertRaises(socket.timeout):
                connection.recv(1)
            connection.settimeout(2)
            connection.sendall(body)
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            self.assertEqual(response.getheader("Connection"), "close")
            self.assertEqual(json.loads(response.read())["error"]["code"], "ORIGIN_DENIED")
        self.service.update.assert_not_called()

    def test_rejected_missing_or_oversized_body_does_not_wait_for_upload(self):
        for length in (1024, probe_web.MAX_BODY + 1):
            with self.subTest(length=length), socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
                connection.sendall(("PATCH /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                                    "Origin: https://evil.example\r\nContent-Type: application/json\r\n"
                                    "Content-Length: %d\r\n\r\n" % (self.port, length)).encode("ascii"))
                connection.settimeout(1.5)
                response = http.client.HTTPResponse(connection)
                response.begin()
                self.assertEqual(response.status, 403)
                self.assertEqual(response.getheader("Connection"), "close")
                response.read()
        self.service.update.assert_not_called()

    def test_rejected_extreme_content_length_is_not_parsed_or_drained(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as connection:
            connection.sendall(("PATCH /api/settings HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                                "Origin: https://evil.example\r\nContent-Type: application/json\r\n"
                                "Content-Length: %s\r\n\r\n" % (self.port, "9" * 5000)).encode("ascii"))
            response = http.client.HTTPResponse(connection)
            response.begin()
            self.assertEqual(response.status, 403)
            self.assertEqual(json.loads(response.read())["error"]["code"], "ORIGIN_DENIED")
        self.service.update.assert_not_called()

    def test_expired_session_and_logout_revoke_access(self):
        self.login()
        self.assertEqual(self.request("DELETE", "/api/session", headers={"Cookie": self.cookie, "X-CSRF-Token": self.csrf})[0], 200)
        self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": self.cookie})[0], 401)
        self.login()
        with patch.object(probe_web.time, "monotonic", return_value=10 ** 20):
            self.assertEqual(self.request("GET", "/api/state", headers={"Cookie": self.cookie})[0], 401)

    def test_weak_startup_token_is_refused(self):
        for token in ("", "short", "a" * 64):
            with self.assertRaises(ValueError):
                probe_web.create_server("127.0.0.1", 0, token, service=self.service)

    def test_api_errors_never_return_private_exception_details(self):
        self.login()
        self.service.snapshot.side_effect = RuntimeError("private-admin-key-and-config")
        status, headers, body = self.request("GET", "/api/state", headers={"Cookie": self.cookie})
        self.assertEqual(status, 503)
        self.assertNotIn(b"private-admin-key", body)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_static_assets_are_local_and_cannot_return_configuration(self):
        for path in ("/", "/app.js", "/styles.css"):
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
            self.assertNotIn(b"https://fonts", body)
        for path in ("/config.yaml", "/settings.json", "/probe_web.py"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_missing_shared_helper_is_a_startup_error(self):
        with patch.object(tool, "update_saved_settings", None):
            with self.assertRaises(ValueError):
                probe_web.create_server("127.0.0.1", 0, self.TOKEN, service=self.service)


if __name__ == "__main__":
    unittest.main()
