"""Bounded source TLS comparison through an isolated, guarded native consumer.

This measures the consumer's source transport. It does not change, or claim to
measure, sub2api's own account HTTP client. Failed comparisons never publish.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import tempfile
import time
import urllib.error
import urllib.request

from relay.config import (RelayError, TLS_PRESETS, _atomic_write, _state_lock,
    canonical_json, compile_bundle, digest, export_bundle, load_bundle, route_id,
    route_identity, diagnostic_model_marker)

LEARNED_KEY = "request_relay_learned_tls_profiles"
JOURNAL = ".tls-diagnostic-journal.json"
GENERATION_FILES = ("consumer.yaml", "routes.json", "transports.json", "manifest.json")


def profile_name(profile):
    if profile is None:
        return "native"
    if (not isinstance(profile, dict) or set(profile) - {"impersonate", "ca-bundle"}
            or profile.get("impersonate") not in TLS_PRESETS):
        raise RelayError("unsupported_transport_tls_preset")
    return TLS_PRESETS[profile["impersonate"]]


def source_binding(record, source_config, model, models, provenance):
    if (not isinstance(model, str) or model not in models or not isinstance(provenance, dict)
            or provenance.get("feature_id") != "cpa2sub2api-diagnostic-no-redirect-v1"
            or provenance.get("source_commit") != "91e0dee827abf476f9f86246f68c500a112f7155"
            or re.fullmatch(r"[0-9a-f]{64}", str(provenance.get("binary_sha256", ""))) is None):
        raise RelayError("tls_diagnostic_binding_invalid")
    scope = record.get("_source_scope") or "default"
    return {"version": 1, "route_id": route_id(record, scope),
        "source": route_identity(record, scope), "source_document_sha256": digest(source_config),
        "model": model, "model_sha256": hashlib.sha256(model.encode("utf8")).hexdigest(),
        "models_sha256": digest(sorted(models)), "provenance": copy.deepcopy(provenance)}


def learned_profile(record, source_config, settings, models, provenance):
    item = (settings.get(LEARNED_KEY) or {}).get(route_id(record, record.get("_source_scope") or "default"))
    if item is None:
        return None
    try:
        binding = item["binding"]
        expected = source_binding(record, source_config, binding["model"], models, provenance)
        if binding != expected or item.get("profile") is None:
            return None
        profile_name(item["profile"])
        return copy.deepcopy(item["profile"])
    except (KeyError, TypeError, ValueError):
        return None


def _bytes(path):
    if path.is_symlink():
        raise RelayError("tls_transaction_symlink_not_permitted")
    return path.read_bytes() if path.exists() else None


def state_snapshot(state_dir, settings_path):
    """CAS covers raw settings/secrets/pointer and every immutable generation."""
    root = Path(state_dir)
    result = {"settings": _bytes(Path(settings_path)), "secrets": _bytes(root / "secrets.json"),
        "current": _bytes(root / "CURRENT"), "generations": {}}
    generations = root / "generations"
    for directory in sorted(generations.iterdir()) if generations.exists() else []:
        if directory.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", directory.name):
            raise RelayError("tls_transaction_generation_invalid")
        # consumer-run-* is a private, ephemeral native KV directory. Only the
        # four immutable generation inputs participate in publication.
        result["generations"][directory.name] = {name: _bytes(directory / name) for name in GENERATION_FILES}
    return result


def _encode(value):
    return None if value is None else base64.b64encode(value).decode("ascii")


def _decode(value):
    return None if value is None else base64.b64decode(value, validate=True)


def _journal_path(root, settings_path, key):
    if key == "settings":
        return Path(settings_path)
    if key in ("secrets.json", "CURRENT"):
        return root / key
    if re.fullmatch(r"generations/[0-9a-f]{64}/(?:consumer\.yaml|routes\.json|transports\.json|manifest\.json)", key):
        return root / key
    raise RelayError("tls_transaction_journal_path_invalid")


def recover_transaction(state_dir, settings_path):
    """Call while holding both writers' locks. Never overwrite a concurrent edit."""
    root, path = Path(state_dir), Path(state_dir) / JOURNAL
    if not path.exists():
        return False
    try:
        journal = json.loads(_bytes(path))
        if (journal.get("version") != 1 or journal.get("settings_path") != str(Path(settings_path).resolve())
                or journal.get("phase", "prepared") not in ("prepared", "committed")
                or not isinstance(journal.get("entries"), dict)):
            raise ValueError()
        entries, conflicted = [], False
        for key, value in journal["entries"].items():
            target = _journal_path(root, settings_path, key)
            before, after = _decode(value["before"]), _decode(value["after"])
            current = _bytes(target)
            if current not in (before, after):
                conflicted = True
            entries.append((target, before, after, current))
        if not entries:
            raise ValueError()
        # Only the durable marker after the final authority check accepts a
        # complete commit. Legacy/prepared journals roll back even if CURRENT
        # was written: the process may have vanished before that final check.
        accepted = journal.get("phase", "prepared") == "committed"
        if accepted:
            # Later operator edits cannot revoke an already accepted generation.
            # Keep the journal for review without mutating any committed file.
            if conflicted or not all(current == after for _, _, after, current in entries):
                raise RelayError("tls_transaction_concurrent_change_needs_review")
            path.unlink()
            return True
        for target, before, after, current in reversed(entries):
            if current == before or current != after:
                continue
            if before is None:
                target.unlink()
                if target.parent.name != "generations":
                    try:
                        target.parent.rmdir()
                    except OSError:
                        pass
            else:
                _atomic_write(target, before)
        if conflicted:
            raise RelayError("tls_transaction_concurrent_change_needs_review")
        path.unlink()
        return True
    except RelayError:
        raise
    except (OSError, ValueError, TypeError, KeyError):
        raise RelayError("tls_transaction_journal_invalid_needs_review") from None


def publish_success(state_dir, settings_path, expected, records, source_config,
                    current_bundle, binding, profile, validate):
    """Publish a complete source generation and its learned setting under CAS."""
    import tool
    root, settings_path = Path(state_dir), str(Path(settings_path).resolve())
    with tool._settings_lock(settings_path, 5.0):
        with _state_lock(root):
            recover_transaction(root, settings_path)
            if state_snapshot(root, settings_path) != expected:
                raise RelayError("tls_transaction_state_changed")
            validate()
            saved = tool._read_saved_settings(settings_path)
            rid = binding["route_id"]
            selected = copy.deepcopy(records)
            for record in selected:
                if route_id(record, current_bundle["source_scope"]) == rid:
                    record["_relay_tls"] = copy.deepcopy(profile)
            candidate = compile_bundle(selected, source_config, current_bundle["source_scope"],
                current_bundle["state"], consumer_port=current_bundle["consumer_port"],
                weighted_bucket=current_bundle.get("weighted_bucket", False),
                transport_port=current_bundle["transport_port"])
            if (set(candidate["routes"]) != set(current_bundle["routes"])
                    or any(candidate["routes"][key]["original_source"] != current_bundle["routes"][key]["original_source"]
                           or candidate["routes"][key]["models"] != current_bundle["routes"][key]["models"]
                           for key in candidate["routes"])):
                raise RelayError("tls_transaction_full_route_contract_changed")
            saved.setdefault(LEARNED_KEY, {})[rid] = {"binding": binding, "profile": copy.deepcopy(profile)}
            if isinstance(saved.get("request_relay"), dict):
                saved["request_relay"]["generation"] = candidate["generation"]
            import yaml
            consumer = yaml.safe_dump(candidate["consumer_config"], allow_unicode=True, sort_keys=True).encode("utf8")
            routes, transports = canonical_json(candidate["routes"]), canonical_json(candidate["transports"])
            manifest = {"version": 1, "generation": candidate["generation"], "source_scope": candidate["source_scope"],
                "consumer_port": candidate["consumer_port"], "transport_port": candidate["transport_port"],
                "weighted_bucket": candidate["weighted_bucket"], "consumer_sha256": hashlib.sha256(consumer).hexdigest(),
                "routes_sha256": hashlib.sha256(routes).hexdigest(), "transports_sha256": hashlib.sha256(transports).hexdigest()}
            prefix = "generations/" + candidate["generation"] + "/"
            updates = {prefix + "consumer.yaml": consumer, prefix + "routes.json": routes,
                prefix + "transports.json": transports, prefix + "manifest.json": canonical_json(manifest),
                "secrets.json": canonical_json(candidate["state"]), "settings": json.dumps(saved, ensure_ascii=False,
                    indent=2, allow_nan=False).encode("utf8"), "CURRENT": (candidate["generation"] + "\n").encode("ascii")}
            journal = {"version": 1, "phase": "prepared", "settings_path": settings_path, "entries": {
                key: {"before": _encode(_bytes(_journal_path(root, settings_path, key))), "after": _encode(value)}
                for key, value in updates.items()}}
            published_state = copy.deepcopy(expected)
            published_state.update(settings=updates["settings"], secrets=updates["secrets.json"],
                current=updates["CURRENT"])
            published_state["generations"][candidate["generation"]] = {
                name: updates[prefix + name] for name in GENERATION_FILES}
            _atomic_write(root / JOURNAL, canonical_json(journal))
            try:
                for key, value in updates.items():
                    validate()
                    target = _journal_path(root, settings_path, key)
                    if _bytes(target) != _decode(journal["entries"][key]["before"]):
                        raise RelayError("tls_transaction_concurrent_change_needs_review")
                    _atomic_write(target, value)
                load_bundle(root)  # Whole generation and secrets must read back.
                validate()
                if any(_bytes(_journal_path(root, settings_path, key)) != value
                       for key, value in updates.items()):
                    raise RelayError("tls_transaction_concurrent_change_needs_review")
                if state_snapshot(root, settings_path) != published_state:
                    raise RelayError("tls_transaction_state_changed")
                journal["phase"] = "committed"
                _atomic_write(root / JOURNAL, canonical_json(journal))
            except BaseException:
                # A prepared journal rolls back only exact authored bytes,
                # including a pointer written before the final authority check.
                recover_transaction(root, settings_path)
                raise
            # Acceptance is durable now. A cleanup failure leaves a committed
            # journal for the next writer instead of undoing an accepted commit.
            try:
                (root / JOURNAL).unlink()
            except OSError:
                pass
            return candidate


def _ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class NativeDiagnosticDriver:
    """An independent loopback front/consumer/adapter, one POST per profile."""
    def __init__(self, binary, timeout=30.0):
        self.binary, self.timeout, self.calls = str(binary), float(timeout), 0
        self.payload = None

    def run(self, record, source_config, model, profile, settings, validate):
        if self.calls >= 2:
            raise RelayError("tls_diagnostic_two_call_budget_exhausted")
        self.calls += 1
        validate()
        return asyncio.run(self._run(record, source_config, model, profile, settings, validate))

    async def _run(self, record, source_config, model, profile, settings, validate):
        from aiohttp import web
        import model_probe
        from relay.server import RelayRuntime, create_app
        consumer_port, transport_port, front_port = _ports(3)
        current = copy.deepcopy(record)
        document = copy.deepcopy(source_config)
        # Explicit baseline really selects the native transport; source payload,
        # cloak, rebuild and fingerprint configuration remain otherwise intact.
        for entry in document.get(current.get("source_section") or current.get("section"), []):
            entry.pop("request-relay-tls", None)
        current.pop("_relay_tls", None)
        if profile is not None:
            current["_relay_tls"] = copy.deepcopy(profile)
        with tempfile.TemporaryDirectory(prefix="cpa-tls-diagnostic-") as temporary:
            bundle = export_bundle([current], document, current.get("_source_scope") or "default", temporary,
                consumer_port=consumer_port, transport_port=transport_port)
            runtime = RelayRuntime(temporary, self.binary, request_timeout=self.timeout)
            runner = web.AppRunner(create_app(runtime), access_log=None, handler_cancellation=True)
            try:
                await runtime.start()
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", front_port).start()
                deadline = time.monotonic() + self.timeout
                while not runtime.accepting:
                    validate()
                    if time.monotonic() >= deadline:
                        raise RelayError("tls_diagnostic_worker_not_ready")
                    await asyncio.sleep(0.05)
                route = next(iter(bundle["routes"].values()))
                marker = diagnostic_model_marker(route, model)
                base = "http://127.0.0.1:" + str(front_port) + "/r/" + route["id"]
                if current.get("platform") not in ("anthropic", "gemini"):
                    base += "/v1"
                method, url, headers, body = model_probe._build_request(current.get("platform"), base,
                    route["token"], marker, {}, current.get("source_section") or current.get("section"))
                if self.payload is None:
                    self.payload = copy.deepcopy(body)
                else:
                    body = copy.deepcopy(self.payload)
                headers = {key: value for key, value in headers.items()
                    if key.lower() not in ("authorization", "x-api-key", "x-goog-api-key")}
                headers.update({"Authorization": "Bearer " + route["token"],
                    "X-Relay-Generation": bundle["generation"]})
                # Gemini credentials are local route tokens, never URL parameters.
                import urllib.parse
                parsed = urllib.parse.urlsplit(url)
                url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                def post():
                    validate()
                    class NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, *args, **kwargs):
                            return None
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    request = urllib.request.Request(url, data=canonical_json(body), headers=headers, method=method)
                    try:
                        with opener.open(request, timeout=self.timeout) as response:
                            raw = response.read(1024 * 1024 + 1)
                            return response.status in (200, 201) and len(raw) <= 1024 * 1024 and model_probe._valid_model_output(raw.decode("utf8"), current.get("platform"))
                    except (OSError, ValueError, urllib.error.URLError):
                        return False
                success = await asyncio.to_thread(post)
                validate()
                return success
            finally:
                runtime.accepting = False
                await runner.cleanup()
                await runtime.close()


def compare_and_publish(records, source_config, record, model, settings, current_bundle,
                        settings_path, provenance, validate, driver=None):
    root = settings.get("request_relay_state_dir")
    if not root:
        raise RelayError("tls_diagnostic_state_dir_required")
    import tool
    with tool._settings_lock(str(Path(settings_path).resolve()), 5.0):
        with _state_lock(Path(root)):
            recover_transaction(root, settings_path)
            expected = state_snapshot(root, settings_path)
    rid = route_id(record, current_bundle["source_scope"])
    route = current_bundle["routes"].get(rid)
    if route is None or model not in route["models"]:
        raise RelayError("tls_diagnostic_model_not_current")
    binding = source_binding(record, source_config, model, route["models"], provenance)
    baseline = copy.deepcopy(record.get("_relay_tls"))
    name = profile_name(baseline)
    alternate = {"impersonate": "firefox135" if name == "chrome136" else "chrome136"}
    if baseline and baseline.get("ca-bundle"):
        alternate["ca-bundle"] = baseline["ca-bundle"]
    driver = driver or NativeDiagnosticDriver(settings.get("request_relay_consumer_bin") or "/usr/local/bin/cpa-consumer")
    validate()
    if driver.run(record, source_config, model, baseline, settings, validate):
        validate()
        return {"status": "baseline_success", "saved": False, "profiles": [name]}
    validate()
    if not driver.run(record, source_config, model, alternate, settings, validate):
        validate()
        return {"status": "comparison_failed", "saved": False, "profiles": [name, profile_name(alternate)]}
    validate()
    published = publish_success(root, settings_path, expected, records, source_config,
        current_bundle, binding, alternate, validate)
    return {"status": "source_tls_profile_saved", "saved": True, "generation": published["generation"],
        "profiles": [name, profile_name(alternate)], "binding": binding}
