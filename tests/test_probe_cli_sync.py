"""Persisted consent and managed sync verification, with no live network."""
import contextlib
import copy
import io
import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool


class ProbeCliSyncTests(unittest.TestCase):
    def test_admin_rate_limit_waits_then_retries_without_proxy(self):
        api = tool.Sub2Api(dict(tool.DEFAULTS, sub2api_base_url="http://example.invalid",
                               sub2api_admin_key="synthetic"))
        error = tool.ApiError(429, "limited", "<redacted>")
        error.retry_after = "7"
        with patch.object(tool, "_http", side_effect=[error, (200, "null")]) as request, \
             patch.object(tool.time, "sleep") as wait, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(api.list_test_plans(7), [])
        wait.assert_called_once_with(7.0)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(request.call_args.kwargs["_allow_proxy_fallback"])

    def test_admin_retry_after_date_and_missing_header_are_conservative(self):
        from email.utils import formatdate
        self.assertEqual(tool.admin_retry_wait("17", now=1000), 17.0)
        self.assertEqual(tool.admin_retry_wait(formatdate(1020, usegmt=True), now=1000), 20.0)
        self.assertEqual(tool.admin_retry_wait(None, now=1000), 60.0)

    def test_admin_http_boundary_preserves_retry_after_and_long_wait_is_not_shortened(self):
        from urllib.error import HTTPError
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 429, "limited", {"Retry-After": "9"}, io.BytesIO(b"limited"))
        with patch.object(tool.urllib.request, "build_opener", return_value=Opener()), \
             patch.object(tool.time, "sleep") as sleep:
            with self.assertRaises(tool.ApiError) as caught:
                tool._http("GET", "http://example.invalid/admin", retries=3, timeout=1,
                           _allow_proxy_fallback=False, _admin_request=True)
        self.assertEqual(caught.exception.retry_after, "9")
        sleep.assert_not_called()
        api = tool.Sub2Api(dict(tool.DEFAULTS, sub2api_base_url="http://example.invalid",
                               sub2api_admin_key="synthetic"))
        caught.exception.retry_after = "3600"
        with patch.object(tool, "_http", side_effect=caught.exception) as request, \
             patch.object(tool.time, "sleep") as sleep:
            with self.assertRaises(tool.ApiError):
                api.list_test_plans(7)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_required_reconciliation_failures_make_process_summary_fail(self):
        base = {"success": [], "failed": [], "skipped": [], "total_planned": 0}
        for field in ("sync_failed", "deactivate_failed", "test_plan_failed", "prune_failed"):
            with self.subTest(field=field), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tool.print_import_summary(dict(base, **{field: [("synthetic", "failed")]})), 1)

    def test_persistent_site_switches_do_not_copy_environment_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = str(pathlib.Path(directory) / "state" / "settings.json")
            with patch.object(tool, "SETTINGS_PATH", settings_path), \
                 patch.object(tool, "CONFIG_YAML", str(pathlib.Path(directory) / "none.yaml")), \
                 patch.object(tool, "_http", side_effect=AssertionError("network")), \
                 patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "false", "SUB2API_ADMIN_KEY": "secret"}), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(tool.load_settings()["model_probe_enabled"])
                self.assertEqual(tool.probe_cli(["enable", "upstream.example"]), 0)
                self.assertFalse(tool.load_settings()["model_probe_enabled"])
                self.assertEqual(tool.probe_cli(["master", "on"]), 0)
                effective = tool.load_settings()
                self.assertTrue(effective["model_probe_enabled"])
                self.assertTrue(effective["online_probe_hosts"]["upstream.example"])
                self.assertEqual(tool.probe_cli(["disable-all"]), 0)
                self.assertFalse(tool.probe_policy.probe_allowed("upstream.example", tool.load_settings()))
            saved = json.loads(pathlib.Path(settings_path).read_text(encoding="utf-8"))
            self.assertNotIn("sub2api_admin_key", saved)
            self.assertTrue(saved["model_probe_enabled"])
            self.assertFalse(saved["online_probe_hosts"]["upstream.example"])

    def test_sync_readback_failure_is_not_success(self):
        class Api:
            def __init__(self, ignore=False):
                self.row = {"id": 7, "name": "wanted", "notes": tool.NOTE_SENTINEL,
                            "platform": "openai", "type": "apikey",
                            "priority": 9, "credentials": {"api_key": "synthetic"}}
                self.ignore = ignore
            def _call(self, method, path):
                return copy.deepcopy(self.row)
            def update_account(self, aid, payload):
                if not self.ignore:
                    self.row.update(payload)
        record = {"name": "wanted"}
        desired = {"name": "wanted", "platform": "openai", "type": "apikey",
                   "notes": tool.NOTE_SENTINEL, "priority": 1,
                   "credentials": {"api_key": "synthetic"}, "extra": {}}
        with patch.object(tool, "to_account", return_value=desired), \
             patch.object(tool, "build_proxy_need_map", return_value={}):
            for ignore, expected in ((False, 1), (True, 0)):
                api = Api(ignore)
                ok, same, failures = tool.sync_existing_accounts(
                    api, [record], {}, {}, {}, {"wanted": copy.deepcopy(api.row)})
                self.assertEqual(ok, expected)
                self.assertEqual(bool(failures), ignore)

    def test_create_and_old_account_sync_share_requested_cooldowns(self):
        document = {"claude-api-key": [{"api-key": "synthetic",
                     "base-url": "https://upstream.example", "models": [{"name": "claude-opus-9"}],
                     "request-scoped-errors": [
                         {"status": code, "action": "continue-and-cooldown", "match": ["upstream unavailable"]}
                         for code in (400, 401, 402, 403)]}]}
        records, _ = tool.collect(document)
        tool.assign_names(records)
        tool.remap_priority(records)
        record = records[0]
        config = dict(tool.DEFAULTS)
        desired = tool.to_account(record, config, {"Claude": 1}, {}, {})
        issued = {rule["error_code"]: rule["duration_minutes"]
                  for rule in desired["credentials"]["temp_unschedulable_rules"]}
        self.assertEqual({code: issued[code] for code in (400, 401, 402, 403)},
                         {400: 1, 401: 30, 402: 15, 403: 1})
        class Api:
            def __init__(self):
                self.row = copy.deepcopy(desired)
                self.row["id"] = 7
                self.row["status"], self.row["schedulable"] = "error", False
                self.row["rate_limit_reset_at"] = "future-real-429"
                for rule in self.row["credentials"]["temp_unschedulable_rules"]:
                    if rule["error_code"] == 402:
                        rule["duration_minutes"] = 180
                    elif rule["error_code"] == 403:
                        rule["duration_minutes"] = 30
            def _call(self, method, path):
                return copy.deepcopy(self.row)
            def update_account(self, aid, patch):
                self.row.update(copy.deepcopy(patch))
        api = Api()
        ok, same, failures = tool.sync_existing_accounts(
            api, records, config, {"Claude": 1}, {}, {record["name"]: copy.deepcopy(api.row)})
        self.assertEqual((ok, same, failures), (1, 0, []))
        actual = {rule["error_code"]: rule["duration_minutes"]
                  for rule in api.row["credentials"]["temp_unschedulable_rules"]}
        self.assertEqual(actual, issued)
        self.assertEqual(api.row["status"], "error")
        self.assertIs(api.row["schedulable"], False)
        self.assertEqual(api.row["rate_limit_reset_at"], "future-real-429")


if __name__ == "__main__":
    unittest.main()
