"""Native scheduled tests must reach the bounded consumer diagnostic namespace."""
import copy
import hashlib
import re
import tempfile
import unittest
from unittest.mock import patch

import tool
from relay.config import (RelayError, diagnostic_model_marker, diagnostic_model_alias,
                         diagnostic_model_mapping, is_diagnostic_model_marker)
import request_relay


class DiagnosticModelMarkerTests(unittest.TestCase):
    def setUp(self):
        self.route = {"id": "a" * 40, "diagnostic_prefix": "d" + "a" * 40,
                      "models": ["gemini-3.1-pro", "vendor/model", "模型别名"]}

    def test_marker_is_gemini_safe_and_contains_no_model_text(self):
        for model in self.route["models"]:
            marker = diagnostic_model_marker(self.route, model)
            self.assertTrue(re.fullmatch(r"[A-Za-z0-9_.-]+", marker))
            self.assertEqual(len(marker), 100)
            self.assertEqual(marker, "d" + "a" * 40 + "-" + hashlib.sha256(model.encode()).hexdigest()[:58])
            self.assertEqual(diagnostic_model_alias(self.route, marker), model)
            self.assertNotIn(model, marker)

    def test_foreign_route_unknown_hash_and_unconfigured_model_are_rejected(self):
        marker = diagnostic_model_marker(self.route, self.route["models"][0])
        with self.assertRaises(RelayError):
            diagnostic_model_alias(self.route, "d" + "b" * 40 + marker[41:])
        with self.assertRaises(RelayError):
            diagnostic_model_alias(self.route, marker[:42] + "b" * 64)
        with self.assertRaises(RelayError):
            diagnostic_model_marker(self.route, "unconfigured-model")
        self.assertIsNone(diagnostic_model_alias(self.route, "gemini-3.1-pro"))

    def test_exact_self_maps_preserve_business_mapping_and_override_wildcard_lookup(self):
        original = {"*": "business-wildcard", "gemini-3.1-pro": "gemini-3.1-pro"}
        updated = diagnostic_model_mapping(self.route, original)
        self.assertEqual(original, {"*": "business-wildcard", "gemini-3.1-pro": "gemini-3.1-pro"})
        for model in self.route["models"]:
            marker = diagnostic_model_marker(self.route, model)
            self.assertEqual(updated[marker], marker)
        for key in original:
            self.assertEqual(updated[key], original[key])

    def test_probe_model_selection_never_picks_internal_marker(self):
        model = "gemini-3.1-pro"
        marker = diagnostic_model_marker(self.route, model)
        self.assertEqual(tool._pick_probe_model({marker: marker, model: model}, "gemini"), model)
        self.assertIsNone(tool._pick_probe_model({marker: marker}, "gemini"))

    def test_marker_collision_or_malformed_route_binding_is_rejected(self):
        marker = diagnostic_model_marker(self.route, self.route["models"][0])
        with self.assertRaises(RelayError):
            diagnostic_model_mapping(self.route, {marker: "different-target"})
        with self.assertRaises(RelayError):
            diagnostic_model_marker(dict(self.route, diagnostic_prefix="d" + "b" * 40), self.route["models"][0])

    def simple_source(self):
        document = {"openai-compatibility": [{"name": "fixture", "base-url": "https://allowed.example",
            "api-key-entries": [{"api-key": "synthetic-key"}],
            # Both IDs are deliberately declared. Fidelity conversion no longer
            # invents a raw-name public alias for an alias-only source.
            "models": [{"name": "gpt-6.1", "alias": "*"},
                       {"name": "gpt-6.1", "alias": "gpt-6.1"}]}]}
        records, _ = tool.collect(document)
        for record in records:
            record["_source_scope"] = "default"
            record["new_priority"] = record.get("priority", 0)
        tool.assign_names(records)
        return document, records

    def test_force_all_is_explicit_and_preview_selects_basic_direct_record_without_network(self):
        document, records = self.simple_source()
        settings = dict(tool.DEFAULTS, request_relay_enabled=True, request_relay_weighted_bucket=False,
                        _request_relay_preview=True, model_probe_enabled=False)
        plain = tool._export_request_relay(records, document, settings)
        self.assertFalse(plain["enabled"])
        self.assertNotIn("_request_relay_required", records[0])
        with patch.object(tool, "_http", side_effect=AssertionError("preview network")):
            guarded = tool._export_request_relay(records, document,
                dict(settings, request_relay_force_all_accounts=True))
        self.assertTrue(guarded["enabled"])
        self.assertTrue(guarded["force_all_accounts"])
        self.assertTrue(records[0]["_request_relay_required"])

    def guarded_fixture(self, root):
        document, records = self.simple_source()
        settings = dict(tool.DEFAULTS, request_relay_enabled=True, request_relay_force_all_accounts=True,
            request_relay_weighted_bucket=False, request_relay_state_dir=root,
            request_relay_base_url="http://relay.example:8380", model_probe_enabled=False)
        direct = tool.to_account(records[0], dict(tool.DEFAULTS), {records[0]["group"]: 1}, {}, {})
        reference = tool._export_request_relay(records, document, settings)
        bundle = request_relay.load_bundle(root)
        relayed = tool.to_account(records[0],
            dict(settings, request_relay=reference, _request_relay_bundle=bundle),
            {records[0]["group"]: 1}, {}, {})
        settings["request_relay"] = reference
        return direct, relayed, settings, bundle

    def test_direct_to_guarded_adaptation_preserves_identity_and_wildcard_then_native_marker(self):
        with tempfile.TemporaryDirectory() as root:
            direct, relayed, settings, bundle = self.guarded_fixture(root)
            self.assertEqual(relayed["name"], direct["name"])
            self.assertEqual(relayed["group_ids"], direct["group_ids"])
            self.assertEqual(direct["credentials"]["model_mapping"]["*"], "gpt-6.1")
            self.assertEqual(relayed["credentials"]["model_mapping"]["*"], "*")
            self.assertEqual(direct["credentials"]["base_url"], "https://allowed.example")
            self.assertTrue(relayed["credentials"]["base_url"].startswith("http://relay.example:8380/r/"))
            route = next(iter(bundle["routes"].values()))
            marker = diagnostic_model_marker(route, "gpt-6.1")
            self.assertEqual(relayed["credentials"]["model_mapping"][marker], marker)
            with patch.object(tool, "_load_request_relay", return_value=bundle):
                self.assertEqual(tool._native_scheduled_diagnostic_model(relayed, "gpt-6.1", settings, automatic=False), marker)
                automatic = diagnostic_model_marker(route, "gpt-6.1", automatic=True)
                self.assertEqual(tool._native_scheduled_diagnostic_model(relayed, "gpt-6.1", settings), automatic)
                self.assertEqual(len(automatic), 100)
                self.assertEqual(relayed["credentials"]["model_mapping"][automatic], automatic)
                with self.assertRaises(ValueError):
                    tool._native_scheduled_diagnostic_model(direct, "gpt-6.1", settings)

    def test_legacy_full_digest_is_exactly_current_route_and_model_bound(self):
        for automatic in (False, True):
            for model in self.route['models']:
                prefix = ('a' if automatic else 'd') + self.route['id'] + '-'
                legacy = prefix + hashlib.sha256(model.encode()).hexdigest()
                self.assertEqual(len(legacy), 106)
                self.assertEqual(diagnostic_model_alias(self.route, legacy), model)
                mapping = diagnostic_model_mapping(self.route, {'*': 'business'})
                self.assertEqual(mapping[legacy], legacy)
                self.assertEqual(mapping[diagnostic_model_marker(self.route, model, automatic)],
                                 diagnostic_model_marker(self.route, model, automatic))
                with self.assertRaises(RelayError):
                    diagnostic_model_alias(self.route, legacy[:-1] + ('1' if legacy[-1] != '1' else '2'))

    def test_compact_and_legacy_markers_resolve_but_101_and_unknown_hash_do_not(self):
        from relay.server import resolve_diagnostic_request
        import json
        route = dict(self.route, source_section='openai-compatibility', prefix='r' + self.route['id'])
        model = route['models'][0]
        for automatic in (False, True):
            prefix = ('a' if automatic else 'd') + route['id'] + '-'
            digest = hashlib.sha256(model.encode()).hexdigest()
            for suffix in (digest[:58], digest):
                marker = prefix + suffix
                resolved, _, raw, marked = resolve_diagnostic_request(
                    route, '/v1/chat/completions', json.dumps({'model': marker}).encode())
                self.assertTrue(marked)
                self.assertEqual(json.loads(raw)['model'], model)
                self.assertEqual(bool(resolved.get('automatic_probe')), automatic)
            for suffix in (digest[:59], '0' * 58, '0' * 64):
                marker = prefix + suffix
                with self.assertRaises(RelayError):
                    resolve_diagnostic_request(route, '/v1/chat/completions', json.dumps({'model': marker}).encode())
            with self.assertRaises(RelayError):
                diagnostic_model_alias(route, prefix + digest[:59])

    def test_truncated_digest_collision_cannot_select_either_model(self):
        from relay.server import resolve_diagnostic_request
        import json
        route = dict(self.route, models=['first', 'second'], source_section='openai-compatibility')
        class Digest:
            def __init__(self, raw):
                self.raw = raw
            def hexdigest(self):
                return '1' * 58 + ('2' if self.raw == b'first' else '3') * 6
        with patch('relay.config.hashlib.sha256', side_effect=Digest):
            marker = diagnostic_model_marker(route, 'first')
            self.assertEqual(marker, diagnostic_model_marker(route, 'second'))
            with self.assertRaises(RelayError):
                diagnostic_model_alias(route, marker)
            with self.assertRaises(RelayError):
                resolve_diagnostic_request(route, '/v1/chat/completions', json.dumps({'model': marker}).encode())
            for model in route['models']:
                legacy = marker[:42] + Digest(model.encode()).hexdigest()
                self.assertEqual(diagnostic_model_alias(route, legacy), model)

    def test_actual_account_token_missing_selfmap_and_wrong_model_never_enable_native_plan(self):
        with tempfile.TemporaryDirectory() as root:
            direct, account, settings, bundle = self.guarded_fixture(root)
            with patch.object(tool, "_load_request_relay", return_value=bundle):
                bad = copy.deepcopy(account)
                bad["credentials"]["api_key"] = "stale-token"
                with self.assertRaises(ValueError):
                    tool._native_scheduled_diagnostic_model(bad, "gpt-6.1", settings)
                missing = copy.deepcopy(account)
                missing["credentials"]["model_mapping"] = {"*": "gpt-6.1"}
                with self.assertRaisesRegex(ValueError, "self_mapping_missing"):
                    tool._native_scheduled_diagnostic_model(missing, "gpt-6.1", settings)
                with self.assertRaises(ValueError):
                    tool._native_scheduled_diagnostic_model(account, "unknown-model", settings)


if __name__ == "__main__":
    unittest.main()
