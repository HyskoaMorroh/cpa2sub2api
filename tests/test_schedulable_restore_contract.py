"""Offline contract regressions; no deployed schema or service is assumed."""
import contextlib
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool
import request_relay

CONSENT = {"explicit_recovery_account_ids": [7], "model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}


class FakeApi:
    def __init__(self, status="inactive", schedulable=False):
        self.row = {"id": 7, "notes": tool.NOTE_SENTINEL, "name": "synthetic",
                    "status": status, "schedulable": schedulable}
        self.base, self.key, self.user_agent, self.timeout = "http://example.invalid", "test", "test", 3
        self.row["platform"] = "openai"
        self.row["credentials"] = {"base_url": "https://upstream.example",
                                   "model_mapping": {"gpt-test": "gpt-test"}}
        document = {"openai-compatibility": [{"name": "fixture",
                    "base-url": "https://upstream.example",
                    "api-key-entries": [{"api-key": "synthetic-source-key"}],
                    "models": [{"name": "gpt-test"}]}]}
        records, _ = tool.collect(document)
        records[0]["_source_scope"] = "fixture"
        records[0]["_relay_models"] = ["gpt-test"]
        self.bundle = request_relay.compile_bundle(records, document, "fixture")
        self.reference = {"enabled": True, "source_scope": "fixture",
                          "generation": self.bundle["generation"], "state_dir": "synthetic-state",
                          "base_url": "http://relay.invalid:8380", "weighted_bucket": False}
        self.cfg = dict(CONSENT, source_scope="fixture", request_relay_source_scope="fixture",
                        request_relay_enabled=True, request_relay_weighted_bucket=False,
                        request_relay_base_url=self.reference["base_url"],
                        request_relay_state_dir=self.reference["state_dir"], request_relay=self.reference)
        self.row = request_relay.adapt_account(self.row, records[0], self.bundle, self.reference["base_url"])
        self.writes = []
        self.ignore = False
        self.read_error = False
        self.write_error = False
        self.evidence = "success"
        self.after = {}

    @contextlib.contextmanager
    def probe_context(self):
        with patch.object(request_relay, "load_bundle", return_value=self.bundle), \
             patch.object(request_relay, "verify_runtime", return_value={
                "ready": True, "diagnostic_guard": "cpa2sub2api-diagnostic-no-redirect-v1",
                "automatic_output_cap": 16,
                "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}):
            yield

    def list_accounts(self, page, size):
        return {"data": {"items": [dict(self.row)], "total": 1}}

    def list_test_plans(self, aid):
        return {"data": [{"id": 1}]}

    def list_test_results(self, pid):
        return {"data": [{"status": self.evidence}]}

    def update_account(self, aid, payload):
        # Mirrors cached PUT schema: unknown schedulable is silently ignored.
        self.writes.append(("PUT", [aid], dict(payload)))
        if "status" in payload:
            self.row["status"] = payload["status"]

    def bulk_update_accounts(self, ids, payload):
        self.writes.append(("bulk", list(ids), dict(payload)))
        if self.write_error:
            raise RuntimeError("secret-do-not-print")
        if not self.ignore:
            self.row.update(payload)
        self.row.update(self.after)

    def _call(self, method, route):
        assert (method, route) == ("GET", "/api/v1/admin/accounts/7")
        if self.read_error:
            raise RuntimeError("secret-do-not-print")
        return {"code": 0, "data": dict(self.row)}


class RestoreContractTests(unittest.TestCase):
    def setUp(self):
        self.support = patch.object(tool, "BULK_HAS_SCHEDULABLE", True)
        self.support.start()
        self.addCleanup(self.support.stop)

    def revive(self, api, **kwargs):
        evidence = {7: {"snapshot": tool._recovery_snapshot(api.row), "status": api.row["status"], "recovery_authorized": True, "success": api.evidence == "success"}}
        return tool.revive_proven_inactive(api, {"revive_proven_inactive": True}, evidence=evidence, **kwargs)

    def reclaim(self, api):
        with api.probe_context(), patch.object(tool, "_http", return_value=(
                200, '{"choices":[{"message":{"content":"5"}}]}')):
            evidence, _ = tool.probe_import_recovery(api, {"synthetic": 7}, api.cfg)
            return tool.reclaim_schedulable(api, evidence=evidence)

    def test_revive_uses_bulk_and_restores_both_fields(self):
        api = FakeApi()
        self.assertEqual(self.revive(api), (1, 1, None))
        self.assertEqual(api.writes, [("bulk", [7], {"status": "active", "schedulable": True})])
        self.assertIs(api.row["schedulable"], True)

    def test_http_success_without_state_change_is_not_recovery(self):
        api = FakeApi()
        api.ignore = True
        result = self.revive(api)
        self.assertEqual(result[:2], (0, 1))
        self.assertTrue(result[2])

    def test_readback_requires_both_fields_and_matching_id(self):
        for change in ({"status": "inactive"}, {"status": None},
                       {"schedulable": False}, {"schedulable": None},
                       {"schedulable": 1}, {"id": 8}):
            with self.subTest(change=change):
                api = FakeApi()
                api.after = change
                result = self.revive(api)
                self.assertEqual(result[:2], (0, 1))
                self.assertTrue(result[2])

    def test_errors_fail_closed_without_secret_output(self):
        for field in ("read_error", "write_error"):
            api = FakeApi()
            setattr(api, field, True)
            result = self.revive(api)
            self.assertEqual(result[:2], (0, 1))
            self.assertTrue(result[2])
            self.assertNotIn("secret-do-not-print", result[2])

    def test_unsupported_schema_does_not_write(self):
        api = FakeApi()
        with patch.object(tool, "BULK_HAS_SCHEDULABLE", False):
            result = self.revive(api)
        self.assertEqual(result[:2], (0, 1))
        self.assertTrue(result[2])
        self.assertEqual(api.writes, [])

    def test_dry_run_or_failed_evidence_does_not_write(self):
        api = FakeApi()
        self.assertEqual(self.revive(api, dry_run=True), (0, 1, None))
        api.evidence = "failed"
        self.assertEqual(self.revive(api), (0, 0, None))
        self.assertEqual(api.writes, [])

    def test_reclaim_does_not_force_active_after_concurrent_error(self):
        api = FakeApi(status="active")
        api.after = {"status": "error"}
        result = self.reclaim(api)
        self.assertEqual(api.writes, [("bulk", [7], {"schedulable": True})])
        self.assertEqual(result[:2], (0, 1))
        self.assertTrue(result[2])

    def test_reclaim_requires_explicit_false_and_respects_cooldown(self):
        for value in (None, 0, "", True):
            api = FakeApi(status="active", schedulable=value)
            self.assertEqual(self.reclaim(api), (0, 0, None))
            self.assertEqual(api.writes, [])
        api = FakeApi(status="active")
        api.row.pop("schedulable")
        self.assertEqual(self.reclaim(api), (0, 0, None))
        api = FakeApi(status="active")
        api.row["temp_unschedulable_until"] = "future"
        self.assertEqual(self.reclaim(api), (0, 0, None))

    def test_reclaim_verified_success(self):
        self.assertEqual(self.reclaim(FakeApi(status="active")), (1, 1, None))


class FreshEvidenceTests(unittest.TestCase):
    good = 'data: {"type":"test_complete","success":true}\n\n'

    def test_historical_success_without_job_never_writes(self):
        api = FakeApi()
        self.assertEqual(tool.revive_proven_inactive(api, {}), (0, 0, None))
        self.assertEqual(tool.reclaim_schedulable(api), (0, 0, None))
        self.assertEqual(api.writes, [])

    def test_only_explicit_terminal_success_is_accepted(self):
        for raw in ('<html>ok</html>', '', 'data: {"success":true}\n\n',
                    'data: {"type":"test_complete","success":1}\n\n',
                    'data: {"type":"test_start"}\n\n',
                    'data: broken\n\n',
                    self.good.rstrip(),
                    'data: {"type":"error"}\n\n' + self.good):
            self.assertFalse(tool._successful_test_stream(raw))
        self.assertTrue(tool._successful_test_stream(self.good))

    def test_real_route_once_per_id_and_no_historical_lookup(self):
        api = FakeApi()
        with api.probe_context(), patch.object(tool, "_http", return_value=(
                200, '{"choices":[{"message":{"content":"5"}}]}')) as http:
            evidence, errors = tool.probe_import_recovery(api, {"a": 7, "b": 7}, api.cfg)
        self.assertEqual(set(evidence), {7})
        self.assertEqual(errors, [])
        self.assertEqual(http.call_count, 1)
        route = next(iter(api.bundle["routes"].values()))
        self.assertEqual(http.call_args.args[1], api.reference["base_url"] + "/r/" + route["id"] + "/v1/chat/completions")
        self.assertEqual(http.call_args.args[3]["model"], "gpt-test")
        self.assertEqual(http.call_args.args[2]["X-Relay-Exact-Key"], "true")
        self.assertEqual(http.call_args.kwargs['retries'], 1)
        self.assertFalse(http.call_args.kwargs['_allow_proxy_fallback'])

    def test_synthetic_and_missing_mapping_never_tested(self):
        for update in ({"extra": {"synthetic_ui_test": True}}, {"credentials": {"base_url": "https://upstream.example"}}):
            api = FakeApi()
            if "extra" in update:
                update = dict(update, extra=dict(api.row["extra"], **update["extra"]))
            api.row.update(update)
            with api.probe_context(), patch.object(tool, "_http") as http:
                evidence, errors = tool.probe_import_recovery(api, {"a": 7}, api.cfg)
            self.assertFalse(evidence)
            self.assertTrue(errors)
            http.assert_not_called()

    def test_configuration_or_status_changed_during_probe_is_skipped(self):
        for change in ({"status": "active"}, {"notes": "manual"},
                       {"credentials": {"model_mapping": {"other": "other"}}},
                       {"proxy_id": 9}, {"updated_at": "changed"},
                       {"priority": 7}, {"rate_multiplier": 0.4}):
            api = FakeApi()
            def mutate(*args, **kwargs):
                api.row.update(change)
                return 200, '{"choices":[{"message":{"content":"5"}}]}'
            with api.probe_context(), patch.object(tool, "_http", side_effect=mutate):
                evidence, errors = tool.probe_import_recovery(api, {"a": 7}, api.cfg)
            self.assertFalse(evidence)
            self.assertTrue(errors)
            self.assertFalse(api.writes)

    def test_changed_after_probe_is_not_written(self):
        api = FakeApi()
        with api.probe_context(), patch.object(tool, "_http", return_value=(
                200, '{"choices":[{"message":{"content":"5"}}]}')):
            evidence, _ = tool.probe_import_recovery(api, {"a": 7}, api.cfg)
        api.row['proxy_id'] = 4
        result = tool.revive_proven_inactive(api, {}, evidence=evidence)
        self.assertEqual(result[:2], (0, 1))
        self.assertTrue(result[2])
        self.assertFalse(api.writes)


if __name__ == "__main__":
    unittest.main()
