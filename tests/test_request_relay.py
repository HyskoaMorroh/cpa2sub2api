"""Local-only relay contracts. No request in this suite targets a real upstream."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import request_relay as relay


def fixture():
    raw = {"claude-api-key": [{"api-key": "fixture-upstream-one", "base-url": "http://127.0.0.1:19090",
            "models": [{"name": "claude-test", "alias": "public-test", "thinking": {"levels": ["high"]}}],
            "cloak": {"mode": "always", "cache-user-id": True}, "rebuild-mid-system-message": True,
            "headers": {"X-Explicit": "retained"}, "connection-pool": {"enabled": True}}]}
    record = {"section": "claude-api-key", "source_section": "claude-api-key", "group": "claude",
              "base_url": "http://127.0.0.1:19090", "api_key": "fixture-upstream-one", "fp": "original-fixture-fp",
              "models_raw": raw["claude-api-key"][0]["models"], "name": "fixture-account"}
    return raw, record


class RelayConfigurationTests(unittest.TestCase):
    def test_stable_route_and_random_tokens_survive_reordering_export(self):
        raw, record = fixture()
        first = relay.compile_bundle([record], raw, "fixture-scope")
        second = relay.compile_bundle([record], raw, "fixture-scope", first["state"])
        self.assertEqual(first, second)
        self.assertNotIn(record["api_key"], next(iter(first["routes"])))
        other = dict(record, source_section="codex-api-key", section="codex-api-key", group="codex")
        raw["codex-api-key"] = [dict(raw["claude-api-key"][0])]
        mixed = relay.compile_bundle([other, record], raw, "fixture-scope", first["state"])
        self.assertEqual(first["routes"][next(iter(first["routes"]))]["token"], mixed["routes"][next(iter(first["routes"]))]["token"])
        self.assertEqual(len(mixed["routes"]), 2)

    def test_exact_entry_preserved_and_internal_prefix_is_unique(self):
        raw, record = fixture()
        b = relay.compile_bundle([record], raw, "fixture-scope")
        entry = next(e for e in b["consumer_config"]["claude-api-key"] if e["prefix"].startswith("r"))
        for field in ("cloak", "rebuild-mid-system-message", "headers", "connection-pool"):
            self.assertEqual(entry[field], raw["claude-api-key"][0][field])
        self.assertTrue(b["consumer_config"]["routing"]["force-model-prefix"])
        self.assertEqual(len(b["consumer_config"]["claude-api-key"]), 2)
        self.assertTrue(entry["prefix"].startswith("r"))

    def test_openai_provider_split_one_key_each(self):
        raw = {"openai-compatibility": [{"name": "fixture", "base-url": "http://localhost:19090/v1", "headers": {"X-A": "B"},
                "support-prompt-cache-key": True, "models": [{"name": "gpt-test", "alias": "gpt-test"}],
                "api-key-entries": [{"api-key": "fixture-one", "weight": 3}, {"api-key": "fixture-two", "proxy-url": "http://proxy:7890"}]}]}
        records = [{"section": "openai-compatibility", "source_section": "openai-compatibility", "group": "openai", "base_url": raw["openai-compatibility"][0]["base-url"], "api_key": key, "models_raw": raw["openai-compatibility"][0]["models"]} for key in ("fixture-one", "fixture-two")]
        b = relay.compile_bundle(records, raw, "scope")
        providers = b["consumer_config"]["openai-compatibility"]
        self.assertEqual(len(providers), 4)
        self.assertEqual(len({p["prefix"] for p in providers}), 4)
        self.assertTrue(all(len(p["api-key-entries"]) == 1 for p in providers))
        self.assertTrue(all(p["support-prompt-cache-key"] for p in providers))

    def test_source_lookup_missing_or_ambiguous_fails(self):
        raw, record = fixture()
        with self.assertRaises(relay.RelayError):
            relay.compile_bundle([dict(record, api_key="absent")], raw, "scope")
        raw["claude-api-key"].append(dict(raw["claude-api-key"][0], headers={"X-Other": "different"}))
        with self.assertRaises(relay.RelayError):
            relay.compile_bundle([record], raw, "scope")

    def test_all_excluded_route_is_retained_and_blocks_business(self):
        raw, record = fixture()
        raw["claude-api-key"][0]["excluded-models"] = ["*"]
        bundle = relay.compile_bundle([dict(record, _relay_models=[])], raw, "scope")
        self.assertEqual(len(bundle["routes"]), 1)
        route = next(iter(bundle["routes"].values()))
        self.assertTrue(route["blocked"])
        self.assertEqual(route["models"], [])
        self.assertEqual(bundle["consumer_config"]["claude-api-key"][0]["excluded-models"], ["*"])
        with self.assertRaises(relay.RelayError):
            relay.prepare_request(route, "/v1/messages", b'{"model":"claude-test"}')

    def test_atomic_export_detects_tampering_and_reuses_private_state(self):
        raw, record = fixture()
        with tempfile.TemporaryDirectory() as tmp:
            first = relay.export_bundle([record], raw, "scope", tmp)
            second = relay.export_bundle([record], raw, "scope", tmp)
            self.assertEqual(first["generation"], second["generation"])
            loaded = relay.load_bundle(tmp)
            self.assertEqual(loaded["generation"], first["generation"])
            with self.assertRaises(relay.RelayError):
                relay.export_bundle([record], raw, "other-scope", tmp)
            (Path(tmp) / "generations" / first["generation"] / "consumer.yaml").write_text("tampered", encoding="utf8")
            with self.assertRaises(relay.RelayError):
                relay.load_bundle(tmp)

    def test_account_adaptation_retains_model_mapping_and_original_identity(self):
        raw, record = fixture()
        bundle = relay.compile_bundle([record], raw, "scope")
        account = {"name": "unchanged", "credentials": {"api_key": record["api_key"], "base_url": record["base_url"], "model_mapping": {"public-test": "claude-test"}}, "proxy_id": 17, "priority": 2, "extra": {"enable_tls_fingerprint": True}}
        result = relay.adapt_account(account, record, bundle, "http://request-relay:8380")
        self.assertEqual(account["credentials"]["api_key"], record["api_key"])
        ordinary = account["credentials"]["model_mapping"]
        mapping = result["credentials"]["model_mapping"]
        self.assertEqual({key: mapping[key] for key in ordinary}, ordinary)
        route = next(iter(bundle["routes"].values()))
        diagnostics = {relay.diagnostic_model_marker(route, model, automatic=automatic):
                       relay.diagnostic_model_marker(route, model, automatic=automatic)
                       for model in route["models"] for automatic in (False, True)}
        diagnostics.update({('a' if automatic else 'd') + route['id'] + '-' + hashlib.sha256(model.encode()).hexdigest():
                            ('a' if automatic else 'd') + route['id'] + '-' + hashlib.sha256(model.encode()).hexdigest()
                            for model in route['models'] for automatic in (False, True)})
        self.assertEqual(set(mapping) - set(ordinary), set(diagnostics))
        self.assertEqual({key: mapping[key] for key in diagnostics}, diagnostics)
        self.assertEqual(result["priority"], 2)
        self.assertIsNone(result["proxy_id"])
        original = result["extra"]["cpa2sub2api_original_source"]
        self.assertEqual(original["base_url"], record["base_url"])
        self.assertEqual(original["key_fingerprint"], record["fp"])
        self.assertNotIn(record["api_key"], json.dumps(original))
        self.assertEqual(relay.account_original_source(result), original)
        result["extra"]["cpa2sub2api_original_source"]["base_url"] = "http://different.invalid"
        with self.assertRaises(relay.RelayError):
            relay.account_original_source(result)

    def test_weighted_bucket_is_explicit_and_keeps_each_exact_prefix(self):
        raw, first = fixture()
        second = dict(first, api_key="fixture-upstream-two")
        raw["claude-api-key"][0]["weight"] = 3
        raw["claude-api-key"].append(dict(raw["claude-api-key"][0], **{"api-key": second["api_key"], "weight": 1}))
        exact = relay.compile_bundle([first, second], raw, "scope")
        self.assertEqual(len({r["prefix"] for r in exact["routes"].values()}), 2)
        pool = relay.compile_bundle([first, second], raw, "scope", exact["state"], weighted_bucket=True)
        self.assertEqual(len({r["prefix"] for r in pool["routes"].values()}), 1)
        self.assertEqual(len({r["exact_prefix"] for r in pool["routes"].values()}), 2)
        self.assertEqual(len(pool["consumer_config"]["claude-api-key"]), 6)
        self.assertTrue(all(r["routing_mode"] == "weighted_bucket" for r in pool["routes"].values()))

    def test_explicit_retry_cooldown_and_strategy_are_preserved_but_diagnostics_zero(self):
        raw, record = fixture()
        raw["routing"] = {"strategy": "fill-first", "retry": {"request-retry": 7, "max-retry-credentials": 2}, "cooldown": {"disable-cooling": False}}
        raw["claude-api-key"][0]["request-retry"] = 3
        before = copy.deepcopy(raw)
        bundle = relay.compile_bundle([record], raw, "scope")
        self.assertEqual(raw, before)
        routing = bundle["consumer_config"]["routing"]
        self.assertEqual(routing["strategy"], "fill-first")
        self.assertEqual(routing["retry"], raw["routing"]["retry"])
        self.assertEqual(routing["cooldown"], raw["routing"]["cooldown"])
        entries = bundle["consumer_config"]["claude-api-key"]
        self.assertEqual(next(e for e in entries if e["prefix"].startswith("r"))["request-retry"], 3)
        self.assertEqual(next(e for e in entries if e["prefix"].startswith("d"))["request-retry"], 0)

    def test_tls_extension_rejects_invalid_before_any_published_state(self):
        raw, record = fixture()
        record["base_url"] = "https://localhost:19090"
        raw["claude-api-key"][0]["base-url"] = record["base_url"]
        with tempfile.TemporaryDirectory() as tmp:
            for options in (None, {}, {"impersonate": "unknown"}, {"impersonate": "chrome136", "verify": False}, {"impersonate": "chrome136", "ca-bundle": "/not-a-real-fixture-ca"}):
                with self.subTest(options=options):
                    candidate = dict(record, _relay_tls=options)
                    self.assertTrue(relay.requires_relay(candidate, raw))
                    with self.assertRaises(relay.RelayError):
                        relay.export_bundle([candidate], raw, "scope", tmp)
                    self.assertFalse((Path(tmp) / "CURRENT").exists())
                    self.assertFalse((Path(tmp) / "secrets.json").exists())
        b = relay.compile_bundle([dict(record, _relay_tls={"impersonate": "chrome136"})], raw, "scope")
        self.assertEqual(len(b["transports"]), 1)
        item = b["consumer_config"]["claude-api-key"][0]
        self.assertNotIn("request-relay-tls", item)
        self.assertTrue(item["base-url"].startswith("http://127.0.0.1:8391/t/"))
        self.assertEqual(item["proxy-url"], "direct")


class RelayProtocolTests(unittest.TestCase):
    def setUp(self):
        raw, record = fixture()
        self.route = next(iter(relay.compile_bundle([record], raw, "scope")["routes"].values()))

    def test_request_changes_only_model_and_rejects_other_route(self):
        body = {"model": "claude-test", "messages": [{"role": "system", "content": "original"}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "keep", "content": "r0/prefix text"}]}], "tools": [{"name": "tool", "input_schema": {"type": "object"}}]}
        path, payload = relay.prepare_request(self.route, "/v1/messages", json.dumps(body).encode())
        transformed = json.loads(payload)
        self.assertEqual(transformed["messages"], body["messages"])
        self.assertEqual(transformed["tools"], body["tools"])
        self.assertEqual(transformed["model"], self.route["prefix"] + "/claude-test")
        with self.assertRaises(relay.RelayError):
            relay.prepare_request(self.route, path, b'{"model":"another-route/model"}')

    def test_response_changes_model_fields_only(self):
        prefix = self.route["prefix"] + "/"
        value = {"model": prefix + "claude-test", "message": {"model": prefix + "claude-test"}, "content": [{"text": prefix + "claude-test"}], "tool": {"model": prefix + "claude-test"}}
        result = json.loads(relay.restore_response(self.route, json.dumps(value).encode()))
        self.assertEqual(result["model"], "claude-test")
        self.assertEqual(result["message"]["model"], "claude-test")
        self.assertEqual(result["content"], value["content"])
        self.assertEqual(result["tool"], value["tool"])

    def test_sse_preserves_envelope_terminal_and_tool_content(self):
        prefix = self.route["prefix"] + "/"
        event = b': keepalive\r\nevent: message_start\r\nid: fixture\r\ndata: ' + json.dumps({"message": {"model": prefix + "claude-test"}, "delta": {"text": prefix + "literal"}}).encode() + b'\r\n\r\n'
        result = relay.restore_sse_event(self.route, event)
        self.assertTrue(result.startswith(b': keepalive\r\nevent: message_start\r\nid: fixture\r\ndata: '))
        self.assertIn((prefix + "literal").encode(), result)
        self.assertEqual(relay.restore_sse_event(self.route, b'data: [DONE]\n\n'), b'data: [DONE]\n\n')

    def test_path_allowlist_rejects_management_absolute_urls_and_traversal(self):
        for path in ("/v0/management/config", "http://example.test/v1/messages", "//example.test/v1/messages", "/v1/../v0/management", "/v1/%2e%2e/messages", "/v1/messages?url=http://example.test"):
            with self.subTest(path=path), self.assertRaises(relay.RelayError):
                relay.prepare_request(self.route, path, b'{"model":"claude-test"}')

    def test_auth_wrong_route_and_conflicting_credentials_denied(self):
        self.assertTrue(relay.authorized({"Authorization": "Bearer " + self.route["token"]}, self.route["token"]))
        self.assertFalse(relay.authorized({"X-Api-Key": "wrong"}, self.route["token"]))
        self.assertFalse(relay.authorized({"Authorization": "Bearer " + self.route["token"], "X-Api-Key": "wrong"}, self.route["token"]))


if __name__ == "__main__":
    unittest.main()
