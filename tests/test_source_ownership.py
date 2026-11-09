"""Several sources can share a target without sharing management authority."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

import tool


class SourceOwnershipTests(unittest.TestCase):
    def records(self, scope, count=1):
        document = {"claude-api-key": [
            {"api-key": "synthetic-%d" % i, "base-url": "https://shared.example",
             "models": [{"name": "claude-opus-9"}]} for i in range(count)]}
        records, _ = tool.collect(document)
        for row in records:
            row["_source_scope"] = scope
        tool.assign_names(records)
        tool.remap_priority(records)
        return records

    def account(self, record, aid):
        account = tool.to_account(record, dict(tool.DEFAULTS, source_scope=record["_source_scope"]),
                                  {"Claude": 1}, {}, {})
        account.update(id=aid, status="active", schedulable=True)
        return account

    def test_same_host_key_in_two_scopes_has_distinct_stable_names_with_original_fingerprint(self):
        a, b = self.records("first")[0], self.records("second")[0]
        self.assertNotEqual(a["name"], b["name"])
        self.assertEqual(a["fp"], b["fp"])
        before = a["name"]
        tool.assign_names([a])
        self.assertEqual(a["name"], before)
        legacy = self.records("default")[0]
        raw = copy.deepcopy(legacy)
        raw.pop("_source_scope")
        tool.assign_names([raw])
        self.assertEqual(legacy["name"], raw["name"])

    def test_direct_payload_persists_source_ownership_independent_of_relay(self):
        record = self.records("first")[0]
        account = self.account(record, 1)
        ownership = account["extra"]["cpa2sub2api_source"]
        self.assertEqual(ownership["scope"], "first")
        self.assertEqual(ownership["account_name"], record["name"])
        self.assertEqual(ownership["base_url"], record["base_url"])
        self.assertNotIn("api_key", ownership)
        self.assertEqual(tool.account_source_scope(account), "first")

    def test_prune_and_find_stale_are_limited_to_explicit_owned_source(self):
        first = self.records("first", 2)
        second = self.records("second", 2)
        rows = [self.account(first[0], 1), self.account(first[1], 2), self.account(second[0], 3),
                self.account(second[1], 4), {"id": 5, "name": "unknown-legacy", "notes": tool.NOTE_SENTINEL}]
        class Api:
            def __init__(self):
                self.rows = copy.deepcopy(rows)
                self.deleted = []
            def list_accounts(self, page, size):
                return {"data": {"items": copy.deepcopy(self.rows), "total": len(self.rows)}}
            def _call(self, method, path):
                aid = int(path.rsplit("/", 1)[1])
                row = next((r for r in self.rows if r["id"] == aid), None)
                if row is None:
                    raise tool.ApiError(404, "missing", "<redacted>")
                return copy.deepcopy(row)
            def delete_account(self, aid):
                self.deleted.append(aid)
                self.rows = [r for r in self.rows if r["id"] != aid]
        api = Api()
        stale, error = tool.find_stale_accounts(api, first[:1])
        self.assertIsNone(error)
        self.assertEqual([r["id"] for r in stale], [2])
        removed, failures = tool.prune_stale_accounts(api, first[:1])
        self.assertFalse(failures)
        self.assertEqual([r["id"] for r in removed], [2])
        self.assertEqual({r["id"] for r in api.rows}, {1, 3, 4, 5})

    def test_partial_prune_excludes_other_hosts_even_within_same_source(self):
        records = self.records("first", 2)
        other = copy.deepcopy(records[1])
        other["base_url"] = "https://other.example"
        tool.assign_names([other])
        rows = [self.account(records[0], 1), self.account(other, 2)]
        with patch.object(tool, "_fetch_all_accounts", return_value=(rows, None)):
            stale, error = tool.find_stale_accounts(object(), records[:1], hosts={"shared.example"})
            self.assertEqual((stale, error), ([], None))
            candidates, failures = tool.prune_stale_accounts(object(), records[:1], dry_run=True,
                                                             hosts={"shared.example"})
        self.assertEqual((candidates, failures), ([], []))

    def test_spoofed_same_name_other_scope_cannot_be_synchronized(self):
        first = self.records("first")[0]
        other = self.account(self.records("second")[0], 7)
        other["name"] = first["name"]
        class Api:
            def _call(self, *args):
                raise AssertionError("unowned account read")
            def update_account(self, *args):
                raise AssertionError("unowned account write")
        result = tool.sync_existing_accounts(Api(), [first], dict(tool.DEFAULTS, source_scope="first"),
                                              {"Claude": 1}, {}, {first["name"]: other})
        self.assertEqual(result[:2], (0, 0))
        self.assertTrue(result[2])

    def test_malformed_ownership_is_not_accepted_as_legacy(self):
        account = self.account(self.records("first")[0], 1)
        account["extra"]["cpa2sub2api_source"]["scope"] = "other"
        with self.assertRaises(ValueError):
            tool.account_source_scope(account)

    def test_legacy_default_exact_identity_can_upgrade_but_unknown_cannot(self):
        record = self.records("default")[0]
        account = self.account(record, 7)
        account["extra"].pop("cpa2sub2api_source", None)
        self.assertTrue(tool.account_matches_source(account, record))
        unknown = copy.deepcopy(account)
        unknown["notes"] = tool.NOTE_SENTINEL + " unknown"
        self.assertFalse(tool.account_matches_source(unknown, record))
        wrong_scope = copy.deepcopy(record)
        wrong_scope["_source_scope"] = "second"
        self.assertFalse(tool.account_matches_source(account, wrong_scope))

    def test_plan_calibration_cannot_enable_or_disable_another_scope_except_global_off(self):
        account = self.account(self.records("second")[0], 7)
        class Api:
            def __init__(self):
                self.plan = {"id": 3, "enabled": True, "auto_recover": True,
                             "cron_expression": "0 3 * * 0", "model_id": "claude-opus-9"}
                self.writes = []
            def _call(self, method, path, body=None):
                if method == "GET":
                    return copy.deepcopy(account)
                self.writes.append(body)
                self.plan.update(body)
            def list_test_plans(self, aid):
                return [dict(self.plan)]
        api = Api()
        cfg = {"source_scope": "first", "model_probe_enabled": True,
               "online_probe_hosts": {"shared.example": True}}
        self.assertEqual(tool.ensure_test_plans(api, {account["name"]: 7}, cfg), (0, []))
        self.assertEqual(api.writes, [])
        count, failures = tool.ensure_test_plans(api, {account["name"]: 7}, dict(cfg, model_probe_enabled=False))
        self.assertEqual((count, failures), (1, []))
        self.assertFalse(api.plan["enabled"])

    def test_live_source_scope_mismatch_rejects_old_plan_before_writes(self):
        plan = {"groups": [], "proxies": [], "accounts_raw": self.records("first"),
                "config": {"source_scope": "first"}}
        with patch.object(tool, "load_settings", return_value={"request_relay_source_scope": "second"}), \
             patch.object(tool, "ensure_groups", side_effect=AssertionError("admin mutation")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                tool.do_import(object(), plan)

    def test_explicit_legacy_claim_previews_then_rechecks_and_preserves_manual_state(self):
        record = self.records("default")[0]
        account = self.account(record, 7)
        account["extra"].pop("cpa2sub2api_source", None)
        account.update(status="inactive", schedulable=False, rate_limit_reset_at="keep")
        class Api:
            def __init__(self):
                self.row = copy.deepcopy(account)
                self.writes = []
            def _call(self, *args):
                return copy.deepcopy(self.row)
            def update_account(self, aid, change):
                self.writes.append(copy.deepcopy(change))
                self.row.update(copy.deepcopy(change))
        api = Api()
        preview, failures = tool.claim_source_accounts(api, [7], "first", dry_run=True)
        self.assertFalse(failures)
        self.assertEqual(api.writes, [])
        self.assertEqual(preview[0]["id"], 7)
        actual, failures = tool.claim_source_accounts(api, [7], "first", expected=preview)
        self.assertFalse(failures)
        self.assertEqual(len(actual), 1)
        self.assertEqual(tool.account_source_scope(api.row), "first")
        self.assertEqual(set(api.writes[0]), {"extra"})
        self.assertEqual((api.row["status"], api.row["schedulable"], api.row["rate_limit_reset_at"]),
                         ("inactive", False, "keep"))
        self.assertNotIn("key_sha256", api.row["extra"]["cpa2sub2api_source"])
        api = Api()
        preview, _ = tool.claim_source_accounts(api, [7], "first", dry_run=True)
        api.row["notes"] += " concurrent-edit"
        actual, failures = tool.claim_source_accounts(api, [7], "first", expected=preview)
        self.assertFalse(actual)
        self.assertTrue(failures)
        self.assertEqual(api.writes, [])


if __name__ == "__main__":
    unittest.main()
