"""New managed fields must never be silently imported or authorize deletion."""
import contextlib
import io
import unittest
from unittest.mock import patch

import tool


class CapabilityGateTests(unittest.TestCase):
    def plan(self, entry, **settings):
        document = {"claude-api-key": [dict({"api-key": "synthetic", "base-url": "https://site.example",
                    "models": [{"name": "claude-opus-9"}]}, **entry)],
                    "unrelated-future-server-feature": {"opaque": True}}
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(document), "fixture")), \
             patch.object(tool, "run_model_probe", return_value=({}, [])):
            return tool.build_plan(dict(tool.DEFAULTS, health_rerank_enabled=False, **settings))

    def test_unknown_managed_field_is_reported_and_blocks_writes_and_prune(self):
        plan, _, _, warnings = self.plan({"future-supported-by-new-cpa": {"opaque": "not-printed"}})
        self.assertTrue(plan["config_incomplete"])
        self.assertIn("future-supported-by-new-cpa", " ".join(plan["unsupported_config_fields"]))
        self.assertNotIn("not-printed", " ".join(warnings))
        with patch.object(tool, "current_probe_policy", side_effect=lambda c: c), \
             patch.object(tool, "load_settings", return_value=tool.DEFAULTS), \
             patch.object(tool, "ensure_groups", side_effect=AssertionError("management write")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                tool.do_import(object(), plan)

    def test_nested_unknown_field_is_reported_but_free_header_names_are_allowed(self):
        plan, _, _, _ = self.plan({"cloak": {"mode": "always", "new-prompt-transform": True},
                                   "headers": {"X-Any-New-Header": "synthetic"}})
        self.assertTrue(plan["config_incomplete"])
        errors = " ".join(plan["unsupported_config_fields"])
        self.assertIn("new-prompt-transform", errors)
        self.assertNotIn("X-Any-New-Header", errors)

    def test_known_contract_and_tool_extensions_do_not_block_other_global_features(self):
        plan, _, _, warnings = self.plan({"disabled": False, "cloak": {"mode": "never"},
                                          "models": [{"name": "claude-opus-9", "thinking": {
                                              "min": 0, "max": 1000, "zero-allowed": True}}]})
        self.assertFalse(plan["config_incomplete"])
        self.assertEqual(plan["unsupported_config_fields"], [])
        self.assertEqual(plan["capability_snapshot"]["consumer_version"], "v8.0.60")


if __name__ == "__main__":
    unittest.main()
