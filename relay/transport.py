"""Optional fixed-target TLS transport, used only after the real CPA consumer.

Targets, proxies, CA bundles and presets come from private compiled state. HTTP
requests cannot select them. libcurl pause/resume gives bounded streaming and
its multi handle is removed immediately when the downstream request is canceled.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import struct
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import RelayError

MAX_BODY = 64 * 1024 * 1024
MAX_HEADERS = 65536
QUEUE_CHUNKS = 8
BLOCKED = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "proxy-authorization", "proxy-authenticate", "upgrade", "te", "trailer", "x-relay-transport-token", "x-relay-exact-key", "x-relay-generation", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "accept-encoding", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions"}
PROTOCOL = re.compile(r"^/(?:v1/(?:messages(?:/count_tokens)?|chat/completions|responses(?:/compact)?)|responses(?:/compact)?|chat/completions|v1beta/(?:interactions|models/[^?]+:(?:generateContent|streamGenerateContent|countTokens)))$")


def transport_target(transport, tail):
    split = urlsplit(tail)
    decoded = unquote(split.path)
    if split.scheme or split.netloc or split.fragment or decoded != split.path or "\\" in decoded or any(x in (".", "..") for x in decoded.split("/")) or not PROTOCOL.fullmatch(decoded):
        raise RelayError("invalid_transport_protocol_path")
    # No URL joining: a leading slash or user path can never replace the origin.
    return transport["target"].rstrip("/") + split.path + ("?" + split.query if split.query else "")


class TLSAdapter:
    def __init__(self, bundle, *, timeout=600.0):
        self.bundle = bundle
        self.timeout = float(timeout)
        self.max_response_bytes = int(os.environ.get("REQUEST_RELAY_MAX_RESPONSE_BYTES", str(MAX_BODY)))
        if not 1024 * 1024 <= self.max_response_bytes <= 512 * 1024 * 1024:
            raise RelayError("response_limit_must_be_1_to_512_mib")
        self.multi = None
        self.ws_session = None
        self.multis = {}
        self.ws_sessions = {}
        self.runner = None
        self.active = set()

    async def start(self):
        import curl_cffi
        from curl_cffi import AsyncCurl, CurlOpt
        from curl_cffi.requests import AsyncSession
        from aiohttp import web
        version = tuple(int(x) for x in curl_cffi.__version__.split(".")[:3])
        if not (0, 17, 0) <= version < (0, 18, 0):
            raise RelayError("tls_transport_requires_curl_cffi_0_17")
        for item in self.bundle["transports"].values():
            if item.get("ca_bundle") and not Path(item["ca_bundle"]).is_file():
                raise RelayError("transport_ca_bundle_unreadable")
        app = web.Application(client_max_size=MAX_BODY)
        app.router.add_route("*", "/t/{route:[0-9a-f]{40}}/{tail:.*}", self.forward)
        self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.bundle["transport_port"]).start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
        for session in self.ws_sessions.values():
            await session.close()
        for multi in self.multis.values():
            await multi.close()

    def http_multi(self, transport):
        from curl_cffi import AsyncCurl
        key = transport["id"]
        if key not in self.multis:
            self.multis[key] = AsyncCurl()
        return self.multis[key]

    def websocket_session(self, transport):
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession
        key = transport["id"]
        if key not in self.ws_sessions:
            self.ws_sessions[key] = AsyncSession(trust_env=False, retry=0, allow_redirects=False, default_headers=False,
                discard_cookies=True, max_clients=8, curl_options={CurlOpt.NOPROXY: b""})
        return self.ws_sessions[key]

    async def forward(self, request):
        import secrets
        from aiohttp import web
        transport = self.bundle["transports"].get(request.match_info.get("route", ""))
        endpoint_id = transport["id"] if transport else ""
        pool = bool(transport and transport.get("auth_mode") == "pool_bearer")
        if pool:
            tokens = request.headers.getall("Authorization", [])
            provided = tokens[0][7:] if len(tokens) == 1 and tokens[0].startswith("Bearer ") else ""
            selected = None
            for member in transport["members"]:
                candidate = self.bundle["transports"][member]
                if secrets.compare_digest(provided, candidate["token"]):
                    selected = candidate
            transport = dict(selected, pool_bearer=True) if selected else None
        else:
            tokens = request.headers.getall("X-Relay-Transport-Token", [])
            if not transport or len(tokens) != 1 or not secrets.compare_digest(tokens[0], transport["token"]):
                transport = None
        if not transport:
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            marker = "/t/" + endpoint_id
            if not request.raw_path.startswith(marker + "/"):
                raise RelayError("invalid_transport_path")
            target = transport_target(transport, request.raw_path[len(marker):])
            if request.headers.get("Upgrade", "").lower() == "websocket":
                if request.method != "GET":
                    raise RelayError("invalid_transport_websocket_method")
                return await self.websocket(request, transport, target)
            if request.method != "POST":
                raise RelayError("invalid_transport_method")
            body = await request.read()
            return await self.http(request, transport, target, body)
        except RelayError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "transport_body_too_large"}, status=413)
        except asyncio.CancelledError:
            raise
        except Exception:
            # libcurl exceptions include target URLs and proxy credentials.
            return web.json_response({"error": "tls_transport_failure"}, status=502)

    def headers(self, request, transport):
        blocked = set(BLOCKED)
        for connection in request.headers.getall("Connection", []):
            blocked.update(x.strip().lower() for x in connection.split(","))
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in blocked]
        if transport.get("pool_bearer"):
            configured = {str(k).lower(): (str(k), str(v)) for k, v in transport["configured_headers"].items()}
            # CPA normally sets Bearer key, then applies explicit configured
            # headers. Reproduce that order after consuming the opaque key.
            headers = [(k, v) for k, v in headers if k.lower() != "authorization" and k.lower() not in configured]
            if "authorization" not in configured:
                headers.append(("Authorization", "Bearer " + transport["upstream_api_key"]))
            headers += [(k, v) for name, (k, v) in configured.items() if name not in BLOCKED]
        return headers + [("Host", urlsplit(transport["target"]).netloc), ("Accept-Encoding", "identity")]

    async def http(self, request, transport, target, body):
        from aiohttp import web
        from curl_cffi import Curl, CurlOpt
        from curl_cffi.curl import CURL_WRITEFUNC_PAUSE, CURL_WRITEFUNC_ERROR, CURLPAUSE_CONT
        queue = asyncio.Queue(maxsize=QUEUE_CHUNKS)
        headers_ready = asyncio.Event()
        status, response_headers, header_size = 0, [], 0
        curl = Curl(cacert=transport.get("ca_bundle") or "")
        multi = self.http_multi(transport)
        paused = False
        decoded_bytes = 0

        def on_header(line):
            nonlocal status, response_headers, header_size
            header_size += len(line)
            if header_size > MAX_HEADERS:
                return CURL_WRITEFUNC_ERROR
            if line.startswith(b"HTTP/"):
                status = int(line.split(b" ", 2)[1])
                response_headers = []
            elif line in (b"\r\n", b"\n") and status >= 200:
                headers_ready.set()
            elif b":" in line:
                key, value = line.split(b":", 1)
                if key.decode("latin1").lower() not in BLOCKED | {"content-encoding"}:
                    response_headers.append((key.decode("latin1"), value.decode("latin1").strip()))
            return len(line)

        def on_data(data):
            nonlocal paused, decoded_bytes
            if queue.full():
                paused = True
                return CURL_WRITEFUNC_PAUSE
            decoded_bytes += len(data)
            if decoded_bytes > self.max_response_bytes:
                return CURL_WRITEFUNC_ERROR
            queue.put_nowait(bytes(data))
            return len(data)

        try:
            if transport["impersonate"] and curl.impersonate(transport["impersonate"], default_headers=False):
                raise RelayError("unsupported_transport_tls_preset")
            for key, value in ((CurlOpt.URL, target.encode()), (CurlOpt.CUSTOMREQUEST, b"POST"), (CurlOpt.POSTFIELDS, body),
                (CurlOpt.POSTFIELDSIZE, len(body)), (CurlOpt.HTTPHEADER, [(k + ": " + v).encode("latin1") for k, v in self.headers(request, transport)]),
                (CurlOpt.FOLLOWLOCATION, 0), (CurlOpt.MAXREDIRS, 0), (CurlOpt.PROXY, ("" if transport["proxy"] in ("", "direct") else transport["proxy"]).encode()),
                (CurlOpt.NOPROXY, b""),
                (CurlOpt.ACCEPT_ENCODING, b""),
                (CurlOpt.SSL_VERIFYPEER, 1), (CurlOpt.SSL_VERIFYHOST, 2), (CurlOpt.TIMEOUT_MS, int(self.timeout * 1000)),
                (CurlOpt.CONNECTTIMEOUT_MS, 15000), (CurlOpt.SUPPRESS_CONNECT_HEADERS, 1), (CurlOpt.WRITEFUNCTION, on_data), (CurlOpt.HEADERFUNCTION, on_header)):
                curl.setopt(key, value)
            future = multi.add_handle(curl)
            future.add_done_callback(lambda _: headers_ready.set())
            await headers_ready.wait()
            if not status:
                await future
                raise RelayError("tls_transport_missing_response")
            if 300 <= status < 400:
                raise RelayError("tls_transport_redirect_blocked")
            response = web.StreamResponse(status=status, headers=response_headers)
            await response.prepare(request)
            try:
                while not future.done() or not queue.empty():
                    if queue.empty():
                        getter = asyncio.create_task(queue.get())
                        try:
                            done, _ = await asyncio.wait((getter, future), return_when=asyncio.FIRST_COMPLETED)
                            if getter not in done:
                                continue
                            data = getter.result()
                        finally:
                            if not getter.done():
                                getter.cancel()
                            await asyncio.gather(getter, return_exceptions=True)
                    else:
                        data = queue.get_nowait()
                    await response.write(data)
                    if paused:
                        paused = False
                        curl.pause(CURLPAUSE_CONT)
                await future
                await response.write_eof()
                return response
            except BaseException:
                response.force_close()
                if request.transport:
                    request.transport.close()
                raise
        finally:
            multi.remove_handle(curl)
            curl.close()

    async def websocket(self, request, transport, target):
        import aiohttp
        from aiohttp import web
        from curl_cffi import CurlWsFlag
        upstream = await self.websocket_session(transport).ws_connect(target.replace("https://", "wss://", 1), headers=self.headers(request, transport),
            impersonate=transport["impersonate"], proxy="" if transport["proxy"] in ("", "direct") else transport["proxy"],
            verify=transport.get("ca_bundle") or True, allow_redirects=False, default_headers=False,
            timeout=self.timeout, max_message_size=MAX_BODY, recv_queue_size=8, send_queue_size=8, autoclose=False)
        downstream = web.WebSocketResponse(autoping=False, autoclose=False, compress=False, max_msg_size=MAX_BODY)
        try:
            await downstream.prepare(request)
        except BaseException:
            await upstream.close()
            raise

        async def to_upstream():
            async for message in downstream:
                mapping = {aiohttp.WSMsgType.TEXT: CurlWsFlag.TEXT, aiohttp.WSMsgType.BINARY: CurlWsFlag.BINARY, aiohttp.WSMsgType.PING: CurlWsFlag.PING, aiohttp.WSMsgType.PONG: CurlWsFlag.PONG}
                if message.type in mapping:
                    await upstream.send(message.data, mapping[message.type], timeout=self.timeout)
                    await upstream.flush()
                elif message.type == aiohttp.WSMsgType.CLOSE:
                    await upstream.close(code=message.data or 1000, message=message.extra)
                    return

        async def to_downstream():
            while True:
                data, flags = await upstream.recv(timeout=self.timeout)
                if flags & CurlWsFlag.CLOSE:
                    code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else 1000
                    await downstream.close(code=code, message=data[2:])
                    return
                if flags & CurlWsFlag.TEXT:
                    await downstream.send_str(data.decode("utf8"))
                elif flags & CurlWsFlag.BINARY:
                    await downstream.send_bytes(data)
                elif flags & CurlWsFlag.PING:
                    await downstream.ping(data)
                elif flags & CurlWsFlag.PONG:
                    await downstream.pong(data)
        tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_downstream())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await downstream.close()
        return downstream
