"""Offline regression tests: consent must precede every upstream probe."""
import copy
import pathlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import tool
import request_relay


class ProbePolicyTests(unittest.TestCase):
    def guarded_plan_fixture(self, model):
        document = {"openai-compatibility": [{"name": "fixture", "base-url": "https://upstream.example",
            "api-key-entries": [{"api-key": "synthetic-source-key"}], "models": [{"name": model}]}]}
        records, _ = tool.collect(document)
        record = records[0]
        record["_source_scope"], record["_relay_models"] = "fixture", [model]
        tool.assign_names(records)
        tool.remap_priority(records)
        bundle = request_relay.compile_bundle(records, document, "fixture")
        settings = dict(tool.DEFAULTS, source_scope="fixture", request_relay_source_scope="fixture",
            request_relay_enabled=True, request_relay_force_all_accounts=True,
            request_relay_weighted_bucket=False, request_relay_state_dir="synthetic-state",
            request_relay_base_url="http://relay.invalid:8380", model_probe_enabled=True,
            online_probe_hosts={"upstream.example": True})
        settings["request_relay"] = tool._request_relay_reference(settings, bundle)
        direct = tool.to_account(record, dict(tool.DEFAULTS, source_scope="fixture"),
            {record["group"]: 1}, {}, {})
        account = request_relay.adapt_account(direct, record, bundle, settings["request_relay_base_url"])
        account.update(id=7, status="active", schedulable=True)
        self.enterContext(patch.object(request_relay, "load_bundle", return_value=bundle))
        self.enterContext(patch.object(request_relay, "verify_runtime", return_value={"ready": True,
            "diagnostic_guard": "cpa2sub2api-diagnostic-no-redirect-v1"}))
        return account, settings, next(iter(bundle["routes"].values()))

    def test_default_never_calls_model_probe(self):
        records = [{"base_url": "https://upstream.example", "api_key": "synthetic",
                    "source_section": "codex-api-key", "models_raw": [{"name": "gpt-6"}]}]
        with patch.object(tool.model_probe, "probe_all", side_effect=AssertionError("network")):
            self.assertEqual(tool.run_model_probe(records, {}), ({}, []))
            self.assertEqual(tool.run_model_probe(records, {"model_probe_enabled": True}), ({}, []))

    def test_weekly_default(self):
        self.assertEqual(tool.DEFAULTS["auto_recover_cron"], "0 3 * * 0")
        self.assertFalse(tool.DEFAULTS["model_probe_enabled"])

    def test_host_policy_requires_both_switches(self):
        import probe_policy
        site = "https://UPSTREAM.example.:443/v1"
        self.assertEqual(probe_policy.normalize_host(site), "upstream.example")
        for config in ({}, {"model_probe_enabled": True},
                       {"online_probe_hosts": {"upstream.example": True}}):
            self.assertFalse(probe_policy.probe_allowed(site, config))
        self.assertTrue(probe_policy.probe_allowed(site, {
            "model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}))
        for invalid in ("https://user@upstream.example", "https://upstream.example\\@evil.example",
                        "ftp://upstream.example", "https://upstream.example:bad"):
            self.assertFalse(probe_policy.probe_allowed(invalid, {
                "model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}))

    def test_removed_managed_parameters_are_cleared(self):
        old = {"credentials": {"api_key": "synthetic", "base_url": "https://upstream.example",
                               "custom_headers": {"X-Old": "stale"}},
               "extra": {"enable_tls_fingerprint": True}}
        desired = {"credentials": {"api_key": "synthetic", "base_url": "https://upstream.example"},
                   "extra": {}}
        changed = tool.diff_account_fields(old, desired)
        self.assertEqual(changed["credentials"], desired["credentials"])
        self.assertEqual(changed["extra"], {})

    def test_unknown_runtime_metadata_survives_managed_cleanup(self):
        existing = {"credentials": {"api_key": "masked", "header_overrides": {"X-Old": "1"},
                                    "runtime_vendor_state": {"opaque": True}},
                    "extra": {"enable_tls_fingerprint": True, "runtime_health": 0.9}}
        changed = tool.diff_account_fields(existing, {"credentials": {"api_key": "synthetic"},
                                                      "extra": {}})
        self.assertEqual(changed["credentials"], {"api_key": "synthetic",
                                                 "runtime_vendor_state": {"opaque": True}})
        self.assertEqual(changed["extra"], {"runtime_health": 0.9})

    def test_native_billing_probe_disabled_on_sync(self):
        changed = tool.diff_account_fields(
            {"extra": {"upstream_billing_probe_enabled": True}},
            {"upstream_billing_probe_enabled": False})
        self.assertIs(changed["upstream_billing_probe_enabled"], False)
        self.assertEqual(tool.diff_account_fields({}, {"upstream_billing_probe_enabled": False}), {})

    def test_low_tier_and_cross_family_never_reappear_on_fallback(self):
        import model_selection
        for models, platform, section in (
            ([{"name": "gemini-9-flash"}], "gemini", "gemini-api-key"),
            ([{"name": "gpt-9-mini"}], "openai", "codex-api-key"),
            ([{"name": "claude-opus-9"}], "openai", "codex-api-key"),
            ([{"name": "gpt-9"}], "anthropic", "claude-api-key"),
        ):
            self.assertEqual(model_selection.select_highest_models(
                models, platform, source_section=section), [])
        record = {"models_raw": [{"name": "gemini-9-flash"}], "platform": "gemini",
                  "source_section": "gemini-api-key"}
        self.assertTrue(tool.serves_nothing(record))

    def test_digest_deny_covers_normalized_subdomains(self):
        import hashlib
        import probe_policy
        digest = hashlib.sha256(b"blocked.example").hexdigest()
        with patch.object(probe_policy, "DENIED_HOST_DIGESTS", {digest}):
            for host in ("https://BLOCKED.example.:443/v1", "https://sub.blocked.example"):
                self.assertTrue(probe_policy.permanently_denied(host))
                self.assertFalse(probe_policy.probe_allowed(host, {
                    "model_probe_enabled": True, "online_probe_hosts": {host: True}}))

    def test_old_plans_disabled_without_consent_and_migrated_with_consent(self):
        account, consent, route = self.guarded_plan_fixture("gpt-6")
        class Api:
            def __init__(self):
                self.plan = {"id": 3, "enabled": True, "auto_recover": True,
                             "cron_expression": "*/30 * * * *", "model_id": "gpt-6"}
            def _call(self, method, path, body=None):
                if method == "GET":
                    return copy.deepcopy(account)
                self.plan.update(body)
            def list_test_plans(self, aid):
                return [dict(self.plan)]
        api = Api()
        self.assertEqual(tool.ensure_test_plans(api, {"site": 7}, {}), (1, []))
        self.assertFalse(api.plan["enabled"])
        self.assertEqual(api.plan["cron_expression"].split()[1:], ["3", "*", "*", "0"])
        self.assertEqual(tool.ensure_test_plans(api, {"site": 7}, consent), (1, []))
        self.assertTrue(api.plan["enabled"])
        self.assertTrue(api.plan["auto_recover"])
        self.assertEqual(api.plan["cron_expression"].split()[1:], ["3", "*", "*", "0"])
        self.assertEqual(api.plan["model_id"], request_relay.diagnostic_model_marker(route, "gpt-6", automatic=True))
        self.assertEqual(account["credentials"]["model_mapping"][api.plan["model_id"]], api.plan["model_id"])

    def test_plan_error_envelope_is_failure_not_empty_success(self):
        class Api:
            def _call(self, method, path, body=None):
                return {"id": 7, "notes": tool.NOTE_SENTINEL,
                        "credentials": {"base_url": "https://upstream.example"}}
            def list_test_plans(self, aid):
                return {"code": 500, "message": "backend failed"}
        reconciled, failures = tool.ensure_test_plans(Api(), {"stale": 7}, {})
        self.assertEqual(reconciled, 0)
        self.assertTrue(failures)

    def test_managed_plan_targets_preserve_stale_duplicate_ids(self):
        rows = [{"id": 1, "name": "same", "notes": tool.NOTE_SENTINEL},
                {"id": 2, "name": "same", "notes": tool.NOTE_SENTINEL},
                {"id": 3, "name": "manual", "notes": "manual"}]
        self.assertEqual(set(tool.managed_plan_targets(rows).values()), {1, 2})

    def test_site_probe_consent_never_authorizes_manual_status_recovery(self):
        class Api:
            def _call(self, *args):
                raise AssertionError("no account reads or upstream tests without explicit recovery ids")
        cfg = {"model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}
        self.assertEqual(tool.probe_import_recovery(Api(), {"manual": 7}, cfg), ({}, []))

    def test_probe_redirect_never_forwards_credentials(self):
        captured = []
        class Opener:
            def open(self, request, timeout):
                handler = next(h for h in captured if isinstance(h, tool.urllib.request.HTTPRedirectHandler))
                for target in ("http://upstream.example:8080/steal", "https://other.example/steal",
                               "https://upstream.example/redirect"):
                    with self_test.assertRaises(tool.ApiError):
                        handler.redirect_request(request, None, 302, "redirect", {}, target)
                raise tool.ApiError(0, "stopped", "<redacted>")
        self_test = self
        def build(*handlers):
            captured.extend(handlers)
            return Opener()
        cfg = {"model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}
        with patch.object(tool.urllib.request, "build_opener", side_effect=build):
            with self.assertRaises(tool.ApiError):
                tool._http("POST", "https://upstream.example/v1/messages",
                           {"Authorization": "Bearer synthetic"}, {}, retries=1,
                           _allow_proxy_fallback=False, probe_settings=cfg)

    def test_saved_plan_is_not_live_consent(self):
        saved = {"model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}
        with patch.object(tool, "load_settings", return_value={"model_probe_enabled": False,
                                                             "online_probe_hosts": {}}):
            effective = tool.current_probe_policy(saved)
        self.assertFalse(effective["model_probe_enabled"])
        self.assertEqual(effective["online_probe_hosts"], {})

    def test_long_reconcile_rechecks_consent_before_enabling_plan(self):
        account, allowed, route = self.guarded_plan_fixture("gpt-9")
        live = copy.deepcopy(allowed)
        class Api:
            def __init__(self):
                self.plan = {"id": 3, "enabled": True, "auto_recover": True,
                             "cron_expression": "*/30 * * * *", "model_id": "gpt-9"}
                self.writes = []
            def _call(self, method, path, body=None):
                if method == "GET":
                    return copy.deepcopy(account)
                self.writes.append(body)
                self.plan.update(body)
            def list_test_plans(self, aid):
                # A slow management read can outlive the operator's grant.
                live.update(model_probe_enabled=False, online_probe_hosts={})
                return [dict(self.plan)]
        api = Api()
        with patch.object(tool, "load_settings", side_effect=lambda: copy.deepcopy(live)), \
             patch.object(tool, "_http", side_effect=AssertionError("upstream requests forbidden")):
            count, failures = tool.ensure_test_plans(api, {"site": 7}, dict(allowed, _live_probe_policy=True))
        self.assertEqual((count, failures), (1, []))
        self.assertFalse(api.plan["enabled"])
        self.assertFalse(api.plan["auto_recover"])
        self.assertTrue(api.writes)
        self.assertFalse(any(body.get("enabled") or body.get("auto_recover") for body in api.writes))
        self.assertEqual(api.plan["cron_expression"].split()[1:], ["3", "*", "*", "0"])
        self.assertEqual(api.plan["model_id"], request_relay.diagnostic_model_marker(route, "gpt-9", automatic=True))

    def test_requested_cooldown_durations_override_historical_long_defaults(self):
        self.assertEqual({code: tool.cooldown_minutes_for(code) for code in (400, 401, 402, 403)},
                         {400: 1, 401: 30, 402: 15, 403: 1})
        self.assertEqual([row["duration_minutes"] for row in tool.build_domain_scoped_cooldown([])
                          if row["error_code"] == 402], [15])

    def test_http_200_literal_null_plan_list_is_empty_only_at_transport_boundary(self):
        api = tool.Sub2Api(dict(tool.DEFAULTS, sub2api_base_url="http://example.invalid",
                               sub2api_admin_key="synthetic"))
        with patch.object(tool, "_http", return_value=(200, "null")):
            self.assertEqual(api.list_test_plans(7), [])
        for status, payload in ((503, "null"), (200, "not-json"), (200, "{}"),
                                (200, '{"code":500,"message":"failed"}')):
            with self.subTest(status=status, payload=payload), \
                 patch.object(tool, "_http", return_value=(status, payload)):
                with self.assertRaises((tool.ApiError, ValueError)):
                    tool._strict_test_plans(api.list_test_plans(7))
        with self.assertRaises(ValueError):
            tool._strict_test_plans(None)

    def test_only_explicit_success_payload_proves_model_call(self):
        import model_probe
        cfg = {"model_probe_enabled": True, "online_probe_hosts": {"upstream.example": True}}
        for status, payload in ((400, '{}'), (422, '{}'), (429, '{}'), (200, '<html>login</html>'),
                                (200, '{"error":{"message":"failed"}}')):
            result = model_probe._probe_once(lambda *a, **k: (status, payload), "openai",
                                            "https://upstream.example", "synthetic", "gpt-6",
                                            {}, None, cfg)
            self.assertFalse(result[0])
        self.assertTrue(model_probe._probe_once(
            lambda *a, **k: (200, '{"choices":[{"message":{"content":"5"}}]}'), "openai",
            "https://upstream.example", "synthetic", "gpt-6", {}, None, cfg)[0])


if __name__ == "__main__":
    unittest.main()
