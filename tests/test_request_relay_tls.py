"""Real local TLS/ClientHello/CONNECT/WS proof; never uses an external host."""
import asyncio
import base64
import contextlib
import datetime
import gzip
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path

try:
    import aiohttp
    import curl_cffi
    from aiohttp import web
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from relay.transport import TLSAdapter
except ImportError:
    curl_cffi = None

import request_relay as relay


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hello_ciphers(record):
    """Return actual ClientHello cipher IDs, including GREASE, from TLS wire."""
    if record[:1] != b"\x16" or record[5:6] != b"\x01":
        raise AssertionError("not_client_hello")
    body = record[9:]
    index = 34
    index += 1 + body[index]
    length = int.from_bytes(body[index:index + 2], "big")
    index += 2
    return [int.from_bytes(body[x:x + 2], "big") for x in range(index, index + length, 2)]


@unittest.skipIf(curl_cffi is None, "optional curl_cffi>=0.17 and local TLS fixture dependencies are unavailable")
class FixedTLSAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
        self.cert = root / "fixture-ca.pem"
        self.key = root / "fixture-key.pem"
        self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        self.key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
        self.hits, self.hellos, self.proxy_hits = [], [], []
        self.capture_tasks = set()
        self.release = asyncio.Event()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.upstream)
        self.tls_runner = web.AppRunner(app, access_log=None)
        await self.tls_runner.setup()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.cert, self.key)
        site = web.TCPSite(self.tls_runner, "127.0.0.1", 0, ssl_context=context)
        await site.start()
        self.tls_port = site._server.sockets[0].getsockname()[1]
        self.capture = await asyncio.start_server(self.capture_tls, "127.0.0.1", 0)
        self.capture_port = self.capture.sockets[0].getsockname()[1]
        self.proxy = await asyncio.start_server(self.connect_proxy, "127.0.0.1", 0)
        self.proxy_port = self.proxy.sockets[0].getsockname()[1]
        self.target = "https://localhost:" + str(self.capture_port)
        self.raw = {"claude-api-key": [{"api-key": "fixture-original-key", "base-url": self.target, "models": [{"name": "claude-test"}],
            "proxy-url": "http://fixture-user:fixture-pass@127.0.0.1:" + str(self.proxy_port),
            "request-relay-tls": {"impersonate": "chrome136", "ca-bundle": str(self.cert)}}]}
        self.record = {"section": "claude-api-key", "source_section": "claude-api-key", "group": "claude", "base_url": self.target, "api_key": "fixture-original-key"}
        self.bundle = relay.compile_bundle([self.record], self.raw, "fixture", transport_port=free_port())
        self.transport = next(iter(self.bundle["transports"].values()))
        self.adapter = TLSAdapter(self.bundle, timeout=4)
        await self.adapter.start()
        self.client = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=8))

    async def asyncTearDown(self):
        self.release.set()
        await self.client.close()
        await self.adapter.close()
        self.capture.close()
        self.proxy.close()
        await self.capture.wait_closed()
        await self.proxy.wait_closed()
        for task in tuple(self.capture_tasks):
            task.cancel()
        await asyncio.gather(*tuple(self.capture_tasks), return_exceptions=True)
        await self.tls_runner.cleanup()
        self.tmp.cleanup()

    async def pump(self, source, destination):
        try:
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            destination.close()

    async def capture_tls(self, reader, writer):
        task = asyncio.current_task()
        self.capture_tasks.add(task)
        try:
            header = await reader.readexactly(5)
            payload = await reader.readexactly(int.from_bytes(header[3:5], "big"))
            self.hellos.append(hello_ciphers(header + payload))
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.tls_port)
            upstream_writer.write(header + payload)
            await upstream_writer.drain()
            await asyncio.gather(self.pump(reader, upstream_writer), self.pump(upstream_reader, writer))
        except (OSError, asyncio.IncompleteReadError):
            writer.close()
        finally:
            self.capture_tasks.discard(task)

    async def connect_proxy(self, reader, writer):
        task = asyncio.current_task()
        self.capture_tasks.add(task)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            self.proxy_hits.append(request)
            self.assertIn(b"CONNECT localhost:" + str(self.capture_port).encode() + b" HTTP/1.1", request)
            self.assertIn(b"Proxy-Authorization: Basic " + base64.b64encode(b"fixture-user:fixture-pass"), request)
            upstream_reader, upstream_writer = await asyncio.open_connection("127.0.0.1", self.capture_port)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(self.pump(reader, upstream_writer), self.pump(upstream_reader, writer))
        except (OSError, asyncio.IncompleteReadError):
            writer.close()
        finally:
            self.capture_tasks.discard(task)

    async def upstream(self, request):
        self.hits.append({"path": request.path, "host": request.host, "auth": request.headers.get("Authorization")})
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await ws.send_str(message.data)
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await ws.send_bytes(message.data)
            return ws
        body = await request.read()
        if request.path.endswith("/chat/completions"):
            return web.json_response({"id": "fixture-chat", "object": "chat.completion", "created": 1, "model": "gpt-test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        if b"redirect" in body:
            return web.Response(status=302, headers={"Location": "https://redirect-does-not-exist.invalid/steal"})
        if b"gzip" in body:
            if b"limit" in body:
                return web.Response(body=gzip.compress(b"x" * (2 * 1024 * 1024)), headers={"Content-Type": "text/event-stream", "Content-Encoding": "gzip"})
            return web.Response(body=gzip.compress(b'data: {"decoded":true}\n\ndata: [DONE]\n\n'), headers={"Content-Type": "text/event-stream", "Content-Encoding": "gzip"})
        if b"stream" in body:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"literal":"keep"}\n\n')
            await self.release.wait()
            with contextlib.suppress(ConnectionError):
                await response.write(b'data: [DONE]\n\n')
                await response.write_eof()
            return response
        return web.json_response({"decrypted": True, "body": body.decode(), "transport_header_leaked": "X-Relay-Transport-Token" in request.headers})

    def url(self, path="/v1/messages"):
        return "http://127.0.0.1:" + str(self.bundle["transport_port"]) + "/t/" + self.transport["id"] + path

    def headers(self):
        return {"X-Relay-Transport-Token": self.transport["token"], "Authorization": "Bearer fixture-original-key", "Host": "untrusted-attacker.invalid"}

    async def test_actual_clienthello_preset_differs_and_fixed_target_connect(self):
        standard = ssl.create_default_context(cafile=str(self.cert))
        async with self.client.post(self.target + "/v1/messages", ssl=standard, data=b"standard") as response:
            self.assertEqual(response.status, 200)
        python_hello = self.hellos[-1]
        async with self.client.post(self.url(), headers=self.headers(), data=b"preset") as response:
            self.assertEqual(response.status, 200)
            value = await response.json()
        curl_hello = self.hellos[-1]
        self.assertNotEqual(python_hello, curl_hello)
        self.assertTrue(any((cipher & 0x0F0F) == 0x0A0A for cipher in curl_hello))
        self.assertTrue(value["decrypted"])
        self.assertFalse(value["transport_header_leaked"])
        self.assertEqual(self.hits[-1]["host"], "localhost:" + str(self.capture_port))
        self.assertEqual(self.hits[-1]["auth"], "Bearer fixture-original-key")
        self.assertEqual(len(self.proxy_hits), 1)

    async def test_stream_cancellation_and_redirect_rejection(self):
        response = await self.client.post(self.url(), headers=self.headers(), data=b"stream")
        self.assertEqual(response.status, 200)
        self.assertIn(b"keep", await response.content.read(40))
        response.close()
        await asyncio.sleep(.1)
        self.assertTrue(all(not multi._curl2future for multi in self.adapter.multis.values()))
        self.release.set()
        before = len(self.hits)
        async with self.client.post(self.url(), headers=self.headers(), data=b"redirect") as blocked:
            self.assertEqual(blocked.status, 400)
            self.assertEqual((await blocked.json())["error"], "tls_transport_redirect_blocked")
        self.assertEqual(len(self.hits), before + 1)

    async def test_wss_text_and_binary_through_real_tls_preset(self):
        async with self.client.ws_connect(self.url("/v1/responses"), headers=self.headers()) as ws:
            await ws.send_str('{"literal":"unchanged"}')
            self.assertEqual((await ws.receive()).data, '{"literal":"unchanged"}')
            await ws.send_bytes(b"binary-fixture")
            self.assertEqual((await ws.receive()).data, b"binary-fixture")
        self.assertTrue(self.proxy_hits)

    async def test_compressed_sse_is_decoded_with_correct_headers(self):
        async with self.client.post(self.url(), headers=self.headers(), data=b"gzip") as response:
            self.assertEqual(response.status, 200)
            self.assertNotIn("Content-Encoding", response.headers)
            self.assertEqual(await response.read(), b'data: {"decoded":true}\n\ndata: [DONE]\n\n')

    async def test_pool_opaque_token_restores_explicit_authorization_priority(self):
        await self.adapter.close()
        raw = {"openai-compatibility": [{"name": "fixture", "base-url": self.target, "models": [{"name": "gpt-test"}],
            "headers": {"Authorization": "Bearer configured-override"},
            "api-key-entries": [{"api-key": "fixture-one", "weight": 3}, {"api-key": "fixture-two", "weight": 1}]}]}
        records = [{"section": "openai-compatibility", "source_section": "openai-compatibility", "group": "openai", "base_url": self.target, "api_key": key,
            "_relay_tls": {"impersonate": "chrome136", "ca-bundle": str(self.cert)}} for key in ("fixture-one", "fixture-two")]
        bundle = relay.compile_bundle(records, raw, "fixture", transport_port=free_port(), weighted_bucket=True)
        self.adapter = TLSAdapter(bundle, timeout=4)
        await self.adapter.start()
        endpoint = next(value for value in bundle["transports"].values() if value.get("members"))
        member = bundle["transports"][endpoint["members"][0]]
        url = "http://127.0.0.1:" + str(bundle["transport_port"]) + "/t/" + endpoint["id"] + "/v1/chat/completions"
        async with self.client.post(url, headers={"Authorization": "Bearer " + member["token"]}, data=b"fixture") as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.hits[-1]["auth"], "Bearer configured-override")
        before = len(self.hits)
        async with self.client.post(url, headers={"Authorization": "Bearer invalid-bucket-token"}, data=b"fixture") as response:
            self.assertEqual(response.status, 401)
        self.assertEqual(len(self.hits), before)

    async def test_gzip_output_limit_aborts_instead_of_normal_truncated_eof(self):
        self.adapter.max_response_bytes = 1024 * 1024
        response = await self.client.post(self.url(), headers=self.headers(), data=b"gzip-limit")
        try:
            self.assertEqual(response.status, 200)
            with self.assertRaises(aiohttp.ClientPayloadError):
                await response.read()
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
