"""aiohttp local loopback proof. Optional dependency; no real upstream request."""
import asyncio
import gzip
import json
import unittest
from unittest.mock import patch

try:
    import aiohttp
    from aiohttp import web
    from relay.server import RelayRuntime, create_app
except ImportError:
    aiohttp = None

import request_relay as relay


@unittest.skipIf(aiohttp is None, "optional aiohttp relay runtime is not installed")
class RelayRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hits = []
        self.forwarded_models = []
        self.raw = {"codex-api-key": [{"api-key": "fixture-key", "base-url": "http://127.0.0.1:9", "models": [{"name": "gpt-test", "alias": "gpt-test"}]}]}
        self.record = {"section": "codex-api-key", "source_section": "codex-api-key", "group": "codex", "base_url": "http://127.0.0.1:9", "api_key": "fixture-key"}
        upstream = web.Application()
        upstream.router.add_route("*", "/{tail:.*}", self.fixture_handler)
        self.up_runner = web.AppRunner(upstream)
        await self.up_runner.setup()
        site = web.TCPSite(self.up_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.bundle = relay.compile_bundle([self.record], self.raw, "fixture", consumer_port=port)
        self.route = next(iter(self.bundle["routes"].values()))
        self.runtime = RelayRuntime("unused-test-dir", "unused", request_timeout=3)
        self.runtime.bundle = self.bundle
        self.runtime.accepting = True
        self.runtime.child = type("Child", (), {"returncode": None})()
        self.runtime.client = aiohttp.ClientSession(trust_env=False, auto_decompress=False)
        self.front_runner = web.AppRunner(create_app(self.runtime), access_log=None, handler_cancellation=True)
        await self.front_runner.setup()
        front_site = web.TCPSite(self.front_runner, "127.0.0.1", 0)
        await front_site.start()
        self.base = "http://127.0.0.1:" + str(front_site._server.sockets[0].getsockname()[1])
        self.client = aiohttp.ClientSession(trust_env=False)

    async def asyncTearDown(self):
        await self.client.close()
        await self.front_runner.cleanup()
        await self.runtime.client.close()
        await self.up_runner.cleanup()

    async def fixture_handler(self, request):
        self.hits.append(request.path)
        self.assertEqual(request.headers.get("Authorization"), "Bearer " + self.bundle["state"]["consumer_token"])
        self.assertNotIn("X-Relay-Exact-Key", request.headers)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    value = json.loads(msg.data)
                    if "model" in value:
                        self.assertEqual(value["model"], self.route["prefix"] + "/gpt-test")
                    await ws.send_json({"type": "response.created", "response": {"model": self.route["prefix"] + "/gpt-test"}, "literal": self.route["prefix"] + "/do-not-replace"})
            return ws
        body = await request.json()
        self.forwarded_models.append(body["model"])
        expected = {self.route["prefix"] + "/gpt-test", self.route.get("diagnostic_prefix", self.route["prefix"]) + "/gpt-test"}
        self.assertIn(body["model"], expected)
        if body.get("fixture_compression"):
            value = json.dumps({"model": body["model"], "literal": body["model"]}).encode()
            if body.get("stream"):
                value = b'data: ' + value + b'\n\ndata: [DONE]\n\n'
            packed = gzip.compress(value) if body["fixture_compression"] == "gzip" else b"broken-gzip-fixture"
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream" if body.get("stream") else "application/json", "Content-Encoding": "gzip"})
            await response.prepare(request)
            await response.write(packed[:7])
            await response.write(packed[7:])
            await response.write_eof()
            return response
        if body.get("stream"):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            event = ('event: response.created\r\ndata: ' + json.dumps({"response": {"model": body["model"]}, "literal": body["model"]}) + '\r\n\r\n').encode()
            for part in (event[:17], event[17:43], event[43:]):
                await response.write(part)
            await response.write(b'data: [DONE]\n\n')
            await response.write_eof()
            return response
        return web.json_response({"model": body["model"], "tools": body.get("tools"), "messages": body.get("messages")})

    def path(self, suffix):
        return self.base + "/r/" + self.route["id"] + suffix

    def headers(self):
        return {"Authorization": "Bearer " + self.route["token"]}

    async def test_auth_path_and_startup_are_zero_inference(self):
        await asyncio.sleep(0)
        self.assertEqual(self.hits, [])
        async with self.client.post(self.path("/v1/responses"), json={"model": "gpt-test"}) as response:
            self.assertEqual(response.status, 401)
        async with self.client.post(self.path("/v0/management/config"), headers=self.headers(), json={"model": "gpt-test"}) as response:
            self.assertEqual(response.status, 400)
        self.assertEqual(self.hits, [])

    async def test_json_preserves_tools_and_messages(self):
        payload = {"model": "gpt-test", "tools": [{"name": "keep", "parameters": {"type": "object"}}], "messages": [{"role": "user", "content": "keep"}]}
        async with self.client.post(self.path("/v1/responses"), headers=self.headers(), json=payload) as response:
            value = await response.json()
        self.assertEqual(value, payload)

    async def test_chunked_sse_keeps_events_and_terminal(self):
        async with self.client.post(self.path("/v1/responses"), headers=self.headers(), json={"model": "gpt-test", "stream": True}) as response:
            body = await response.read()
        self.assertIn(b'event: response.created\r\n', body)
        self.assertIn(b'"model":"gpt-test"', body)
        self.assertIn((self.route["prefix"] + "/gpt-test").encode(), body)
        self.assertTrue(body.endswith(b'data: [DONE]\n\n'))

    async def test_websocket_route_and_continuation(self):
        async with self.client.ws_connect(self.path("/v1/responses"), headers=self.headers()) as ws:
            await ws.send_json({"type": "response.create", "model": "gpt-test", "input": "fixture"})
            first = await ws.receive_json()
            self.assertEqual(first["response"]["model"], "gpt-test")
            await ws.send_json({"type": "response.create", "previous_response_id": "fixture-previous"})
            second = await ws.receive_json()
            self.assertEqual(second["response"]["model"], "gpt-test")

    async def test_unready_blocks_authenticated_business_request(self):
        self.runtime.accepting = False
        async with self.client.post(self.path("/v1/responses"), headers=self.headers(), json={"model": "gpt-test"}) as response:
            self.assertEqual(response.status, 503)
        self.assertEqual(self.hits, [])

    async def test_fragmented_gzip_json_and_sse_decoding(self):
        for stream in (False, True):
            async with self.client.post(self.path("/v1/responses"), headers=self.headers(), json={"model": "gpt-test", "stream": stream, "fixture_compression": "gzip"}) as response:
                data = await response.read()
                self.assertEqual(response.status, 200)
                self.assertNotIn("Content-Encoding", response.headers)
                self.assertIn(b'"model":"gpt-test"', data)
                self.assertIn((self.route["prefix"] + "/gpt-test").encode(), data)
                if stream:
                    self.assertTrue(data.endswith(b'data: [DONE]\n\n'))

    async def test_bad_gzip_json_fails_closed(self):
        async with self.client.post(self.path("/v1/responses"), headers=self.headers(), json={"model": "gpt-test", "fixture_compression": "broken"}) as response:
            self.assertEqual(response.status, 400)
            self.assertEqual((await response.json())["error"], "invalid_consumer_compression")

    async def test_exact_selector_is_route_bound_and_not_forwarded(self):
        self.route["exact_prefix"] = self.route["prefix"]
        self.route["prefix"] = "pool-synthetic-prefix"
        headers = dict(self.headers(), **{"X-Relay-Exact-Key": "true"})
        pool = self.route["prefix"]
        self.runtime.consumer_guard_proof = {"feature_id": "cpa2sub2api-diagnostic-no-redirect-v1"}
        with patch("relay.server.diagnostic_policy_allowed", return_value=True):
            async with self.client.post(self.path("/v1/responses"), headers=headers, json={"model": "gpt-test"}) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual((await response.json())["model"], "gpt-test")
        self.assertEqual(self.forwarded_models, [self.route["diagnostic_prefix"] + "/gpt-test"])
        self.assertEqual(self.route["prefix"], pool)


if __name__ == "__main__":
    unittest.main()
