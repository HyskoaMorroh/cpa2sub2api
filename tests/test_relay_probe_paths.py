"""Product diagnostics use the relay's protocol roots, with no external traffic."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import model_probe
import request_relay
import tool

try:
    import aiohttp
    from aiohttp import web
    from relay.server import RelayRuntime, create_app
except ImportError:
    aiohttp = None


class RelayProbePathTests(unittest.TestCase):
    def test_relay_probe_protocols_have_one_version_segment(self):
        cases = (("openai", "openai-compatibility", "/v1/chat/completions"),
                 ("openai", "codex-api-key", "/v1/responses"),
                 ("anthropic", "claude-api-key", "/v1/messages"),
                 ("gemini", "gemini-api-key", "/v1beta/models/gemini-9-pro:generateContent"))
        for platform, section, suffix in cases:
            for version in ("", "/v1", "/v1beta"):
                with self.subTest(section=section, version=version):
                    calls = []
                    def http(method, url, headers, body, **kwargs):
                        calls.append((url, headers))
                        return 200, '{"type":"message","content":[],"choices":[{}],"candidates":[{}]}'
                    policy = {"model_probe_enabled": True, "online_probe_hosts": {"source.example": True},
                              "request_relay": {"generation": "synthetic-generation"}}
                    model_probe._probe_once(http, platform, "https://source.example/original/v1", "source-key",
                                            "gemini-9-pro", {}, None, policy, section,
                                            "http://relay.invalid/r/route-id" + version, "route-token")
                    self.assertEqual(urlsplit(calls[0][0]).path, "/r/route-id" + suffix)
                    self.assertEqual(calls[0][1]["X-Relay-Exact-Key"], "true")

    def test_direct_request_paths_keep_the_declared_base(self):
        self.assertEqual(model_probe._build_request("openai", "https://source.example/v1", "key", "gpt-6",
                                                   source_section="openai-compatibility")[1],
                         "https://source.example/v1/chat/completions")
        self.assertEqual(model_probe._build_request("openai", "https://source.example/custom", "key", "gpt-6",
                                                   source_section="codex-api-key")[1],
                         "https://source.example/custom/responses")


@unittest.skipIf(aiohttp is None, "optional aiohttp relay runtime is not installed")
class RelayExactRecoveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_recovery_through_real_front_reaches_exact_consumer_and_denial_is_zero_http(self):
        hits = []
        holder = {}
        async def consumer(request):
            body = await request.json()
            hits.append((request.path, body["model"], request.headers.get("Authorization")))
            self.assertEqual(request.path, "/v1/chat/completions")
            route = holder["route"]
            self.assertEqual(body["model"], route.get("diagnostic_prefix", route["exact_prefix"]) + "/gpt-6")
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + holder["bundle"]["state"]["consumer_token"])
            self.assertNotIn("X-Relay-Exact-Key", request.headers)
            return web.json_response({"model": body["model"], "choices": [{"message": {"content": "5"}}]})
        upstream = web.Application()
        upstream.router.add_post("/{path:.*}", consumer)
        upstream_runner = web.AppRunner(upstream, access_log=None)
        await upstream_runner.setup()
        upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
        await upstream_site.start()
        port = upstream_site._server.sockets[0].getsockname()[1]
        raw = {"openai-compatibility": [{"name": "fixture", "base-url": "https://source.example/v1",
                 "api-key-entries": [{"api-key": "synthetic-source-key"}], "models": [{"name": "gpt-6"}]}]}
        records, _ = tool.collect(raw)
        for record in records:
            record["_source_scope"] = "fixture"
        tool.assign_names(records)
        tool.remap_priority(records)
        with tempfile.TemporaryDirectory() as directory:
            bundle = request_relay.export_bundle(records, raw, "fixture", directory, consumer_port=port)
            route = next(iter(bundle["routes"].values()))
            holder.update(bundle=bundle, route=route)
            runtime = RelayRuntime(directory, "unused-fixture", request_timeout=3)
            runtime.bundle, runtime.accepting = bundle, True
            runtime.consumer_guard_proof = {
                "feature_id": "cpa2sub2api-diagnostic-no-redirect-v1",
                "source_commit": "91e0dee827abf476f9f86246f68c500a112f7155",
                "binary_sha256": "a" * 64, "automatic_output_cap": 16,
                "automatic_output_header": "X-CPA2SUB2API-Auto-Output-Cap"}
            runtime.child = type("Child", (), {"returncode": None})()
            runtime.client = aiohttp.ClientSession(trust_env=False, auto_decompress=False)
            front_runner = web.AppRunner(create_app(runtime), access_log=None)
            await front_runner.setup()
            front_site = web.TCPSite(front_runner, "127.0.0.1", 0)
            await front_site.start()
            base = "http://127.0.0.1:" + str(front_site._server.sockets[0].getsockname()[1])
            reference = {"enabled": True, "source_scope": "fixture", "state_dir": directory, "base_url": base,
                         "generation": bundle["generation"], "weighted_bucket": False}
            cfg = {"source_scope": "fixture", "model_probe_enabled": True,
                   "online_probe_hosts": {"source.example": True}, "request_relay": reference,
                   "explicit_recovery_account_ids": [7], "revive_proven_inactive": True}
            account = tool.to_account(records[0], dict(tool.DEFAULTS, **cfg, _request_relay_bundle=bundle),
                                      {records[0]["group"]: 1}, {}, {})
            account.update(id=7, status="inactive", schedulable=False)
            class Admin:
                def _call(self, method, path):
                    return copy.deepcopy(account)
            try:
                with patch.object(tool, "SETTINGS_PATH", str(Path(directory) / "unused-settings.json")), \
                     patch.object(tool, "_pick_probe_model", return_value="gpt-6"), \
                     patch.object(tool, "load_settings", return_value=cfg):
                    evidence, failures = await asyncio.to_thread(
                        tool.probe_import_recovery, Admin(), {account["name"]: 7}, cfg)
                    self.assertFalse(failures)
                    self.assertTrue(evidence[7]["success"])
                    self.assertEqual(len(hits), 1)
                    hits.clear()
                    with patch.object(tool, "_http", wraps=tool._http) as actual_http, \
                         patch.object(tool.probe_policy, "permanently_denied", side_effect=lambda host: "source.example" in host):
                        denied, _ = await asyncio.to_thread(
                            tool.probe_import_recovery, Admin(), {account["name"]: 7}, cfg)
                    self.assertEqual(denied, {})
                    self.assertEqual(hits, [])
                    actual_http.assert_not_called()
            finally:
                await front_runner.cleanup()
                await runtime.client.close()
                await upstream_runner.cleanup()


if __name__ == "__main__":
    unittest.main()
