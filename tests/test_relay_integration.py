"""Relay integration contracts use isolated files and synthetic records only."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import tool
import request_relay
import model_probe


class RelayIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = str(Path(self.tmp.name) / "relay")
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(tool, "SETTINGS_PATH", str(Path(self.tmp.name) / "settings.json")))
        stack.enter_context(patch.object(tool, "CONFIG_YAML", str(Path(self.tmp.name) / "config.yaml")))
        stack.enter_context(patch.object(tool, "OUT_DIR", str(Path(self.tmp.name) / "out")))
        stack.enter_context(patch.dict(os.environ, {"MODEL_PROBE_ENABLED": "", "ONLINE_PROBE_HOSTS": "",
                                                  "REQUEST_RELAY_ENABLED": "", "REQUEST_RELAY_SOURCE_SCOPE": "fixture",
                                                  "SUB2API_ADMIN_KEY": ""}))
        self.document = {"claude-api-key": [
            {"api-key": "synthetic-alpha", "base-url": "https://alpha.example",
             "priority": 3, "cloak": {"mode": "always"}, "models": [{"name": "claude-opus-9"}]},
            {"api-key": "synthetic-beta", "base-url": "https://beta.example",
             "priority": 1, "models": [{"name": "claude-opus-9"}]}]}
        self.records, _ = tool.collect(self.document)
        for record in self.records:
            record["_source_scope"] = "fixture"
        tool.assign_names(self.records)
        tool.remap_priority(self.records)
        for record in self.records:
            record["_relay_models"] = ["claude-opus-9"]
        self.bundle = request_relay.export_bundle(self.records[:1], self.document, "fixture", self.state_dir)
        self.reference = {"enabled": True, "base_url": "http://relay.invalid:8380",
                          "state_dir": self.state_dir, "source_scope": "fixture",
                          "generation": self.bundle["generation"], "weighted_bucket": False}
        self.cfg = dict(tool.DEFAULTS, request_relay=self.reference, _request_relay_bundle=self.bundle)

    def account(self, record, cfg=None):
        return tool.to_account(record, cfg or self.cfg, {"Claude": 1}, {}, {})

    def test_enabled_relay_preserves_source_identity_priorities_and_model_mapping(self):
        source = copy.deepcopy(self.records[0])
        direct = self.account(source, dict(tool.DEFAULTS))
        relayed = self.account(source)
        self.assertNotEqual(relayed["credentials"]["base_url"], source["base_url"])
        self.assertEqual(relayed["credentials"]["base_url"],
                         self.reference["base_url"] + "/r/" + request_relay.route_id(source, "fixture"))
        for key in ("name", "priority", "group_ids", "platform"):
            self.assertEqual(relayed[key], direct[key])
        self.assertTrue(relayed["notes"].startswith(tool.NOTE_SENTINEL))
        self.assertIn("fp=" + source["fp"], relayed["notes"])
        self.assertNotIn("cloak_已写入extra但网关暂未实现", relayed["notes"])
        ordinary = direct["credentials"]["model_mapping"]
        mapping = relayed["credentials"]["model_mapping"]
        self.assertEqual({key: mapping[key] for key in ordinary}, ordinary)
        route = self.bundle["routes"][request_relay.route_id(source, "fixture")]
        diagnostics = {request_relay.diagnostic_model_marker(route, model, automatic=automatic):
                       request_relay.diagnostic_model_marker(route, model, automatic=automatic)
                       for model in route["models"] for automatic in (False, True)}
        diagnostics.update({('a' if automatic else 'd') + route['id'] + '-' + hashlib.sha256(model.encode()).hexdigest():
                            ('a' if automatic else 'd') + route['id'] + '-' + hashlib.sha256(model.encode()).hexdigest()
                            for model in route['models'] for automatic in (False, True)})
        self.assertEqual(set(mapping) - set(ordinary), set(diagnostics))
        self.assertEqual({key: mapping[key] for key in diagnostics}, diagnostics)
        self.assertEqual(source, self.records[0])
        self.assertEqual(tool.account_probe_base_url(relayed), source["base_url"])
        self.assertEqual(tool._host_key(source), ("Claude", "alpha.example"))

    def test_unselected_record_keeps_direct_payload_when_relay_is_enabled(self):
        relayed = self.account(self.records[1])
        self.assertEqual(relayed["credentials"]["base_url"], "https://beta.example")
        self.assertNotIn(request_relay.RELAY_METADATA, relayed["extra"])

    def test_disabling_relay_restores_direct_fields_and_removes_only_managed_metadata(self):
        relayed = self.account(self.records[0])
        relayed["extra"]["operator_runtime"] = {"keep": True}
        direct = self.account(self.records[0], dict(tool.DEFAULTS))
        changed = tool.diff_account_fields(relayed, direct)
        self.assertEqual(changed["credentials"]["base_url"], "https://alpha.example")
        self.assertEqual(changed["credentials"]["api_key"], "synthetic-alpha")
        self.assertNotIn(request_relay.RELAY_METADATA, changed["extra"])
        self.assertEqual(changed["extra"]["operator_runtime"], {"keep": True})
        self.assertFalse(set(changed) & {"status", "schedulable", "rate_limit_reset_at"})

    def test_generation_mismatch_blocks_import_before_any_admin_write_or_prune(self):
        reference = dict(self.reference, generation="f" * 64)
        plan = {"groups": [], "proxies": [], "accounts_raw": self.records,
                "config": dict(tool.DEFAULTS, request_relay=reference)}
        with patch.object(tool, "current_probe_policy", side_effect=lambda c: dict(c)), \
             patch.object(tool, "ensure_groups", side_effect=AssertionError("admin write")), \
             patch.object(tool, "prune_stale_accounts", side_effect=AssertionError("prune")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(request_relay.RelayError):
                tool.do_import(object(), plan)

    def test_live_relay_enable_requires_regeneration_of_old_direct_plan(self):
        cfg = {"_live_probe_policy": True, "request_relay": {"enabled": False}}
        with patch.object(tool, "load_settings", return_value={"request_relay_enabled": True}):
            with self.assertRaises(request_relay.RelayError):
                tool._prepare_request_relay(cfg, self.records)

    def test_live_relay_disable_restores_direct_transport_from_stale_relay_plan(self):
        cfg = dict(self.cfg, _live_probe_policy=True)
        with patch.object(tool, "load_settings", return_value={"request_relay_enabled": False}):
            tool._prepare_request_relay(cfg, self.records)
        direct = self.account(self.records[0], cfg)
        self.assertEqual(direct["credentials"]["base_url"], "https://alpha.example")
        self.assertNotIn(request_relay.RELAY_METADATA, direct["extra"])

    def test_unready_relay_blocks_import_before_any_admin_write_or_prune(self):
        plan = {"groups": [], "proxies": [], "accounts_raw": self.records,
                "config": dict(tool.DEFAULTS, request_relay=self.reference)}
        with patch.object(tool, "current_probe_policy", side_effect=lambda c: dict(c)), \
             patch.object(request_relay, "verify_runtime", side_effect=request_relay.RelayError("not_ready")), \
             patch.object(tool, "ensure_groups", side_effect=AssertionError("admin write")), \
             patch.object(tool, "prune_stale_accounts", side_effect=AssertionError("prune")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(request_relay.RelayError):
                tool.do_import(object(), plan)

    def test_build_plan_stores_reference_only_and_keeps_raw_records_direct(self):
        settings = dict(tool.DEFAULTS, request_relay_enabled=True,
                        request_relay_base_url=self.reference["base_url"],
                        request_relay_state_dir=self.state_dir, request_relay_source_scope="fixture",
                        request_relay_weighted_bucket=False, health_rerank_enabled=False)
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(self.document), "fixture")), \
             patch.object(tool, "run_model_probe", return_value=({}, [])):
            plan, _, _, _ = tool.build_plan(settings)
        self.assertTrue(plan["config"]["request_relay"]["enabled"])
        self.assertEqual(plan["config"]["request_relay"]["generation"], self.bundle["generation"])
        self.assertEqual(plan["accounts_raw"][0]["base_url"], "https://alpha.example")
        public_reference = json.dumps(plan["config"])
        for secret in (self.bundle["state"]["consumer_token"], self.bundle["state"]["control_token"],
                       next(iter(self.bundle["routes"].values()))["token"]):
            self.assertNotIn(secret, public_reference)
        self.assertNotIn("_request_relay_bundle", plan["config"])

    def test_probe_plan_uses_original_host_and_does_not_accept_relay_host_grant(self):
        account = request_relay.adapt_account(self.account(self.records[0], dict(tool.DEFAULTS)),
                                               self.records[0], self.bundle, self.reference["base_url"])
        account.update(id=7, status="active", schedulable=True)
        class Api:
            def __init__(self):
                self.plan = {"id": 3, "enabled": True, "auto_recover": True,
                             "cron_expression": "0 3 * * 0", "model_id": "claude-opus-9"}
            def _call(self, method, path, body=None):
                if method == "GET":
                    return copy.deepcopy(account)
                self.plan.update(body)
            def list_test_plans(self, aid):
                return [dict(self.plan)]
        api = Api()
        policy = dict(self.cfg, source_scope="fixture", model_probe_enabled=True,
                      online_probe_hosts={"alpha.example": True})
        with patch.object(request_relay, "verify_runtime", return_value={
                "ready": True, "diagnostic_guard": "cpa2sub2api-diagnostic-no-redirect-v1",
                "automatic_output_cap": 16,
                "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}):
            self.assertEqual(tool.ensure_test_plans(api, {account["name"]: 7}, policy), (1, []))
            self.assertTrue(api.plan["enabled"])
            route = self.bundle["routes"][request_relay.route_id(self.records[0], "fixture")]
            self.assertEqual(api.plan["model_id"],
                             request_relay.diagnostic_model_marker(route, "claude-opus-9", automatic=True))
            self.assertEqual(tool.ensure_test_plans(api, {account["name"]: 7}, dict(
                policy, online_probe_hosts={"relay.invalid": True})), (1, []))
        self.assertFalse(api.plan["enabled"])

    def test_malformed_original_metadata_fails_closed(self):
        account = request_relay.adapt_account(self.account(self.records[0], dict(tool.DEFAULTS)),
                                               self.records[0], self.bundle, self.reference["base_url"])
        account["extra"][request_relay.RELAY_METADATA]["base_url"] = "https://unauthorized.example"
        with self.assertRaises(request_relay.RelayError):
            tool.account_probe_base_url(account)

    def test_weighted_bucket_recovery_uses_bound_exact_key_probe_and_fresh_snapshot(self):
        document = copy.deepcopy(self.document)
        document["claude-api-key"].append(dict(document["claude-api-key"][0], **{"api-key": "synthetic-second-alpha"}))
        records, _ = tool.collect(document)
        for record in records:
            record["_source_scope"] = "fixture"
        tool.assign_names(records)
        tool.remap_priority(records)
        records = [r for r in records if r["base_url"] == "https://alpha.example"]
        bundle = request_relay.compile_bundle(records, document, "fixture", weighted_bucket=True)
        account = request_relay.adapt_account(self.account(records[0], dict(tool.DEFAULTS)), records[0], bundle,
                                               self.reference["base_url"])
        account.update(id=7, status="inactive", schedulable=False)
        class Api:
            base, key, user_agent, timeout = "http://admin.invalid", "synthetic-admin", "fixture", 1
            def _call(self, method, path, body=None):
                return copy.deepcopy(account)
        policy = {"source_scope": "fixture", "model_probe_enabled": True,
                  "online_probe_hosts": {"alpha.example": True},
                  "explicit_recovery_account_ids": [7], "revive_proven_inactive": True,
                  "request_relay": dict(self.reference, generation=bundle["generation"], weighted_bucket=True)}
        with patch.object(tool, "_pick_probe_model", return_value="claude-opus-9"), \
             patch.object(request_relay, "load_bundle", return_value=bundle), \
             patch.object(request_relay, "verify_runtime", return_value={"ready": True}), \
             patch.object(tool, "_http", return_value=(200, '{"type":"message","content":[{"type":"text","text":"5"}]}')) as http:
            evidence, failures = tool.probe_import_recovery(Api(), {account["name"]: 7}, policy)
        self.assertFalse(failures)
        self.assertTrue(evidence[7]["success"])
        self.assertEqual(http.call_count, 1)
        self.assertTrue(http.call_args.args[1].startswith(self.reference["base_url"] + "/r/"))
        self.assertEqual(http.call_args.args[2]["X-Relay-Exact-Key"], "true")
        self.assertEqual(http.call_args.kwargs["probe_authority_url"], "https://alpha.example")

    def test_tool_model_probe_binds_original_policy_to_verified_relay_unit(self):
        seen = []
        policy = dict(self.cfg, model_probe_enabled=True, online_probe_hosts={"alpha.example": True})
        def inspect(units, http, **kwargs):
            seen.extend(copy.deepcopy(units))
            return {}
        with patch.object(request_relay, "verify_runtime", return_value={"ready": True}), \
             patch.object(tool.model_probe, "probe_all", side_effect=inspect):
            tool.run_model_probe(self.records, policy)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["base_url"], "https://alpha.example")
        self.assertEqual(seen[0]["host"], "alpha.example")
        self.assertIn("/r/", seen[0]["request_base_url"])
        self.assertIn(self.bundle["generation"], seen[0]["transport_generation"])

    def test_explicit_tls_profiles_bind_consumer_transport_without_guessing_detected_boolean(self):
        settings = dict(tool.DEFAULTS, request_relay_enabled=True,
                        request_relay_base_url=self.reference["base_url"],
                        request_relay_state_dir=self.state_dir, request_relay_source_scope="fixture",
                        request_relay_tls_profiles={"alpha.example": {"impersonate": "chrome"}},
                        detected_tls_fingerprint_hosts=["beta.example"], health_rerank_enabled=False)
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(self.document), "fixture")), \
             patch.object(tool, "run_model_probe", return_value=({}, [])):
            plan, _, _, _ = tool.build_plan(settings)
        alpha, beta = plan["accounts_raw"]
        self.assertEqual(alpha["_relay_tls"], {"impersonate": "chrome"})
        self.assertNotIn("_relay_tls", beta)
        bundle = request_relay.load_bundle(self.state_dir)
        self.assertEqual(bundle["transports"][request_relay.route_id(alpha, "fixture")]["impersonate"], "chrome136")

    def test_unknown_explicit_tls_profile_fails_instead_of_exporting_direct(self):
        settings = dict(tool.DEFAULTS, request_relay_enabled=True,
                        request_relay_base_url=self.reference["base_url"],
                        request_relay_state_dir=self.state_dir, request_relay_source_scope="fixture",
                        request_relay_tls_profiles={"alpha.example": {"impersonate": "future-unknown"}},
                        health_rerank_enabled=False)
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(self.document), "fixture")), \
             patch.object(tool, "run_model_probe", return_value=({}, [])):
            with self.assertRaises(request_relay.RelayError):
                tool.build_plan(settings)

    def test_preview_validates_relay_without_creating_state_files(self):
        state_dir = str(Path(self.tmp.name) / "never-created")
        settings = dict(tool.DEFAULTS, request_relay_enabled=True,
                        request_relay_state_dir=state_dir, request_relay_source_scope="fixture",
                        health_rerank_enabled=False, model_probe_enabled=False, _request_relay_preview=True)
        with patch.object(tool, "fetch_cpa_text", return_value=(tool._yaml().safe_dump(self.document), "fixture")), \
             patch.object(tool, "_http", side_effect=AssertionError("network forbidden")):
            plan, _, _, _ = tool.build_plan(settings)
        self.assertEqual(plan["config"]["request_relay"]["generation"], "preview-not-exported")
        self.assertFalse(Path(state_dir).exists())

    def test_relay_alias_pool_mapping_is_preserved_without_reintroducing_lower_tier(self):
        record = copy.deepcopy(self.records[0])
        record["models_raw"] = [{"name": "claude-opus-9-a", "alias": "pooled"},
                                {"name": "claude-opus-9-b", "alias": "pooled"}]
        mapping = {"pooled": "claude-opus-9-b", "claude-opus-9-a": "claude-opus-9-a",
                   "claude-opus-9-b": "claude-opus-9-b"}
        self.assertEqual(request_relay.relay_model_mapping(record, mapping)["pooled"], "pooled")
        restricted = {"pooled": "claude-opus-9-b", "claude-opus-9-b": "claude-opus-9-b"}
        self.assertNotEqual(request_relay.relay_model_mapping(record, restricted).get("pooled"), "pooled")

    def test_sync_relay_on_and_off_preserves_manual_runtime_state(self):
        direct = self.account(self.records[0], dict(tool.DEFAULTS))
        direct.update(id=7, status="inactive", schedulable=False, rate_limit_reset_at="preserve-cooldown")
        class Api:
            def __init__(self):
                self.row = copy.deepcopy(direct)
            def _call(self, method, path):
                return copy.deepcopy(self.row)
            def update_account(self, aid, payload):
                self.row.update(copy.deepcopy(payload))
        api = Api()
        for cfg, relayed in ((self.cfg, True), (dict(tool.DEFAULTS), False)):
            result = tool.sync_existing_accounts(api, self.records[:1], cfg, {"Claude": 1}, {},
                                                  {self.records[0]["name"]: copy.deepcopy(api.row)})
            self.assertEqual(result, (1, 0, []))
            self.assertEqual(request_relay.RELAY_METADATA in api.row["extra"], relayed)
            self.assertEqual((api.row["status"], api.row["schedulable"], api.row["rate_limit_reset_at"]),
                             ("inactive", False, "preserve-cooldown"))

    def probe_unit(self):
        route = next(iter(self.bundle["routes"].values()))
        return {"section": "claude-api-key", "host": "alpha.example", "platform": "anthropic",
                "base_url": "https://alpha.example", "api_key": "synthetic-alpha",
                "headers": {"Authorization": "Bearer synthetic-source-header"},
                "latest_gen": 9, "prev_gen": None, "latest_model": "claude-opus-9", "prev_model": None,
                "request_base_url": self.reference["base_url"] + "/r/" + route["id"],
                "request_api_key": route["token"], "transport_generation": self.bundle["generation"] + ":" + route["id"]}

    def test_relay_model_probe_targets_exact_route_but_authorizes_original_host(self):
        unit = self.probe_unit()
        calls = []
        def http(*args, **kwargs):
            calls.append((args, kwargs))
            return 200, '{"type":"message","content":[{"type":"text","text":"5"}]}'
        policy = {"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}}
        result = model_probe.probe_unit(http, unit, fallback_proxy="http://outbound.invalid:7890", settings=policy)
        self.assertTrue(result["latest"])
        args, kwargs = calls[0]
        self.assertTrue(args[1].startswith(unit["request_base_url"] + "/"))
        self.assertEqual(kwargs["probe_authority_url"], unit["base_url"])
        self.assertIsNone(kwargs["proxy"])
        self.assertEqual(args[2]["X-Relay-Exact-Key"], "true")
        self.assertNotIn("synthetic-alpha", json.dumps(calls))
        self.assertNotIn("synthetic-source-header", json.dumps(calls))

    def test_relay_probe_cannot_replace_original_host_permission_with_relay_permission(self):
        unit = self.probe_unit()
        with patch.object(model_probe.probe_policy, "permanently_denied", side_effect=lambda h: "alpha.example" in h), \
             patch.object(tool, "_http", side_effect=AssertionError("upstream HTTP")) as http:
            result = model_probe.probe_unit(http, unit, settings={
                "model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True, "relay.invalid": True}})
        self.assertFalse(result["latest"])
        http.assert_not_called()

    def test_relay_probe_cache_is_bound_to_transport_generation(self):
        unit = self.probe_unit()
        base_key = model_probe.cache_key(unit["section"], unit["host"], 9, None)
        old = {base_key: {"latest": True, "prev": False, "detail": "old direct", "ts": 1}}
        calls = []
        def http(*args, **kwargs):
            calls.append((args, kwargs))
            return 200, '{"type":"message","content":[{"type":"text","text":"5"}]}'
        with patch.object(model_probe, "load_cache", return_value=old), patch.object(model_probe, "save_cache"):
            result = model_probe.probe_all([unit], http, out_dir=self.tmp.name, settings={
                "model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}})
        self.assertTrue(result[(unit["section"], unit["host"])]["latest"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("缓存", result[(unit["section"], unit["host"])]["detail"])

    def test_plain_probe_authority_override_cannot_bypass_actual_target_policy(self):
        policy = {"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}}
        opener = unittest.mock.Mock()
        with patch.object(tool.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(tool.ApiError):
                tool._http("POST", "https://protected.invalid/v1/messages", {}, {}, retries=1,
                           probe_settings=policy, probe_authority_url="https://alpha.example")
        opener.open.assert_not_called()

    def test_bound_relay_http_checks_exact_route_token_scope_generation_and_live_authority(self):
        route = next(iter(self.bundle["routes"].values()))
        target = self.reference["base_url"] + "/r/" + route["id"] + "/v1/messages"
        headers = {"Authorization": "Bearer " + route["token"], "X-Relay-Generation": self.bundle["generation"]}
        policy = {"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True},
                  "request_relay": self.reference, "_request_relay_bundle": self.bundle}
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status, response.read.return_value = 200, b'{"type":"message","content":[]}'
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.object(tool.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(tool._http("POST", target, headers, {}, timeout=1, retries=1,
                                       probe_settings=policy, probe_authority_url="https://alpha.example")[0], 200)
        self.assertEqual(opener.open.call_count, 1)
        for bad_url, bad_headers, bad_policy, authority in (
            (target, dict(headers, Authorization="Bearer wrong"), policy, "https://alpha.example"),
            (target, dict(headers, **{"X-Relay-Generation": "f" * 64}), policy, "https://alpha.example"),
            (target, headers, dict(policy, request_relay=dict(self.reference, source_scope="wrong")), "https://alpha.example"),
            (target, headers, policy, "https://beta.example"),
            ("https://protected.invalid/r/" + route["id"] + "/v1/messages", headers, policy, "https://alpha.example"),
            (target, headers, None, "https://alpha.example"),
        ):
            with self.subTest(authority=authority, url=bad_url), \
                 patch.object(tool.urllib.request, "build_opener", return_value=opener):
                opener.open.reset_mock()
                with self.assertRaises(tool.ApiError):
                    tool._http("POST", bad_url, bad_headers, {}, timeout=1, retries=1,
                               probe_settings=bad_policy, probe_authority_url=authority)
                opener.open.assert_not_called()

    def test_probe_retry_rechecks_revocation_before_next_http_request(self):
        allowed = {"model_probe_enabled": True, "online_probe_hosts": {"alpha.example": True}}
        revoked = {"model_probe_enabled": False, "online_probe_hosts": {"alpha.example": False}}
        opener = unittest.mock.Mock()
        opener.open.side_effect = [tool.urllib.error.URLError("synthetic retry"), AssertionError("revoked HTTP")]
        with patch.object(tool.urllib.request, "build_opener", return_value=opener), \
             patch.object(tool, "load_settings", side_effect=[allowed, allowed, revoked]), \
             patch.object(tool.time, "sleep"):
            with self.assertRaises(tool.ApiError):
                tool._http("POST", "https://alpha.example/v1/messages", {}, {}, timeout=1, retries=2,
                           probe_settings=dict(allowed, _live_probe_policy=True), _allow_proxy_fallback=False)
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
