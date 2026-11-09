"""A config-bound front to a supervised CPA process with a diagnostic guard.

Only local registry readiness is polled. This module never synthesizes inference
requests. aiohttp is deliberately imported only by this optional runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import RelayError, load_bundle
from .protocol import authorized, prepare_request, restore_response, restore_sse_event

LOG = logging.getLogger("cpa2sub2api.relay")
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"})
AUTH_HEADERS = frozenset({"authorization", "x-api-key", "x-goog-api-key", "cookie"})
MAX_EVENT = 8 * 1024 * 1024
MAX_BODY = 64 * 1024 * 1024
DIAGNOSTIC_AUTH_BODY_LIMIT = 64 * 1024
GUARD_FEATURE = "cpa2sub2api-diagnostic-no-redirect-v1"
GUARD_SOURCE_COMMIT = "91e0dee827abf476f9f86246f68c500a112f7155"
DIAGNOSTIC_PREFIX_ENV = "CPA2SUB2API_DIAGNOSTIC_PREFIXES"
GUARD_QUERY_ENV = "CPA2SUB2API_GUARD_FEATURE_QUERY"
AUTOMATIC_PROBE_HEADER = "X-Relay-Automatic-Probe"
AUTOMATIC_OUTPUT_HEADER = "X-CPA2SUB2API-Auto-Output-Cap"
AUTOMATIC_SKIP_CODES = frozenset({"automatic_probe_unsafe_profile", "automatic_probe_unsafe_payload",
    "automatic_probe_unsupported_protocol", "automatic_probe_wire_budget_exhausted", "automatic_probe_output_hint_invalid"})
CHILD_ENV_KEYS = frozenset({"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TZ", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"})


def diagnostic_prefixes(bundle):
    """Derive only this verified generation's exact diagnostic route namespace."""
    prefixes = []
    for route_id, route in sorted(bundle["routes"].items()):
        expected = "d" + route_id
        if (not re.fullmatch(r"[0-9a-f]{40}", route_id) or route.get("id") != route_id
                or route.get("diagnostic_prefix") != expected):
            raise RelayError("invalid_diagnostic_route_prefix")
        from probe_policy import permanently_denied
        if not route.get("blocked") and not permanently_denied((route.get("original_source") or {}).get("base_url", "")):
            prefixes.append(expected)
    if len(prefixes) != len(set(prefixes)):
        raise RelayError("duplicate_diagnostic_route_prefix")
    return prefixes


def verify_consumer_guard(consumer_binary, provenance_path=None, timeout=5):
    """Require both the image's binary digest and the executable feature reply."""
    path = Path(provenance_path) if provenance_path is not None else (
        Path(__file__).resolve().parents[1] / "third_party" / "CLIProxyAPI.PROVENANCE.json")
    try:
        provenance = json.loads(path.read_text(encoding="utf8"))
        if (not isinstance(provenance, dict) or type(provenance.get("schema_version")) is not int
                or provenance["schema_version"] != 1 or provenance.get("feature_id") != GUARD_FEATURE
                or provenance.get("upstream_commit") != GUARD_SOURCE_COMMIT
                or type(provenance.get("automatic_output_cap")) is not int
                or provenance.get("automatic_output_cap") != 16
                or provenance.get("automatic_output_header") != AUTOMATIC_OUTPUT_HEADER
                or not re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("binary_sha256", "")))):
            raise RelayError("consumer_guard_provenance_invalid")
        digest = hashlib.sha256()
        with open(consumer_binary, "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != provenance["binary_sha256"]:
            raise RelayError("consumer_guard_binary_digest_mismatch")
    except RelayError:
        raise
    except (OSError, ValueError, TypeError):
        raise RelayError("consumer_guard_provenance_unavailable") from None
    env = {key: value for key, value in os.environ.items() if key.upper() in CHILD_ENV_KEYS}
    env[GUARD_QUERY_ENV] = "1"
    try:
        response = subprocess.run([str(consumer_binary), "-h"], env=env,
            capture_output=True, text=True, encoding="utf8", timeout=timeout)
        feature = json.loads(response.stdout)
        if (response.returncode or not isinstance(feature, dict)
                or type(feature.get("automatic_output_cap")) is not int
                or feature != {"feature_id": GUARD_FEATURE,
                "source_commit": GUARD_SOURCE_COMMIT, "prefix_env": DIAGNOSTIC_PREFIX_ENV,
                "automatic_output_cap": 16, "automatic_output_header": AUTOMATIC_OUTPUT_HEADER}):
            raise RelayError("consumer_guard_feature_mismatch")
    except RelayError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise RelayError("consumer_guard_feature_unavailable") from None
    return {"feature_id": GUARD_FEATURE, "source_commit": GUARD_SOURCE_COMMIT,
            "binary_sha256": provenance["binary_sha256"],
            "automatic_output_cap": 16, "automatic_output_header": AUTOMATIC_OUTPUT_HEADER}


def resolve_diagnostic_request(route, path, body, websocket=False):
    """Resolve a path-safe native tester marker only within its authenticated route."""
    value, target = None, None
    gemini = re.match(r"/v1beta/models/([^/:?]+)", path) if route["source_section"] == "gemini-api-key" else None
    if gemini:
        model = urllib.parse.unquote(gemini.group(1))
    else:
        try:
            value = json.loads(body)
        except (ValueError, TypeError):
            return route, path, body, False
        target = value.get("session") if websocket and isinstance(value, dict) and value.get("type") == "session.update" else value
        model = target.get("model") if isinstance(target, dict) else None
    if not isinstance(model, str) or not re.match(r"^[ad][0-9a-f]{40}[-/]", model):
        return route, path, body, False
    marker = re.fullmatch(r"([ad][0-9a-f]{40})-([0-9a-f]{58}|[0-9a-f]{64})", model)
    automatic = bool(marker and marker.group(1).startswith("a"))
    expected = "a" + route["id"] if automatic else route.get("diagnostic_prefix")
    if (not marker or marker.group(1) != expected
            or automatic and (route.get("diagnostic_prefix") != "d" + route["id"]
                              or route.get("automatic_prefix", expected) != expected)):
        raise RelayError("diagnostic_model_route_mismatch")
    matched = [name for name in route["models"] if hashlib.sha256(name.encode("utf8")).hexdigest()[:len(marker.group(2))] == marker.group(2)]
    if len(matched) != 1:
        raise RelayError("diagnostic_model_not_current")
    actual = matched[0]
    if gemini:
        path = path[:gemini.start(1)] + urllib.parse.quote(actual, safe="") + path[gemini.end(1):]
    else:
        target["model"] = actual
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")
    resolved = dict(route, prefix=route["diagnostic_prefix"])
    resolved.pop("automatic_probe", None)
    if automatic:
        resolved["automatic_probe"] = True
    return resolved, path, body, True


async def _authentication_rejection(request):
    """Local diagnostic authorization failures cannot revoke an upstream key."""
    from aiohttp import web
    reserved = lambda model: isinstance(model, str) and re.match(r"^[ad][0-9a-f]{40}[-/]", model) is not None
    diagnostic = any(key.lower() in {"x-relay-exact-key", "x-relay-automatic-probe"} for key in request.headers)
    if not diagnostic:
        path = urllib.parse.urlsplit(request.raw_path).path
        match = re.search(r"/(?:v1beta|v1)/models/([^:]+)", path)
        diagnostic = bool(match and reserved(urllib.parse.unquote(match.group(1))))
    if not diagnostic and request.method == "POST":
        length = getattr(request, "content_length", None)
        content_type = getattr(request, "content_type", "")
        encoding = request.headers.get("Content-Encoding", "identity").lower()
        if (type(length) is int and 0 < length <= DIAGNOSTIC_AUTH_BODY_LIMIT
                and content_type == "application/json" and encoding in ("", "identity")):
            try:
                raw = await asyncio.wait_for(request.read(), timeout=1)
                if len(raw) <= DIAGNOSTIC_AUTH_BODY_LIMIT:
                    value = json.loads(raw)
                    diagnostic = isinstance(value, dict) and reserved(value.get("model"))
            except (ValueError, UnicodeDecodeError, RecursionError, asyncio.TimeoutError,
                    OSError, asyncio.IncompleteReadError, web.HTTPRequestEntityTooLarge):
                pass
    if diagnostic:
        return web.json_response({"error": "diagnostic_authority_unavailable"}, status=400)
    return web.json_response({"error": "unauthorized"}, status=401)


def diagnostic_policy_allowed(route):
    """Use current local grants; policy failure is never an upstream credential error."""
    try:
        import probe_policy
        import tool
        source = (route.get("original_source") or {}).get("base_url", "")
        return not probe_policy.permanently_denied(source) and probe_policy.probe_allowed(
            source, tool.current_probe_policy(tool.load_settings()))
    except Exception:
        return False


def prepare_automatic_request(route, path, body):
    """Bound native automatic probes after routing validation, before quota admission."""
    prefix = route.get("diagnostic_prefix", "")
    if route.get("prefix") != prefix or prefix != "d" + route["id"]:
        raise RelayError("automatic_probe_invalid_route")
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
    except (TypeError, UnicodeDecodeError, ValueError):
        raise RelayError("automatic_probe_invalid_payload") from None
    split = urllib.parse.urlsplit(path)
    section = route["source_section"]

    def bounded(target, field):
        current = target.get(field, 16)
        if isinstance(current, bool) or not isinstance(current, int) or current <= 0:
            raise RelayError("automatic_probe_invalid_budget")
        target[field] = min(current, 16)

    if section == "gemini-api-key":
        match = re.fullmatch(r"/(?:v1beta|v1)/models/(.+):(generateContent|streamGenerateContent)", split.path)
        if not match:
            raise RelayError("automatic_probe_unsupported_path")
        routed_model = urllib.parse.unquote(match.group(1))
        config = value.setdefault("generationConfig", {})
        if not isinstance(config, dict):
            raise RelayError("automatic_probe_invalid_payload")
        bounded(config, "maxOutputTokens")
        config["candidateCount"] = 1
    else:
        if section == "claude-api-key" and split.path == "/v1/messages":
            field = "max_tokens"
        elif section in ("openai-compatibility", "xai-api-key") and split.path == "/v1/chat/completions":
            field = "max_completion_tokens" if "max_completion_tokens" in value else "max_tokens"
            value["n"] = 1
        elif section in ("codex-api-key", "openai-compatibility", "xai-api-key") and split.path == "/v1/responses":
            field = "max_output_tokens"
        else:
            raise RelayError("automatic_probe_unsupported_path")
        routed_model = value.get("model")
        for name in {field} | ({"max_tokens", "max_completion_tokens", "max_output_tokens"} & value.keys()):
            bounded(value, name)
    if not isinstance(routed_model, str) or not routed_model.startswith(prefix + "/"):
        raise RelayError("automatic_probe_invalid_model")
    model = routed_model[len(prefix) + 1:]
    if model not in route["models"]:
        raise RelayError("automatic_probe_invalid_model")
    return model, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")


def automatic_skip_code(data):
    """Decode only bounded, known local consumer errors, never arbitrary source prose."""
    if len(data) > 64 * 1024:
        return None
    try:
        value = json.loads(data)
        for _ in range(3):
            if isinstance(value, str):
                value = json.loads(value)
                continue
            if not isinstance(value, dict):
                return None
            error = value.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                if isinstance(code, str) and code in AUTOMATIC_SKIP_CODES:
                    return code
                if code not in (None, ""):
                    return None
                message = error.get("message")
            elif isinstance(error, str):
                message = error
            else:
                return None
            if not isinstance(message, str) or len(message) > 64 * 1024:
                return None
            value = json.loads(message)
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    return None


def verify_runtime(relay_base_url, bundle, timeout=5.0):
    """Authenticated local readiness barrier before changing account endpoints."""
    from .config import normalized_base
    base = normalized_base(relay_base_url)
    if not base:
        raise RelayError("invalid_relay_base")
    request = urllib.request.Request(base + "/internal/ready", headers={"Authorization": "Bearer " + bundle["state"]["control_token"]})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.loads(response.read(16384))
        if (value.get("ready") is not True or value.get("generation") != bundle["generation"]
                or value.get("route_count") != len(bundle["routes"])
                or value.get("diagnostic_guard") != GUARD_FEATURE
                or type(value.get("automatic_output_cap")) is not int
                or value.get("automatic_output_cap") != 16
                or value.get("automatic_output_header") != AUTOMATIC_OUTPUT_HEADER):
            raise RelayError("relay_generation_not_ready")
        return value
    except RelayError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise RelayError("relay_readiness_unavailable") from None


def _headers(headers, *, request=False, consumer_token=""):
    connection = set()
    for value in headers.getall("Connection", []) if hasattr(headers, "getall") else [headers.get("Connection", "")]:
        connection.update(x.strip().lower() for x in value.split(","))
    blocked = set(HOP_HEADERS) | connection | {AUTOMATIC_PROBE_HEADER.lower(), AUTOMATIC_OUTPUT_HEADER.lower()}
    if request:
        blocked |= set(AUTH_HEADERS) | {"x-relay-generation", "x-relay-exact-key", "accept-encoding", "forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}
    out = [(key, value) for key, value in headers.items() if key.lower() not in blocked]
    if request:
        out += [("Authorization", "Bearer " + consumer_token), ("Accept-Encoding", "identity")]
    return out


class RelayRuntime:
    def __init__(self, state_dir, consumer_binary, *, request_timeout=600.0):
        self.state_dir = Path(state_dir)
        self.consumer_binary = consumer_binary
        self.request_timeout = float(request_timeout)
        try:
            self.max_response_bytes = int(os.environ.get("REQUEST_RELAY_MAX_RESPONSE_BYTES", str(MAX_BODY)))
            if not 1024 * 1024 <= self.max_response_bytes <= 512 * 1024 * 1024:
                raise ValueError()
        except ValueError:
            raise RelayError("response_limit_must_be_1_to_512_mib") from None
        self.bundle = None
        self.child = None
        self.accepting = False
        self.active = 0
        self.active_tasks = set()
        self.stopping = False
        self.client = None
        self.monitor_task = None
        self.error_category = "not_initialized"
        self.tls_adapter = None
        self.consumer_home = None
        self.consumer_guard_proof = None

    @property
    def origin(self):
        return "http://127.0.0.1:" + str(self.bundle["consumer_port"])

    async def start(self):
        import aiohttp
        trace = aiohttp.TraceConfig()
        async def refuse_redirect(session, context, params):
            # All requests here address the pinned local child. In particular,
            # ws_connect must not follow a handshake Location to another host.
            raise RelayError("consumer_redirect_blocked")
        trace.on_request_redirect.append(refuse_redirect)
        self.client = aiohttp.ClientSession(trust_env=False, auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=self.request_timeout, connect=10, sock_read=self.request_timeout),
            cookie_jar=aiohttp.DummyCookieJar(), skip_auto_headers={"User-Agent"}, trace_configs=[trace])
        self.monitor_task = asyncio.create_task(self._monitor())

    async def _stop_child(self):
        child, self.child = self.child, None
        if child and child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                child.terminate()
            try:
                await asyncio.wait_for(child.wait(), 15)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    child.kill()
                await child.wait()
        if self.consumer_home is not None:
            self.consumer_home.cleanup()
            self.consumer_home = None

    async def _registry_ready(self, bundle):
        token = bundle["state"]["consumer_token"]
        origin = "http://127.0.0.1:" + str(bundle["consumer_port"])
        try:
            async with self.client.get(origin + "/v1/models", headers={"Authorization": "Bearer " + token}, allow_redirects=False, timeout=3) as response:
                if response.status != 200:
                    return False
                value = await response.json()
            catalog = {item.get("id") for item in value.get("data", []) if isinstance(item, dict)}
            required = {prefix + "/" + m for r in bundle["routes"].values() for prefix in {r["prefix"], r.get("exact_prefix", r["prefix"]), r.get("diagnostic_prefix", r["prefix"])} for m in r["models"]}
            return required.issubset(catalog)
        except (OSError, ValueError, asyncio.TimeoutError):
            return False
        except Exception:
            # Transport exceptions must never print URLs or auth-bearing objects.
            return False

    async def _activate(self, bundle):
        self.accepting = False
        self.error_category = "generation_draining"
        drain_deadline = asyncio.get_running_loop().time() + self.request_timeout
        while self.active and not self.stopping:
            if asyncio.get_running_loop().time() >= drain_deadline:
                for task in tuple(self.active_tasks):
                    task.cancel()
                await asyncio.gather(*tuple(self.active_tasks), return_exceptions=True)
                break
            await asyncio.sleep(0.05)
        if self.stopping:
            return
        await self._stop_child()
        if self.tls_adapter:
            await self.tls_adapter.close()
            self.tls_adapter = None
        # Refuse to connect to an unrelated pre-existing listener on the port.
        probe = socket.socket()
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", bundle["consumer_port"]))
        except OSError:
            raise RelayError("consumer_port_in_use") from None
        finally:
            probe.close()
        self.bundle = bundle
        self.consumer_guard_proof = verify_consumer_guard(self.consumer_binary)
        prefixes = diagnostic_prefixes(bundle)
        if bundle.get("transports"):
            from .transport import TLSAdapter
            self.tls_adapter = TLSAdapter(bundle, timeout=self.request_timeout)
            await self.tls_adapter.start()
        child_env = {k: v for k, v in os.environ.items() if k.upper() in CHILD_ENV_KEYS}
        child_env[DIAGNOSTIC_PREFIX_ENV] = json.dumps(prefixes, separators=(",", ":"))
        # CPA writes its default bbolt KV database under auth-dir. A new child
        # needs a fresh private HOME even when reusing the same generation.
        # Never reuse or delete the legacy consumer-home or immutable config.
        generation_dir = self.state_dir / "generations" / bundle["generation"]
        self.consumer_home = tempfile.TemporaryDirectory(prefix="consumer-run-", dir=str(generation_dir))
        auth_dir = Path(self.consumer_home.name)
        empty_auth = auth_dir / "empty-auth"
        empty_auth.mkdir(mode=0o700)
        if any(empty_auth.iterdir()):
            raise RelayError("consumer_oauth_directory_not_empty")
        if os.name != "nt":
            auth_dir.chmod(0o700)
            empty_auth.chmod(0o700)
        child_env["HOME"] = str(auth_dir)
        self.child = await asyncio.create_subprocess_exec(self.consumer_binary, "-config", bundle["consumer_path"], "-local-model",
            cwd=str(auth_dir), env=child_env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.error_category = "consumer_starting"
        for _ in range(120):
            if self.child.returncode is not None:
                raise RelayError("consumer_exited_during_start")
            if await self._registry_ready(bundle):
                # Same immutable generation file + new process + full prefix registry.
                current = load_bundle(self.state_dir)
                if current["generation"] != bundle["generation"]:
                    return
                self.accepting = True
                self.error_category = ""
                LOG.info("relay generation ready %s routes=%d", bundle["generation"][:12], len(bundle["routes"]))
                return
            await asyncio.sleep(0.25)
        raise RelayError("consumer_registry_not_ready")

    async def _monitor(self):
        while not self.stopping:
            try:
                desired = load_bundle(self.state_dir)
                needs_start = not self.bundle or self.bundle["generation"] != desired["generation"] or not self.child or self.child.returncode is not None or not self.accepting
                if needs_start:
                    await self._activate(desired)
                elif not await self._registry_ready(self.bundle):
                    self.accepting = False
                    self.error_category = "consumer_registry_unavailable"
            except asyncio.CancelledError:
                raise
            except RelayError as exc:
                self.accepting = False
                self.error_category = str(exc)
            except Exception:
                self.accepting = False
                self.error_category = "consumer_lifecycle_failure"
            await asyncio.sleep(1)

    async def close(self):
        self.stopping = True
        self.accepting = False
        if self.monitor_task:
            self.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.monitor_task
        await self._stop_child()
        if self.tls_adapter:
            await self.tls_adapter.close()
            self.tls_adapter = None
        if self.client:
            await self.client.close()

    def control_authorized(self, headers):
        try:
            # Control auth must remain usable while startup is waiting for a bundle.
            state = json.loads((self.state_dir / "secrets.json").read_text(encoding="utf8"))
            return authorized(headers, state["control_token"])
        except (OSError, ValueError, KeyError):
            return False

    async def ready(self, request):
        from aiohttp import web
        if not self.control_authorized(request.headers):
            return web.json_response({"error": "unauthorized"}, status=401)
        ready = (self.accepting and self.bundle is not None and self.child is not None
                 and self.child.returncode is None and self.consumer_guard_proof is not None
                 and self.consumer_guard_proof.get("automatic_output_cap") == 16
                 and self.consumer_guard_proof.get("automatic_output_header") == AUTOMATIC_OUTPUT_HEADER)
        return web.json_response({"ready": ready, "generation": self.bundle["generation"] if self.bundle else "",
            "route_count": len(self.bundle["routes"]) if self.bundle else 0, "category": self.error_category,
            "diagnostic_guard": self.consumer_guard_proof["feature_id"] if ready else "",
            "automatic_output_cap": self.consumer_guard_proof.get("automatic_output_cap") if ready else None,
            "automatic_output_header": self.consumer_guard_proof.get("automatic_output_header") if ready else ""}, status=200 if ready else 503)

    def _automatic_authorized(self, route, bundle, model):
        """Recheck current files and grants even for a previously minted batch ticket."""
        try:
            if (not self.accepting or not self.child or self.child.returncode is not None
                    or self.consumer_guard_proof is None
                    or self.consumer_guard_proof.get("automatic_output_cap") != 16
                    or self.consumer_guard_proof.get("automatic_output_header") != AUTOMATIC_OUTPUT_HEADER
                    or not self.bundle or self.bundle["generation"] != bundle["generation"]):
                return False
            current = load_bundle(self.state_dir)
            if current["generation"] != bundle["generation"]:
                return False
            active_route = current["routes"].get(route["id"])
            if (not active_route or active_route.get("blocked")
                    or active_route.get("id") != route["id"]
                    or active_route.get("token") != route.get("token")
                    or model not in active_route["models"]):
                return False
            return diagnostic_policy_allowed(active_route)
        except Exception:
            return False

    async def _admit_automatic(self, route, bundle, model, ticket=None):
        if (not self.consumer_guard_proof or self.consumer_guard_proof.get("automatic_output_cap") != 16
                or self.consumer_guard_proof.get("automatic_output_header") != AUTOMATIC_OUTPUT_HEADER):
            raise RelayError("automatic_probe_output_cap_unavailable")
        try:
            import automatic_probe_budget as budget
        except ImportError:
            raise RelayError("automatic_probe_unavailable") from None
        validate = lambda: self._automatic_authorized(route, bundle, model)
        host = (route.get("original_source") or {}).get("base_url", "")
        try:
            if ticket is None:
                binding = {"source_id": route["id"], "model": model,
                           "generation": bundle["generation"], "limit": 1}
                ticket = await asyncio.to_thread(budget.claim_batch, self.state_dir, host, [binding], validate)
            await asyncio.to_thread(budget.consume_ticket, self.state_dir, host, ticket,
                                    route["id"], model, bundle["generation"], validate)
            if not await asyncio.to_thread(validate):
                raise RelayError("automatic_probe_authority_revoked")
        except budget.AutomaticProbeSkipped as exc:
            code = exc.code if re.fullmatch(r"automatic_probe_[a-z0-9_]+", str(exc.code)) else "automatic_probe_unavailable"
            raise RelayError(code) from None
        except RelayError:
            raise
        except Exception:
            raise RelayError("automatic_probe_unavailable") from None

    async def forward(self, request):
        from aiohttp import web
        bundle = self.bundle
        route = (bundle or {}).get("routes", {}).get(request.match_info.get("route", ""))
        # Same response for unknown routes and wrong tokens; no route enumeration.
        if not route or not authorized(request.headers, route["token"]):
            return await _authentication_rejection(request)
        requested_generation = request.headers.getall("X-Relay-Generation", [])
        exact = request.headers.getall("X-Relay-Exact-Key", [])
        automatic_tickets = request.headers.getall(AUTOMATIC_PROBE_HEADER, [])
        if automatic_tickets and (len(automatic_tickets) != 1 or not automatic_tickets[0]):
            return web.json_response({"error": "automatic_probe_invalid_ticket"}, status=400)
        if exact or automatic_tickets:
            if exact and (len(exact) != 1 or exact[0].lower() != "true"):
                return web.json_response({"error": "invalid_exact_key_selector"}, status=400)
            if route.get("diagnostic_prefix") != "d" + route["id"]:
                return web.json_response({"error": "diagnostic_route_unavailable"}, status=400)
            route = dict(route, prefix=route["diagnostic_prefix"])
        self.active += 1
        task = asyncio.current_task()
        self.active_tasks.add(task)
        try:
            # raw_path retains percent-encoding so traversal checks cannot be bypassed.
            marker = "/r/" + route["id"]
            if not request.raw_path.startswith(marker + "/"):
                raise RelayError("invalid_protocol_path")
            path = request.raw_path[len(marker):]
            body, marked = None, False
            if request.method == "POST":
                if request.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                    raise RelayError("compressed_request_not_supported")
                body = await request.read()
                route, path, body, marked = resolve_diagnostic_request(route, path, body)
            diagnostic = bool(exact) or marked or bool(automatic_tickets)
            if diagnostic and not diagnostic_policy_allowed(route):
                return web.json_response({"error": "diagnostic_probe_not_allowed"}, status=400)
            if diagnostic and self.consumer_guard_proof is None:
                return web.json_response({"error": "diagnostic_guard_unavailable"}, status=400)
            if not self.accepting or not self.child or self.child.returncode is not None:
                return web.json_response({"error": "diagnostic_not_ready" if diagnostic else "relay_not_ready"}, status=400 if diagnostic else 503)
            if requested_generation and (len(requested_generation) != 1 or requested_generation[0] != bundle["generation"]):
                return web.json_response({"error": "diagnostic_generation_changed" if diagnostic else "relay_generation_mismatch"}, status=400 if diagnostic else 503)
            if request.method == "GET" and path == "/v1/models":
                if automatic_tickets:
                    raise RelayError("automatic_probe_unsupported_path")
                return web.json_response({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "configured-route"} for m in route["models"]]})
            if request.headers.get("Upgrade", "").lower() == "websocket":
                if automatic_tickets:
                    raise RelayError("automatic_probe_websocket_not_supported")
                if request.method != "GET" or path != "/v1/responses" or route["source_section"] != "codex-api-key":
                    raise RelayError("unsupported_websocket_path")
                return await self._websocket(request, route, bundle)
            if request.method != "POST":
                raise RelayError("unsupported_protocol_method")
            path, body = prepare_request(route, path, body)
            if automatic_tickets or marked and route.get("automatic_probe") is True:
                model, body = prepare_automatic_request(route, path, body)
                await self._admit_automatic(route, bundle, model, automatic_tickets[0] if automatic_tickets else None)
                return await self._http(request, route, bundle, path, body, automatic=True)
            return await self._http(request, route, bundle, path, body)
        except RelayError as exc:
            if str(exc) == "consumer_redirect_blocked":
                return web.json_response({"error": "consumer_redirect_blocked"}, status=502)
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPRequestEntityTooLarge:
            return web.json_response({"error": "request_body_too_large"}, status=413)
        except (asyncio.TimeoutError, TimeoutError):
            return web.json_response({"error": "consumer_timeout"}, status=504)
        except (ConnectionError, OSError):
            return web.json_response({"error": "consumer_transport_failure"}, status=502)
        except asyncio.CancelledError:
            raise
        except Exception:
            return web.json_response({"error": "relay_transport_failure"}, status=502)
        finally:
            self.active -= 1
            self.active_tasks.discard(task)

    async def _http(self, request, route, bundle, path, body, *, automatic=False):
        from aiohttp import web
        origin = "http://127.0.0.1:" + str(bundle["consumer_port"])
        headers = _headers(request.headers, request=True, consumer_token=bundle["state"]["consumer_token"])
        if automatic:
            if (not self.consumer_guard_proof or self.consumer_guard_proof.get("automatic_output_cap") != 16
                    or self.consumer_guard_proof.get("automatic_output_header") != AUTOMATIC_OUTPUT_HEADER):
                raise RelayError("automatic_probe_output_cap_unavailable")
            headers.append((AUTOMATIC_OUTPUT_HEADER, "16"))
        async with self.client.post(origin + path, data=body, headers=headers, allow_redirects=False) as upstream:
            if (300 <= upstream.status < 400 and route.get("prefix") == route.get("diagnostic_prefix")
                    and re.fullmatch(r"d[0-9a-f]{40}", str(route.get("prefix", "")))):
                # Neither the consumer's Location nor its body may cause a second
                # hop in sub2api's HTTP client after the native guard refused it.
                return web.json_response({"error": "upstream_redirect_blocked"}, status=502)
            # Decode incrementally before model-field normalization, and never
            # propagate stale encoding/length headers after decompression.
            response_headers = [(k, v) for k, v in _headers(upstream.headers) if k.lower() != "content-encoding"]
            if "text/event-stream" not in upstream.headers.get("Content-Type", "").lower() or (automatic and upstream.status == 400):
                data = bytearray()
                async for chunk in _decoded_chunks(upstream, self.max_response_bytes):
                    data.extend(chunk)
                    if len(data) > self.max_response_bytes:
                        raise RelayError("consumer_response_too_large")
                if automatic and upstream.status == 400:
                    code = automatic_skip_code(bytes(data))
                    if code is not None:
                        return web.json_response({"error": code}, status=400)
                return web.Response(status=upstream.status, headers=response_headers, body=restore_response(route, bytes(data)))
            response = web.StreamResponse(status=upstream.status, headers=response_headers)
            await response.prepare(request)
            pending = bytearray()
            try:
                async for chunk in _decoded_chunks(upstream, self.max_response_bytes):
                    pending.extend(chunk)
                    while True:
                        match = re.search(br"\r?\n\r?\n", pending)
                        if not match:
                            break
                        event = bytes(pending[:match.end()])
                        del pending[:match.end()]
                        if len(event) > MAX_EVENT:
                            raise RelayError("consumer_sse_event_too_large")
                        await response.write(restore_sse_event(route, event))
                    if len(pending) > MAX_EVENT:
                        raise RelayError("consumer_sse_event_too_large")
                if pending:
                    await response.write(restore_sse_event(route, bytes(pending)))
                await response.write_eof()
                return response
            except BaseException:
                # Do not append a JSON success/error envelope to a partially sent SSE.
                response.force_close()
                if request.transport:
                    request.transport.close()
                raise

    async def _websocket(self, request, route, bundle):
        import aiohttp
        from aiohttp import web
        origin = "http://127.0.0.1:" + str(bundle["consumer_port"])
        headers = _headers(request.headers, request=True, consumer_token=bundle["state"]["consumer_token"])
        upstream = await self.client.ws_connect(origin + "/v1/responses", headers=headers,
            autoping=False, autoclose=False, heartbeat=None, compress=0, max_msg_size=MAX_BODY)
        downstream = web.WebSocketResponse(autoping=False, autoclose=False, heartbeat=None, compress=False, max_msg_size=MAX_BODY)
        await downstream.prepare(request)
        model = None
        active_route = route

        async def pipe(source, target, inbound):
            nonlocal model, active_route
            while True:
                message = await source.receive(timeout=self.request_timeout)
                if message.type == aiohttp.WSMsgType.TEXT:
                    data = message.data.encode("utf8")
                    if inbound:
                        value = json.loads(data)
                        if not isinstance(value, dict):
                            raise RelayError("invalid_websocket_json")
                        # Non-creation protocol events carry no routing model.
                        if value.get("type") in ("response.create", "session.update") or "model" in value:
                            resolved, _, data, marked = resolve_diagnostic_request(active_route, "/v1/responses", data, websocket=True)
                            if marked and resolved.get("automatic_probe") is True:
                                raise RelayError("automatic_probe_websocket_not_supported")
                            if marked or resolved["prefix"] == resolved.get("diagnostic_prefix"):
                                if not diagnostic_policy_allowed(resolved) or self.consumer_guard_proof is None:
                                    raise RelayError("diagnostic_probe_not_allowed")
                            active_route = resolved
                            original = json.loads(data)
                            _, data = prepare_request(active_route, "/v1/responses", data, websocket=True, continuation_model=model)
                            nested = original.get("session") if original.get("type") == "session.update" else original
                            if isinstance(nested, dict) and isinstance(nested.get("model"), str):
                                model = nested["model"]
                        elif "model" in value:
                            raise RelayError("unexpected_websocket_model")
                    else:
                        data = restore_response(active_route, data)
                    await target.send_str(data.decode("utf8"))
                elif message.type == aiohttp.WSMsgType.BINARY:
                    if inbound:
                        # Responses routing is text JSON; arbitrary binary cannot
                        # select/override another model route.
                        raise RelayError("binary_request_not_supported_by_responses")
                    await target.send_bytes(message.data)
                elif message.type == aiohttp.WSMsgType.PING:
                    await target.ping(message.data)
                elif message.type == aiohttp.WSMsgType.PONG:
                    await target.pong(message.data)
                elif message.type == aiohttp.WSMsgType.CLOSE:
                    await target.close(code=message.data or 1000, message=message.extra.encode("utf8"))
                    return
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                    return

        tasks = [asyncio.create_task(pipe(downstream, upstream, True)), asyncio.create_task(pipe(upstream, downstream, False))]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (RelayError, ValueError, asyncio.TimeoutError):
            await downstream.close(code=1008, message=b"invalid_or_timed_out_route_message")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await downstream.close()
        return downstream


async def _decoded_chunks(upstream, max_decoded=MAX_BODY):
    """Bounded gzip/deflate/Brotli decoding without changing binary payloads."""
    import zlib
    encoding = upstream.headers.get("Content-Encoding", "identity").strip().lower()
    decoder = None
    brotli_decoder = False
    if encoding in ("", "identity"):
        pass
    elif encoding in ("gzip", "deflate"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    elif encoding == "br":
        try:
            from aiohttp.compression_utils import BrotliDecompressor
            decoder = BrotliDecompressor()
            brotli_decoder = True
        except RuntimeError:
            raise RelayError("unsupported_consumer_content_encoding") from None
    else:
        raise RelayError("unsupported_consumer_content_encoding")
    compressed = decoded = 0
    first = True
    def inflate(chunk):
        nonlocal decoder
        if brotli_decoder:
            yield decoder.decompress_sync(chunk)
            return
        try:
            data = decoder.decompress(chunk, 65536)
        except zlib.error:
            if encoding != "deflate" or not first:
                raise
            decoder = zlib.decompressobj(-zlib.MAX_WBITS)
            data = decoder.decompress(chunk, 65536)
        yield data
        while decoder.unconsumed_tail:
            yield decoder.decompress(decoder.unconsumed_tail, 65536)
    try:
        async for chunk in upstream.content.iter_chunked(1024 if brotli_decoder else 65536):
            compressed += len(chunk)
            if decoder is None:
                decoded += len(chunk)
                if decoded > max_decoded:
                    raise RelayError("consumer_response_too_large")
                yield chunk
                continue
            for part in inflate(chunk):
                decoded += len(part)
                if decoded > max_decoded or (decoded > 1024 * 1024 and decoded > compressed * 200):
                    raise RelayError("consumer_decompression_limit")
                if part:
                    yield part
            first = False
        if decoder and not brotli_decoder and not decoder.eof:
            raise RelayError("invalid_consumer_compression")
    except (zlib.error, ValueError):
        raise RelayError("invalid_consumer_compression") from None


def create_app(runtime):
    from aiohttp import web
    app = web.Application(client_max_size=MAX_BODY)
    app.router.add_get("/internal/ready", runtime.ready)
    app.router.add_route("*", "/r/{route:[0-9a-f]{40}}/{tail:.*}", runtime.forward)

    async def unknown(request):
        return await _authentication_rejection(request)
    app.router.add_route("*", "/{tail:.*}", unknown)
    return app


async def _serve(options):
    from aiohttp import web
    runtime = RelayRuntime(options.state_dir, options.consumer_bin, request_timeout=options.timeout)
    await runtime.start()
    runner = web.AppRunner(create_app(runtime), access_log=None, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, options.bind, options.port).start()
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stopping.set))
    try:
        await stopping.wait()
    finally:
        runtime.accepting = False
        await runner.cleanup()  # aiohttp drains existing responses before the child.
        await runtime.close()


def main():
    parser = argparse.ArgumentParser(description="Optional local CPA request relay")
    parser.add_argument("command", choices=("serve", "health"), nargs="?", default="serve")
    parser.add_argument("--state-dir", default=os.environ.get("REQUEST_RELAY_STATE_DIR", "/app/state/relay"))
    parser.add_argument("--consumer-bin", default=os.environ.get("REQUEST_RELAY_CONSUMER_BIN", "/usr/local/bin/cpa-consumer"))
    parser.add_argument("--bind", default=os.environ.get("REQUEST_RELAY_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("REQUEST_RELAY_PORT", "8380")))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("REQUEST_RELAY_TIMEOUT", "600")))
    options = parser.parse_args()
    if options.command == "health":
        try:
            verify_runtime("http://127.0.0.1:" + str(options.port), load_bundle(options.state_dir))
            return 0
        except RelayError:
            return 1
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if sys.version_info < (3, 10):
        LOG.error("request relay requires Python 3.10 or newer")
        return 1
    try:
        import aiohttp
        if tuple(int(part) for part in aiohttp.__version__.split(".")[:3]) < (3, 14, 5):
            LOG.error("request relay requires aiohttp 3.14.5 or newer, below 4")
            return 1
    except ImportError:
        LOG.error("request relay requires optional dependency aiohttp>=3.14.5,<4")
        return 1
    try:
        asyncio.run(_serve(options))
        return 0
    except (RelayError, OSError, ImportError):
        LOG.error("relay startup failed; check private state and installed runtime")
        return 1
