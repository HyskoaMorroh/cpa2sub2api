"""Removal safety is independent of live upstream diagnostics."""
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool


class Api:
    def __init__(self):
        self.rows = [
            {"id": 1, "name": "wanted", "notes": tool.NOTE_SENTINEL},
            {"id": 2, "name": "stale", "notes": tool.NOTE_SENTINEL},
            {"id": 3, "name": "manual", "notes": "copied from CPA"},
            {"id": 4, "name": "wanted", "notes": tool.NOTE_SENTINEL},
        ]
        self.deleted = []
        self.ignore = False
        self.changed = False
    def list_accounts(self, page, size):
        return {"data": {"items": [dict(r) for r in self.rows], "total": len(self.rows)}}
    def _call(self, method, route):
        aid = int(route.rsplit("/", 1)[1])
        found = next((dict(r) for r in self.rows if r["id"] == aid), None)
        if not found:
            raise tool.ApiError(404, "not found", route)
        if self.changed and aid == 2:
            found["notes"] = "manual ownership now"
        return found
    def delete_account(self, aid):
        self.deleted.append(aid)
        if not self.ignore:
            self.rows = [r for r in self.rows if r["id"] != aid]


class ManagedPruneTests(unittest.TestCase):
    def setUp(self):
        self.records = [{"name": "wanted", "api_key": "synthetic"}]

    def test_preview_no_mutation_and_exact_owned_apply(self):
        api = Api()
        candidates, failures = tool.prune_stale_accounts(api, self.records, dry_run=True)
        self.assertEqual({r["id"] for r in candidates}, {2, 4})
        self.assertFalse(failures)
        self.assertEqual(api.deleted, [])
        removed, failures = tool.prune_stale_accounts(api, self.records)
        self.assertEqual({r["id"] for r in removed}, {2, 4})
        self.assertFalse(failures)
        self.assertEqual({r["id"] for r in api.rows}, {1, 3})

    def test_missing_target_empty_plan_and_failed_snapshot_never_delete(self):
        for records in ([], [{"name": "missing", "api_key": "synthetic"}]):
            api = Api()
            removed, failures = tool.prune_stale_accounts(api, records)
            self.assertFalse(removed)
            self.assertTrue(failures)
            self.assertFalse(api.deleted)
        api = Api()
        with patch.object(tool, "_fetch_all_accounts", return_value=(api.rows, "incomplete")):
            self.assertTrue(tool.prune_stale_accounts(api, self.records)[1])
        self.assertFalse(api.deleted)

    def test_concurrent_ownership_change_and_false_delete_success_fail(self):
        api = Api(); api.changed = True
        removed, failures = tool.prune_stale_accounts(api, self.records)
        self.assertNotIn(2, api.deleted)
        self.assertTrue(failures)
        api = Api(); api.ignore = True
        removed, failures = tool.prune_stale_accounts(api, self.records)
        self.assertFalse(removed)
        self.assertEqual(len(failures), 2)

    def test_invalid_nested_credentials_block_automatic_prune(self):
        from unittest.mock import patch
        import yaml
        for invalid in ({}, "", False, None, [None], [{}], [{"api-key": ""}]):
            document = {"openai-compatibility": [
                {"name": "valid", "base-url": "https://upstream.example/v1",
                 "api-key-entries": [{"api-key": "synthetic"}]},
                {"name": "invalid", "base-url": "https://other.example/v1",
                 "api-key-entries": invalid},
            ]}
            with self.subTest(invalid=invalid), \
                 patch.object(tool, "fetch_cpa_text", return_value=(yaml.safe_dump(document), "synthetic")):
                settings = dict(tool.DEFAULTS, health_rerank_enabled=False, model_probe_enabled=False)
                plan, _, _, _ = tool.build_plan(settings)
                self.assertTrue(plan["config_incomplete"])

    def test_same_url_key_cross_protocol_names_are_stable_and_distinct(self):
        import copy
        document = {
            "codex-api-key": [{"api-key": "synthetic", "base-url": "https://upstream.example/v1",
                               "models": [{"name": "gpt-9"}]}],
            "openai-compatibility": [{"name": "compat", "base-url": "https://upstream.example/v1",
                                      "models": [{"name": "gpt-9"}],
                                      "api-key-entries": [{"api-key": "synthetic"},
                                                          {"api-key": "synthetic"}]}],
        }
        records, _ = tool.collect(document)
        self.assertEqual(len(records), 2)
        tool.assign_names(records)
        names = {row["section"]: row["name"] for row in records}
        self.assertEqual(len(set(names.values())), 2)
        reversed_records = copy.deepcopy(list(reversed(records)))
        tool.assign_names(reversed_records)
        self.assertEqual({row["section"]: row["name"] for row in reversed_records}, names)
        self.assertTrue(all("synthetic" not in name for name in names.values()))

    def test_legacy_collision_migration_waits_for_both_new_targets(self):
        api = Api()
        records = [{"name": "protocol-one", "api_key": "synthetic"},
                   {"name": "protocol-two", "api_key": "synthetic"}]
        api.rows = [{"id": 1, "name": "old-collision", "notes": tool.NOTE_SENTINEL},
                    {"id": 2, "name": "protocol-one", "notes": tool.NOTE_SENTINEL},
                    {"id": 3, "name": "manual", "notes": "manual"}]
        self.assertTrue(tool.prune_stale_accounts(api, records)[1])
        self.assertEqual(api.deleted, [])
        api.rows.append({"id": 4, "name": "protocol-two", "notes": tool.NOTE_SENTINEL})
        removed, failures = tool.prune_stale_accounts(api, records)
        self.assertFalse(failures)
        self.assertEqual([row["id"] for row in removed], [1])
        self.assertEqual({row["id"] for row in api.rows}, {2, 3, 4})


if __name__ == "__main__":
    unittest.main()
