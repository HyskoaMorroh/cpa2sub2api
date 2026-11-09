"""Single-site imports retain the full source plan for identity and ordering."""
import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tool


class PartialImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.doc = {"claude-api-key": [
            {"api-key": "synthetic-alpha", "base-url": "https://alpha.example", "priority": 2,
             "models": [{"name": "claude-opus-9"}]},
            {"api-key": "synthetic-beta", "base-url": "https://beta.example", "priority": 1,
             "models": [{"name": "claude-opus-9"}]}]}
        self.settings = dict(tool.DEFAULTS, health_rerank_enabled=False, request_relay_source_scope="default")
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(self.doc), "fixture")), \
             patch.object(tool, "run_model_probe", return_value=({}, [])):
            self.plan = tool.build_plan(self.settings)[0]

    def test_site_selection_preserves_whole_plan_and_original_priority(self):
        selected = tool.select_import_hosts(self.plan, ["alpha.example"])
        self.assertEqual(len(selected["accounts_raw"]), 2)
        self.assertEqual(selected["selected_hosts"], ["alpha.example"])
        self.assertEqual(selected["accounts_raw"], self.plan["accounts_raw"])
        self.assertNotIn("selected_hosts", self.plan)

    def test_partial_cli_preview_does_not_import_and_apply_uses_shared_import(self):
        with patch.object(tool, "load_settings", return_value=self.settings), \
             patch.object(tool, "build_plan", return_value=(self.plan, "", "fixture", [])), \
             patch.object(tool, "Sub2Api"), patch.object(tool, "do_import", return_value={"fixture": True}) as run, \
             patch.object(tool, "print_import_summary", return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tool.import_cli(["--host", "alpha.example", "--preview"]), 0)
            run.assert_not_called()
            self.assertEqual(tool.import_cli(["--host", "alpha.example", "--yes"]), 0)
        self.assertEqual(run.call_args.args[1]["selected_hosts"], ["alpha.example"])
        self.assertEqual(len(run.call_args.args[1]["accounts_raw"]), 2)

    def test_partial_import_mutates_only_selected_accounts_and_skips_global_fallback(self):
        selected = copy.deepcopy(self.plan)
        selected["selected_hosts"] = ["alpha.example"]
        records = self.plan["accounts_raw"]
        rows = [dict(tool.to_account(r, self.plan["config"], {"Claude": 1}, {}, {}), id=i + 1,
                     status="active", schedulable=True) for i, r in enumerate(records)]
        payloads = []
        class Api:
            base, key = "http://admin.invalid", "synthetic-admin"
            def update_account(self, aid, payload):
                self_test.assertEqual(aid, 1)
            def bulk_update_accounts(self, ids, patch):
                self_test.assertTrue(set(ids) <= {1})
        self_test = self
        have = {r["name"]: rows[i] for i, r in enumerate(records)}
        def sync(api, recs, cfg, gmap, pmap, have_map):
            payloads.extend(tool.to_account(r, cfg, gmap, pmap, {}) for r in recs)
            return len(recs), 0, []
        with patch.object(tool, "load_settings", return_value=self.settings), \
             patch.object(tool, "OUT_DIR", str(Path(self.tmp.name) / "out")), \
             patch.object(tool, "ensure_groups", return_value=({"Claude": 1}, [])), \
             patch.object(tool, "ensure_proxies", return_value={}), \
             patch.object(tool, "existing_account_names", return_value=(set(have), None)), \
             patch.object(tool, "existing_accounts_by_name", return_value=(have, None)), \
             patch.object(tool, "sync_existing_accounts", side_effect=sync), \
             patch.object(tool, "_fetch_all_accounts", return_value=(rows, None)), \
             patch.object(tool, "ensure_test_plans", return_value=(1, [])) as plans, \
             patch.object(tool, "probe_import_recovery", return_value=({}, [])), \
             patch.object(tool, "reclaim_schedulable", return_value=(0, 0, None)), \
             patch.object(tool, "revive_proven_inactive", return_value=(0, 0, None)), \
             patch.object(tool, "find_stale_accounts", return_value=([], None)) as stale, \
             patch.object(tool, "prune_stale_accounts", return_value=([], [])) as prune, \
             patch.object(tool, "apply_fallback_chain", side_effect=AssertionError("global group mutation")), \
             contextlib.redirect_stdout(io.StringIO()):
            result = tool.do_import(Api(), selected)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["name"], records[0]["name"])
        self.assertEqual(payloads[0]["priority"], records[0]["new_priority"])
        self.assertEqual(set(plans.call_args.args[1].values()), {1})
        self.assertEqual([r["base_url"] for r in stale.call_args.args[1]], ["https://alpha.example"])
        self.assertEqual(prune.call_args.kwargs["hosts"], {"alpha.example"})
        self.assertEqual(result["selected_hosts"], ["alpha.example"])


if __name__ == "__main__":
    unittest.main()
