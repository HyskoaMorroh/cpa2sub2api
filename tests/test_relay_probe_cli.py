"""The public diagnostics CLI must bind the active immutable relay runtime."""
import contextlib
import copy
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool
import request_relay


class Admin:
    def __init__(self, row):
        self.row = copy.deepcopy(row)
        self.base, self.key, self.user_agent, self.timeout = "http://admin.invalid", "synthetic", "fixture", 1
        self.writes = []
        self.reads = 0
        self.on_read = None

    def _call(self, method, path):
        assert method == "GET" and path == "/api/v1/admin/accounts/7"
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        return copy.deepcopy(self.row)

    def bulk_update_accounts(self, ids, payload):
        assert ids == [7]
        self.writes.append(copy.deepcopy(payload))
        self.row.update(payload)


class RelayProbeCliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = dict(tool.DEFAULTS, model_probe_enabled=True,
                             online_probe_hosts={"source.example": True},
                             request_relay_enabled=True, request_relay_source_scope="fixture",
                             request_relay_base_url="http://relay.example:8380",
                             request_relay_state_dir=self.directory.name)
        raw = {"openai-compatibility": [{"name": "fixture", "base-url": "https://source.example/v1",
                "rebuild-mid-system-message": True,
                "api-key-entries": [{"api-key": "synthetic-source-key"}], "models": [{"name": "gpt-6"}]}]}
        records, _ = tool.collect(raw)
        for record in records:
            record["_source_scope"] = "fixture"
        tool.assign_names(records)
        tool.remap_priority(records)
        reference = tool._export_request_relay(records, raw, self.settings)
        bundle = request_relay.load_bundle(self.directory.name)
        account = tool.to_account(records[0], dict(self.settings, request_relay=reference,
                                  _request_relay_bundle=bundle), {records[0]["group"]: 1}, {}, {})
        account.update(id=7, status="inactive", schedulable=False)
        self.api = Admin(account)
        self.reference, self.bundle = reference, bundle

    def test_recover_cli_uses_current_bundle_and_exact_route_without_regenerating(self):
        with patch.object(tool, "load_settings", return_value=self.settings), \
             patch.object(tool, "Sub2Api", return_value=self.api), \
             patch.object(tool, "BULK_HAS_SCHEDULABLE", True), \
             patch.object(request_relay, "verify_runtime"), \
             patch.object(request_relay, "export_bundle", side_effect=AssertionError("diagnostics must not re-export")), \
             patch.object(tool, "_http", return_value=(200, json.dumps({"choices": [{"message": {"content": "5"}}]}))) as http, \
             contextlib.redirect_stdout(io.StringIO()):
            result = tool.probe_cli(["recover", "7"])
        self.assertEqual(result, 0)
        self.assertEqual((self.api.row["status"], self.api.row["schedulable"]), ("active", True))
        self.assertEqual(len(http.call_args_list), 1)
        route = next(iter(self.bundle["routes"].values()))
        self.assertEqual(http.call_args.args[1], self.reference["base_url"] + "/r/" + route["id"] + "/v1/chat/completions")
        self.assertEqual(http.call_args.args[2]["X-Relay-Exact-Key"], "true")

    def test_live_relay_disable_or_scope_change_blocks_bound_http(self):
        route = next(iter(self.bundle["routes"].values()))
        headers = {"Authorization": "Bearer " + route["token"], "X-Relay-Generation": self.bundle["generation"]}
        url = self.reference["base_url"] + "/r/" + route["id"] + "/v1/chat/completions"
        for changes in ({"request_relay_enabled": False}, {"request_relay_source_scope": "other"},
                        {"request_relay_base_url": "http://other.example:8380"},
                        {"request_relay_state_dir": self.directory.name + "-other"},
                        {"request_relay_weighted_bucket": True}):
            with self.subTest(changes=changes):
                policy = dict(self.settings, **changes, _live_probe_policy=True,
                              _current_source_scope=changes.get("request_relay_source_scope", "fixture"),
                              request_relay=self.reference, _request_relay_bundle=self.bundle)
                with self.assertRaises(tool.ApiError):
                    tool._bound_probe_authority(url, headers, policy, "https://source.example/v1")

    def test_diagnose_cli_uses_the_same_active_runtime_without_exporting(self):
        try:
            from tests.test_tls_diagnostics import SourceFixture
        except ImportError:
            from test_tls_diagnostics import SourceFixture
        import tls_diagnostic_transaction as tls
        fixture = SourceFixture()
        self.addCleanup(fixture.close)
        fixture.settings["_live_probe_policy"] = True
        calls = []
        def run(driver, record, source, model, profile, settings, validate):
            validate()
            calls.append(copy.deepcopy(profile))
            return True
        with patch.object(tool, "load_settings", return_value=fixture.settings), \
             patch.object(tool, "Sub2Api", return_value=fixture.api), \
             patch.object(tool, "_fetch_all_accounts", return_value=([fixture.api.row], None)), \
             patch.object(tool, "update_saved_settings"), \
             patch.object(request_relay, "export_bundle", side_effect=AssertionError("unexpected export")), \
             patch.object(tls.NativeDiagnosticDriver, "run", new=run), \
             patch.object(tool, "_http", side_effect=AssertionError("external HTTP forbidden")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.probe_cli(["diagnose", "upstream.example"]), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls, [None])
        self.assertEqual(fixture.api.writes, [])
        self.assertEqual(tls.state_snapshot(fixture.root, fixture.settings_path), fixture.before)

    def test_live_bundle_replacement_blocks_the_previously_bound_request(self):
        route = next(iter(self.bundle["routes"].values()))
        headers = {"Authorization": "Bearer " + route["token"], "X-Relay-Generation": self.bundle["generation"]}
        policy = dict(self.settings, _live_probe_policy=True, _current_source_scope="fixture",
                      request_relay=self.reference, _request_relay_bundle=self.bundle)
        newer = copy.deepcopy(self.bundle)
        newer["generation"] = "replacement-generation"
        with patch.object(request_relay, "load_bundle", return_value=newer), self.assertRaises(tool.ApiError):
            tool._bound_probe_authority(self.reference["base_url"] + "/r/" + route["id"] + "/v1/chat/completions",
                                        headers, policy, "https://source.example/v1")

    def test_current_policy_refreshes_relay_authority_as_well_as_site_consent(self):
        old = dict(self.settings, request_relay=self.reference)
        changed = dict(self.settings, request_relay_enabled=False,
                       request_relay_base_url="http://changed.example:8380", request_relay_source_scope="other")
        with patch.object(tool, "load_settings", return_value=changed):
            current = tool.current_probe_policy(old)
        self.assertFalse(current["request_relay_enabled"])
        self.assertEqual(current["request_relay_base_url"], changed["request_relay_base_url"])
        self.assertEqual(current["_current_source_scope"], "other")

    def test_contract_revoked_after_successful_probe_cannot_be_consumed_by_restore(self):
        initial_settings = copy.deepcopy(self.settings)
        initial_account = copy.deepcopy(self.api.row)
        for changes in ({"request_relay_enabled": False},
                        {"request_relay_base_url": "http://replacement.example:8380"},
                        {"request_relay_state_dir": self.directory.name + "-replacement"},
                        {"request_relay_weighted_bucket": True}):
            with self.subTest(changes=changes):
                settings, api = copy.deepcopy(initial_settings), Admin(initial_account)
                api.on_read = lambda count: settings.update(changes) if count == 2 else None
                with patch.object(tool, "load_settings", return_value=settings), \
                     patch.object(tool, "Sub2Api", return_value=api), \
                     patch.object(tool, "BULK_HAS_SCHEDULABLE", True), \
                     patch.object(request_relay, "verify_runtime"), \
                     patch.object(tool, "_http", return_value=(200, json.dumps({"choices": [{"message": {"content": "5"}}]}))) as http, \
                     contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(tool.probe_cli(["recover", "7"]), 1)
                self.assertEqual(http.call_count, 1)
                self.assertEqual(api.writes, [])
                self.assertEqual((api.row["status"], api.row["schedulable"]), ("inactive", False))

    def test_relay_contract_revoked_during_429_wait_never_resends_restore(self):
        initial = copy.deepcopy(self.settings)
        for changes in ({"request_relay_enabled": False}, {"request_relay_source_scope": "other"},
                        {"request_relay_base_url": "http://replacement.example:8380"},
                        {"request_relay_state_dir": self.directory.name + "-replacement"},
                        {"request_relay_weighted_bucket": True}, {"generation": "replacement"}):
            with self.subTest(changes=changes):
                live, sends, revoked = copy.deepcopy(initial), [], [False]
                account = copy.deepcopy(self.api.row)
                api = tool.Sub2Api(dict(tool.DEFAULTS, sub2api_base_url="http://admin.invalid", sub2api_admin_key="synthetic"))
                original_call = api._call
                def call(method, path, body=None, **kwargs):
                    if method == "GET":
                        return copy.deepcopy(account)
                    return original_call(method, path, body, **kwargs)
                def http(method, url, headers, body, **kwargs):
                    sends.append(copy.deepcopy(body))
                    error = tool.ApiError(429, "limited", "<redacted>")
                    error.retry_after = "1"
                    raise error
                def revoke(wait):
                    live.update({key: value for key, value in changes.items() if key != "generation"})
                    revoked[0] = True
                def active_bundle(directory):
                    return dict(self.bundle, generation="replacement") if revoked[0] and "generation" in changes else self.bundle
                policy = dict(initial, _live_probe_policy=True, request_relay=self.reference)
                evidence = {7: {"snapshot": tool._recovery_snapshot(account), "status": "inactive", "success": True,
                                "recovery_authorized": True, "live_policy": True, "host": "source.example",
                                "source_scope": "fixture", "policy": policy}}
                with patch.object(api, "_call", side_effect=call), \
                     patch.object(tool, "BULK_HAS_SCHEDULABLE", True), \
                     patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)), \
                     patch.object(request_relay, "load_bundle", side_effect=active_bundle), \
                     patch.object(tool, "_http", side_effect=http), \
                     patch.object(tool.time, "sleep", side_effect=revoke), \
                     contextlib.redirect_stdout(io.StringIO()):
                    result = tool._restore_from_job(api, evidence, "inactive", False, 50)
                self.assertEqual(len(sends), 1)
                self.assertEqual(result[:2], (0, 1))


if __name__ == "__main__":
    unittest.main()
