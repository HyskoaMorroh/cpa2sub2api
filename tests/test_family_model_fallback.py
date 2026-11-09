"""Known model families keep independent selection and exact-key probe evidence."""
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
import model_probe
import model_selection as selection


def models(*names):
    return [{"name": name, "alias": name} for name in names]


class FamilySelectionTests(unittest.TestCase):
    def test_known_family_probe_generation_uses_the_same_parser(self):
        for name in ("gpt-42.10.2-tools", "gemini-42.10.2-pro", "claude-opus-42-10-20260101",
                     "grok-42.10.2-tools", "kimi-k42.10-tools", "moonshot-v2-128k",
                     "deepseek-v42.10-0324", "deepseek-r4-0528"):
            family, version = selection.model_family_version(name)
            self.assertEqual(model_probe._model_gen(name, family), version)
    def test_kimi_known_numeric_lineages_and_context_suffixes_are_not_confused(self):
        source = models("kimi-k12", "kimi-k12-0905", "kimi-k12.5", "kimi-k12.5-tools", "kimi-k12.6-fast",
                        "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k", "kimi-latest")
        latest = selection.select_highest_models(source, "openai", "openai-compatibility")
        self.assertEqual({m["name"] for m in latest}, {m["name"] for m in source[2:4] + source[5:]})
        both = selection.select_highest_models(source, "openai", "openai-compatibility", probe={"families": {
            "kimi-k": {"latest": False, "prev": True, "latest_gen": [12, 5], "prev_gen": [12, 0]}}})
        self.assertEqual({m["name"] for m in both}, {m["name"] for m in source if not selection.is_low_tier(m["name"])})

    def test_deepseek_v_and_r_are_separate_and_unversioned_aliases_are_not_guessed(self):
        source = models("deepseek-v12.9", "deepseek-v12.10", "deepseek-v12.10-exp", "deepseek-v13-fast",
                        "deepseek-r2", "deepseek-r3", "deepseek-r3-0528", "deepseek-chat", "deepseek-reasoner")
        latest = selection.select_highest_models(source, "openai", "openai-compatibility")
        self.assertEqual({m["name"] for m in latest}, {m["name"] for m in source[1:3] + source[5:]})
        both = selection.select_highest_models(source, "openai", "openai-compatibility", probe={"families": {
            "deepseek-r": {"latest": False, "prev": True, "latest_gen": [3, 0], "prev_gen": [2, 0]},
            "deepseek-v": {"latest": True, "prev": False}}})
        self.assertIn("deepseek-r2", {m["name"] for m in both})
        self.assertNotIn("deepseek-v12.9", {m["name"] for m in both})
    def test_claude_minor_version_shapes_and_dates_are_consistent_across_protocols(self):
        self.assertEqual(selection.claude_version("claude-opus-42-10-20260101"), (42, 10))
        self.assertEqual(selection.claude_version("claude-42-10-sonnet"), (42, 10))
        self.assertEqual(selection.claude_version("claude-opus-42-20260101"), (42, 0))
        source = models("claude-opus-42-10", "claude-42-10-sonnet", "claude-opus-42-10-20260101",
                        "claude-opus-42-9", "claude-opus-42-9-thinking", "claude-opus-42-8", "claude-opus-42-11-fast")
        for platform, section in (("anthropic", "claude-api-key"), ("openai", "openai-compatibility")):
            with self.subTest(platform=platform):
                latest = selection.select_highest_models(source, platform, section, series={"anthropic": 41})
                self.assertEqual({m["name"] for m in latest}, {m["name"] for m in source[:3]})
                both = selection.select_highest_models(source, platform, section, series={"anthropic": 41},
                    probe={"families": {"claude": {"latest": False, "prev": True, "latest_gen": [42, 10], "prev_gen": [42, 9]}}})
                self.assertEqual({m["name"] for m in both}, {m["name"] for m in source[:5]})
    def test_native_claude_uses_observed_future_release_before_series_and_forbidden_tiers(self):
        source = models("claude-opus-42", "claude-sonnet-42-tools", "claude-opus-41", "claude-opus-43-fast")
        picked = selection.select_highest_models(source, "anthropic", "claude-api-key", series={"anthropic": 41})
        self.assertEqual({m["name"] for m in picked}, {"claude-opus-42", "claude-sonnet-42-tools"})
        picked = selection.select_highest_models(source, "anthropic", "claude-api-key", series={"anthropic": 41},
            probe={"families": {"claude": {"latest": False, "prev": True, "latest_gen": [42, 0], "prev_gen": [41, 0]}}})
        self.assertEqual({m["name"] for m in picked}, {"claude-opus-42", "claude-sonnet-42-tools", "claude-opus-41"})

    def test_native_gemini_filters_forbidden_tiers_before_highest_valid_pro(self):
        source = models("gemini-42.10-pro", "gemini-42.10-pro-thinking", "gemini-42.9-pro", "gemini-43-pro-fast")
        picked = selection.select_highest_models(source, "gemini", "gemini-api-key", series={"gemini": 41})
        self.assertEqual({m["name"] for m in picked}, {"gemini-42.10-pro", "gemini-42.10-pro-thinking"})

    def test_native_models_without_hint_remain_numeric_and_filtered_all_stays_empty(self):
        for platform, section, source, expected in (
                ("anthropic", "claude-api-key", models("claude-opus-42", "claude-sonnet-41"), {"claude-opus-42"}),
                ("gemini", "gemini-api-key", models("gemini-42.10-pro", "gemini-42.9-pro"), {"gemini-42.10-pro"}),
                ("openai", "codex-api-key", models("gpt-42.10", "gpt-42.9"), {"gpt-42.10"})):
            with self.subTest(platform=platform):
                self.assertEqual({m["name"] for m in selection.select_highest_models(source, platform, section)}, expected)
        for platform, section, source in (
                ("anthropic", "claude-api-key", models("claude-opus-42-fast", "claude-sonnet-41-mini")),
                ("gemini", "gemini-api-key", models("gemini-42.10-pro-fast", "gemini-42.9-flash")),
                ("openai", "codex-api-key", models("gpt-42.10-fast", "gpt-42.9-mini"))):
            with self.subTest(platform=platform, filtered_all=True):
                self.assertEqual(selection.select_highest_models(source, platform, section), [])
    def test_codex_highest_numeric_release_keeps_suffixes_and_only_proven_previous(self):
        source = models("gpt-42.10", "gpt-42.10-tools", "gpt-42.9", "gpt-42.9-sol", "gpt-42.8", "gpt-42.11-mini")
        picked = selection.select_highest_models(source, "openai", "codex-api-key", series={"openai": 42})
        self.assertEqual({m["name"] for m in picked}, {"gpt-42.10", "gpt-42.10-tools"})
        picked = selection.select_highest_models(source, "openai", "codex-api-key", series={"openai": 42},
            probe={"families": {"gpt": {"latest": False, "prev": True, "latest_gen": [42, 10], "prev_gen": [42, 9]}}})
        self.assertEqual({m["name"] for m in picked}, {"gpt-42.10", "gpt-42.10-tools", "gpt-42.9", "gpt-42.9-sol"})
    def test_entire_gpt_family_has_one_latest_release_until_previous_is_proven(self):
        source = models("gpt-42.10", "gpt-42.10-tools", "gpt-41.9", "gpt-40.2")
        self.assertEqual({m["name"] for m in selection.select_highest_models(source, "openai", "openai-compatibility")},
                         {"gpt-42.10", "gpt-42.10-tools"})
        selected = selection.select_highest_models(source, "openai", "openai-compatibility", probe={"families": {
            "gpt": {"latest": False, "prev": True, "latest_gen": [42, 10], "prev_gen": [41, 9]}}})
        self.assertEqual({m["name"] for m in selected}, {"gpt-42.10", "gpt-42.10-tools", "gpt-41.9"})
    def test_grok_versions_are_numeric_and_equivalent_trailing_zero_forms_share_release(self):
        self.assertEqual(selection.grok_version("grok-40"), selection.grok_version("grok-40.0.0"))
        self.assertGreater(selection.grok_version("grok-40.10.2"), selection.grok_version("grok-40.9.20"))
        source = models("grok-40.10", "grok-40.10.0", "grok-40.10-preview", "grok-40.9.2", "grok-unknown-999")
        self.assertEqual({m["name"] for m in selection.select_highest_models(source, "openai", "openai-compatibility")},
                         {"grok-40.10", "grok-40.10.0", "grok-40.10-preview", "grok-unknown-999"})
    def test_grok_numeric_versions_keep_all_top_variants_and_unknown_names(self):
        source = models("grok-12.9", "grok-12.10", "grok-12.10-reasoning", "grok-12.10-fast",
                        "grok-12.11-mini", "grok-12.11-flash", "grok-beta", "vendor-999-preview")
        picked = selection.select_highest_models(source, "openai", "openai-compatibility")
        self.assertEqual({m["name"] for m in picked},
                         {"grok-12.10", "grok-12.10-reasoning", "grok-beta", "vendor-999-preview"})

    def test_empty_known_eligible_family_never_rebounds(self):
        self.assertEqual(selection.select_highest_models(models("gemini-12-nonpro", "grok-20-fast"),
                                                        "openai", "openai-compatibility"), [])

    def test_family_probe_success_cannot_add_another_family_previous_generation(self):
        source = models("grok-12.10", "grok-12.9", "grok-12.9-reasoning", "grok-12.9-fast",
                        "gemini-12.10-pro", "gemini-12.9-pro", "claude-opus-12", "claude-opus-11",
                        "gpt-12.10", "gpt-12.9", "vendor-999-preview")
        probe = {"families": {"grok": {"latest": False, "prev": True, "latest_gen": [12, 10], "prev_gen": [12, 9]},
                              "gemini": {"latest": True, "prev": False},
                              "claude": {"latest": True, "prev": False},
                              "gpt": {"latest": True, "prev": False}}}
        names = {m["name"] for m in selection.select_highest_models(source, "openai", "openai-compatibility", probe=probe)}
        self.assertIn("grok-12.10", names)
        self.assertIn("grok-12.9", names)
        self.assertIn("grok-12.9-reasoning", names)
        self.assertNotIn("grok-12.9-fast", names)
        self.assertNotIn("gemini-12.9-pro", names)
        self.assertNotIn("claude-opus-11", names)
        self.assertNotIn("gpt-12.9", names)
        self.assertIn("vendor-999-preview", names)

    def test_each_compatible_known_family_can_add_only_its_tested_previous_generation(self):
        cases = (("grok", ("grok-12.10", "grok-12.9", "grok-12.8"), [12, 10], [12, 9]),
                 ("gemini", ("gemini-12.10-pro", "gemini-12.9-pro", "gemini-12.8-pro"), [12, 10], [12, 9]),
                 ("claude", ("claude-opus-12", "claude-opus-11", "claude-opus-10"), [12, 0], [11, 0]),
                 ("gpt", ("gpt-12.10", "gpt-12.9", "gpt-12.8"), [12, 10], [12, 9]))
        for family, names, latest, previous in cases:
            with self.subTest(family=family):
                result = selection.select_highest_models(models(*names), "openai", "openai-compatibility",
                    probe={"families": {family: {"latest": False, "prev": True, "latest_gen": latest, "prev_gen": previous}}})
                self.assertEqual({m["name"] for m in result}, set(names[:2]))


class FamilyPlanTests(unittest.TestCase):
    def test_real_plan_keeps_families_and_tested_key_evidence_separate(self):
        source = {"openai-compatibility": [{"name": "fixture", "base-url": "https://family.example/v1",
            "api-key-entries": [{"api-key": "synthetic-first"}, {"api-key": "synthetic-second"}],
            "models": models("gpt-12.10", "gpt-12.9", "grok-12.10", "grok-12.9", "grok-12.9-fast",
                             "gemini-12.10-pro", "gemini-12.9-pro", "claude-opus-12", "claude-opus-11")} ]}
        settings = dict(tool.DEFAULTS, model_probe_enabled=True, online_probe_hosts={"family.example": True},
                        health_rerank_enabled=False, fallback_proxy="", request_relay_enabled=False)
        calls = []
        def http(method, url, headers, body, **kwargs):
            name = body["model"]
            calls.append((name, headers.get("Authorization")))
            if name in {"grok-12.10", "gemini-12.10-pro", "claude-opus-12"}:
                return 503, '{}'
            return 200, json.dumps({"choices": [{"message": {"content": "5"}}]})
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(tool, "OUT_DIR", directory), \
             patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(source), "synthetic")), \
             patch.object(tool, "_http", side_effect=http), contextlib.redirect_stdout(io.StringIO()):
            plan = tool.build_plan(settings)[0]
        self.assertEqual(len(plan["accounts_raw"]), 2)
        first, second = plan["accounts_raw"]
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(key == "Bearer synthetic-first" for _, key in calls))
        self.assertIn("_probe", first)
        self.assertNotIn("_probe", second)
        names = {m["name"] for m in selection.select_highest_models(first["models_raw"], "openai",
                    "openai-compatibility", probe=first["_probe"])}
        self.assertIn("grok-12.9", names)
        self.assertIn("gemini-12.9-pro", names)
        self.assertIn("claude-opus-11", names)
        self.assertNotIn("gpt-12.9", names)
        untested = {m["name"] for m in selection.select_highest_models(second["models_raw"], "openai", "openai-compatibility")}
        self.assertNotIn("grok-12.9", untested)

    def test_identical_numeric_versions_do_not_share_family_cache(self):
        key1 = model_probe.cache_key("openai-compatibility", "family.example", (12, 10), (12, 9), family="gpt")
        key2 = model_probe.cache_key("openai-compatibility", "family.example", (12, 10), (12, 9), family="grok")
        self.assertNotEqual(key1, key2)

    def test_provenance_changes_with_key_catalog_scope_and_base(self):
        record = {"api_key": "synthetic-first", "base_url": "https://family.example/v1", "models_raw": models("grok-12.10"),
                  "_source_scope": "first", "source_section": "openai-compatibility"}
        digest = tool._probe_record_provenance(record)
        for change in ({"api_key": "synthetic-second"}, {"models_raw": models("grok-12.9")},
                       {"_source_scope": "second"}, {"base_url": "https://family.example/custom"}):
            self.assertNotEqual(tool._probe_record_provenance(dict(record, **change)), digest)
        self.assertNotIn("synthetic-first", digest)

    def test_explicit_family_fallback_retests_cached_previous_success(self):
        unit = {"section": "openai-compatibility", "host": "family.example", "family": "grok", "provenance": "synthetic-digest",
                "platform": "openai", "base_url": "https://family.example/v1", "api_key": "synthetic",
                "latest_gen": (12, 10), "prev_gen": (12, 9), "latest_model": "grok-12.10", "prev_model": "grok-12.9"}
        key = model_probe.cache_key(unit["section"], unit["host"], unit["latest_gen"], unit["prev_gen"],
                                    family="grok", provenance=unit["provenance"])
        calls = []
        def http(method, url, headers, body, **kwargs):
            calls.append(body["model"])
            return (503, '{}') if len(calls) == 1 else (200, '{"choices":[{"message":{"content":"5"}}]}')
        with patch.object(model_probe, "load_cache", return_value={key: {"latest": False, "prev": True}}), \
             patch.object(model_probe, "save_cache"):
            result = model_probe.probe_all([unit], http, out_dir="synthetic-unused", settings={
                        "model_probe_enabled": True, "online_probe_hosts": {"family.example": True}})
        self.assertEqual(calls, ["grok-12.10", "grok-12.9"])
        self.assertTrue(result[(unit["section"], unit["host"], "grok")]["prev"])

    def test_default_off_and_permanent_deny_have_zero_family_requests(self):
        records, _ = tool.collect({"openai-compatibility": [{"name": "fixture", "base-url": "https://family.example/v1",
                                "models": models("grok-12.10", "grok-12.9"), "api-key-entries": [{"api-key": "synthetic"}]}]})
        with patch.object(tool, "_http", side_effect=AssertionError("no network")) as http:
            self.assertEqual(tool.run_model_probe(records, {}), ({}, []))
            with patch.object(tool.probe_policy, "permanently_denied", return_value=True):
                self.assertEqual(tool.run_model_probe(records, {"model_probe_enabled": True,
                                 "online_probe_hosts": {"family.example": True}}), ({}, []))
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
