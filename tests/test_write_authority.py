"""Revocation across admin reads and retries must precede every sensitive send."""
import copy
import unittest
from unittest.mock import patch

import tool
import request_relay
try:
    from tests.test_tls_diagnostics import SourceFixture
except ImportError:
    from test_tls_diagnostics import SourceFixture


class WriteAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.account = {"id": 7, "name": "synthetic", "notes": tool.NOTE_SENTINEL,
                        "platform": "openai", "status": "inactive", "schedulable": False,
                        "credentials": {"base_url": "https://site.example", "model_mapping": {"gpt-6": "gpt-6"}}}
        self.live = {"model_probe_enabled": True, "online_probe_hosts": {"site.example": True},
                     "auto_recover_enabled": True}
        self.cfg = dict(copy.deepcopy(self.live), _live_probe_policy=True)

    def test_recovery_revoked_during_fresh_account_get_never_writes(self):
        account, live = self.account, self.live
        class Api:
            def __init__(self):
                self.writes = []
            def _call(self, *args):
                live["model_probe_enabled"] = False
                return copy.deepcopy(account)
            def bulk_update_accounts(self, ids, body):
                self.writes.append(body)
                account.update(body)
        api = Api()
        evidence = {7: {"snapshot": tool._recovery_snapshot(account), "status": "inactive", "success": True,
                        "recovery_authorized": True, "live_policy": True, "host": "site.example"}}
        with patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)), \
             patch.object(tool, "BULK_HAS_SCHEDULABLE", True):
            result = tool._restore_from_job(api, evidence, "inactive", False, 50)
        self.assertEqual(api.writes, [])
        self.assertEqual(result[:2], (0, 1))
        self.assertTrue(result[2])

    def scoped_account(self, scope):
        document = {"codex-api-key": [{"api-key": "synthetic", "base-url": "https://site.example",
                                       "models": [{"name": "gpt-6"}]}]}
        records, _ = tool.collect(document)
        record = records[0]
        record["_source_scope"] = scope
        tool.assign_names(records)
        tool.remap_priority(records)
        record["_relay_models"] = ["gpt-6"]
        self.bundle = request_relay.compile_bundle(records, document, scope)
        self.reference = {"enabled": True, "source_scope": scope, "state_dir": "synthetic-state",
                          "base_url": "http://relay.invalid:8380", "generation": self.bundle["generation"],
                          "weighted_bucket": False}
        self.live.update(source_scope=scope, request_relay_source_scope=scope,
                         request_relay_enabled=True, request_relay_weighted_bucket=False,
                         request_relay_base_url=self.reference["base_url"],
                         request_relay_state_dir=self.reference["state_dir"], request_relay=self.reference)
        self.cfg.update(copy.deepcopy(self.live))
        direct = tool.to_account(record, dict(tool.DEFAULTS, source_scope=scope), {"Codex": 1}, {}, {})
        return dict(request_relay.adapt_account(direct, record, self.bundle, self.reference["base_url"]), id=7)

    def test_scope_changed_during_plan_read_cannot_enable_previous_scope(self):
        account = self.scoped_account("first")
        live = dict(self.live, request_relay_source_scope="first", source_scope="first")
        cfg = dict(live, _live_probe_policy=True)
        class Api:
            def __init__(self):
                self.plan = {"id": 3, "enabled": False, "auto_recover": False,
                             "cron_expression": "0 3 * * 0", "model_id": "gpt-6"}
                self.writes = []
            def _call(self, method, path, body=None):
                if method == "GET":
                    return copy.deepcopy(account)
                self.writes.append(copy.deepcopy(body))
                self.plan.update(body)
            def list_test_plans(self, aid):
                live.update(source_scope="second", request_relay_source_scope="second")
                return [dict(self.plan)]
        api = Api()
        with patch.object(request_relay, "load_bundle", return_value=self.bundle), \
             patch.object(request_relay, "verify_runtime", return_value={"ready": True,
                "diagnostic_guard": "cpa2sub2api-diagnostic-no-redirect-v1", "automatic_output_cap": 16,
                "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}), \
             patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)):
            tool.ensure_test_plans(api, {account["name"]: 7}, cfg)
        self.assertFalse(any(body.get("enabled") or body.get("auto_recover") for body in api.writes))
        self.assertFalse(api.plan["enabled"])

    def test_plan_429_retry_after_revocation_only_sends_disable(self):
        account, live = self.scoped_account("fixture"), self.live
        api = tool.Sub2Api({"sub2api_base_url": "http://admin.fixture", "sub2api_admin_key": "synthetic"})
        plan = {"id": 3, "enabled": False, "auto_recover": False, "cron_expression": "0 3 * * 0", "model_id": "gpt-6"}
        original = api._call
        sends = []
        def call(method, path, body=None, **kwargs):
            if method == "GET":
                return copy.deepcopy(account)
            return original(method, path, body, **kwargs)
        def http(method, url, headers, body, **kwargs):
            sends.append((copy.deepcopy(body), live["model_probe_enabled"]))
            if len(sends) == 1:
                error = tool.ApiError(429, "fixture", "<redacted>")
                error.retry_after = "1"
                raise error
            plan.update(body)
            return 200, "{}"
        def revoke(wait):
            live["model_probe_enabled"] = False
        with patch.object(request_relay, "load_bundle", return_value=self.bundle), \
             patch.object(request_relay, "verify_runtime", return_value={"ready": True,
                "diagnostic_guard": "cpa2sub2api-diagnostic-no-redirect-v1", "automatic_output_cap": 16,
                "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}), \
             patch.object(api, "_call", side_effect=call), \
             patch.object(api, "list_test_plans", side_effect=lambda aid: [dict(plan)]), \
             patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)), \
             patch.object(tool, "_http", side_effect=http), patch.object(tool.time, "sleep", side_effect=revoke):
            tool.ensure_test_plans(api, {account["name"]: 7}, self.cfg)
        self.assertGreaterEqual(len(sends), 2)
        self.assertTrue(sends[0][0]["enabled"])
        self.assertFalse(any((body.get("enabled") or body.get("auto_recover")) and not consent for body, consent in sends))
        self.assertFalse(plan["enabled"])

    def test_recovery_429_retry_after_revocation_does_not_send_second_restore(self):
        account, live = self.account, self.live
        api = tool.Sub2Api({"sub2api_base_url": "http://admin.fixture", "sub2api_admin_key": "synthetic"})
        original = api._call
        sends = []
        def call(method, path, body=None, **kwargs):
            if method == "GET":
                return copy.deepcopy(account)
            return original(method, path, body, **kwargs)
        def http(method, url, headers, body, **kwargs):
            sends.append((copy.deepcopy(body), live["model_probe_enabled"]))
            error = tool.ApiError(429, "fixture", "<redacted>")
            error.retry_after = "1"
            raise error
        evidence = {7: {"snapshot": tool._recovery_snapshot(account), "status": "inactive", "success": True,
                        "recovery_authorized": True, "live_policy": True, "host": "site.example"}}
        with patch.object(api, "_call", side_effect=call), \
             patch.object(tool, "BULK_HAS_SCHEDULABLE", True), \
             patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)), \
             patch.object(tool, "_http", side_effect=http), \
             patch.object(tool.time, "sleep", side_effect=lambda wait: live.update(model_probe_enabled=False)):
            result = tool._restore_from_job(api, evidence, "inactive", False, 50)
        self.assertEqual(len(sends), 1)
        self.assertEqual(result[:2], (0, 1))

    def test_tls_trial_429_retry_after_revocation_never_resends_trial(self):
        fixture = SourceFixture()
        self.addCleanup(fixture.close)
        def revoke(call):
            if call == 1:
                fixture.settings["model_probe_enabled"] = False
        # The source transaction has no model retry. A 429 plus revocation must
        # stop before the second profile and preserve every main-state byte.
        (learned, failures), sends = fixture.diagnose([(429, "{}"), (200, "{}")], revoke)
        self.assertEqual(len(sends), 1)
        self.assertFalse(learned)
        self.assertTrue(failures)
        self.assertEqual(fixture.api.writes, [])
        import tls_diagnostic_transaction as tls
        self.assertEqual(tls.state_snapshot(fixture.root, fixture.settings_path), fixture.before)

    def test_scope_changed_during_existing_account_sync_429_prevents_retry(self):
        records, _ = tool.collect({"codex-api-key": [{"api-key": "synthetic", "base-url": "https://site.example",
                                                   "models": [{"name": "gpt-6"}]}]})
        record = records[0]
        record["_source_scope"] = "first"
        tool.assign_names(records)
        tool.remap_priority(records)
        cfg = dict(tool.DEFAULTS, source_scope="first", _live_probe_policy=True)
        current = dict(tool.to_account(record, cfg, {"Codex": 1}, {}, {}), id=7, priority=999)
        live = dict(self.live, source_scope="first", request_relay_source_scope="first")
        api = tool.Sub2Api({"sub2api_base_url": "http://admin.fixture", "sub2api_admin_key": "synthetic"})
        sends, original = [], api._call
        def call(method, path, body=None, **kwargs):
            if method == "GET":
                return copy.deepcopy(current)
            return original(method, path, body, **kwargs)
        def http(method, url, headers, body, **kwargs):
            sends.append(copy.deepcopy(body))
            if len(sends) == 1:
                error = tool.ApiError(429, "fixture", "<redacted>")
                error.retry_after = "1"
                raise error
            current.update(body)
            return 200, "{}"
        with patch.object(api, "_call", side_effect=call), \
             patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)), \
             patch.object(tool, "_http", side_effect=http), \
             patch.object(tool.time, "sleep", side_effect=lambda wait: live.update(source_scope="second", request_relay_source_scope="second")):
            result = tool.sync_existing_accounts(api, records, cfg, {"Codex": 1}, {}, {record["name"]: current})
        self.assertEqual(len(sends), 1)
        self.assertEqual(result[:2], (0, 0))
        self.assertTrue(result[2])


if __name__ == "__main__":
    unittest.main()
