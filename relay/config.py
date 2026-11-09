"""Pure CPA export and private, atomic relay state. Never contacts an upstream."""
from __future__ import annotations

from collections import OrderedDict
import contextlib
import copy
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import yaml


class RelayError(ValueError):
    """A bounded public category; do not include credentials or URLs in messages."""


SECTIONS = frozenset({"claude-api-key", "codex-api-key", "gemini-api-key", "interactions-api-key", "xai-api-key", "openai-compatibility"})
SAFE_GLOBALS = frozenset({"proxy-url", "request-retry", "max-retry-credentials", "max-retry-interval", "disable-cooling", "transient-error-cooldown-seconds", "payload", "claude", "codex", "claude-header-defaults", "codex-header-defaults", "disable-claude-cloak-mode", "tls-handshake-timeout-seconds", "passthrough-headers", "requests", "upstream", "routing"})
RELAY_METADATA = "cpa2sub2api_original_source"
TLS_PRESETS = {"chrome": "chrome136", "chrome136": "chrome136", "firefox135": "firefox135", "safari184": "safari184"}
DIAGNOSTIC_MODEL_HASH_LENGTH = 58  # 41-byte prefix + separator + 232-bit hash fits native model_id(100).


def is_diagnostic_model_marker(model):
    return isinstance(model, str) and re.fullmatch(r"[da][0-9a-f]{40}-(?:[0-9a-f]{58}|[0-9a-f]{64})", model) is not None


def is_automatic_model_marker(model):
    return isinstance(model, str) and re.fullmatch(r"a[0-9a-f]{40}-(?:[0-9a-f]{58}|[0-9a-f]{64})", model) is not None


def diagnostic_model_marker(route, model, automatic=False):
    """A Gemini-safe model ID that selects only this route's native diagnostic."""
    prefix = route.get("diagnostic_prefix")
    if (not isinstance(prefix, str) or prefix != "d" + str(route.get("id", ""))
            or re.fullmatch(r"d[0-9a-f]{40}", prefix) is None
            or not isinstance(model, str) or model not in route.get("models", [])):
        raise RelayError("invalid_diagnostic_model_binding")
    if type(automatic) is not bool:
        raise RelayError("invalid_diagnostic_model_binding")
    return ("a" + route["id"] if automatic else prefix) + "-" + hashlib.sha256(model.encode("utf8")).hexdigest()[:DIAGNOSTIC_MODEL_HASH_LENGTH]


def _diagnostic_model_markers(route, model, automatic=False):
    """Generate the compact marker and its exact legacy equivalent only."""
    marker = diagnostic_model_marker(route, model, automatic=automatic)
    legacy = marker[:42] + hashlib.sha256(model.encode("utf8")).hexdigest()
    return marker, legacy


def diagnostic_model_alias(route, marker):
    if not is_diagnostic_model_marker(marker):
        if isinstance(marker, str) and re.match(r"^[ad][0-9a-f]{40}[-/]", marker):
            raise RelayError("diagnostic_model_route_mismatch")
        return None
    expected_prefix = "a" + route.get("id", "") if is_automatic_model_marker(marker) else route.get("diagnostic_prefix")
    if marker[:41] != expected_prefix:
        raise RelayError("diagnostic_model_route_mismatch")
    matches = [model for model in route.get("models", []) if marker in _diagnostic_model_markers(
        route, model, automatic=is_automatic_model_marker(marker))]
    if len(matches) != 1:
        raise RelayError("diagnostic_model_not_in_route")
    return matches[0]


def diagnostic_model_mapping(route, mapping):
    """Exact self-maps prevent sub2api wildcard mappings swallowing a marker."""
    result = copy.deepcopy(mapping)
    for model in route.get("models", []):
        for automatic in (False, True):
            for marker in _diagnostic_model_markers(route, model, automatic=automatic):
                if marker in result and result[marker] != marker:
                    raise RelayError("diagnostic_model_mapping_collision")
                result[marker] = marker
    return result


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


def normalized_base(value):
    try:
        u = urlsplit(str(value).strip())
        if not u.scheme and not u.netloc and not value:
            return ""
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise RelayError("invalid_original_base")
        host = u.hostname.rstrip(".").lower()
        host = "[" + host + "]" if ":" in host else host
        port = u.port
        netloc = host + ((":" + str(port)) if port and port != {"http": 80, "https": 443}[u.scheme] else "")
        return urlunsplit((u.scheme, netloc, u.path.rstrip("/"), "", ""))
    except (ValueError, TypeError):
        raise RelayError("invalid_original_base") from None


def route_identity(record, scope):
    section = record.get("source_section") or record.get("section")
    if section not in SECTIONS or not isinstance(scope, str) or not scope.strip():
        raise RelayError("invalid_source_scope_or_protocol")
    key = record.get("api_key")
    if not isinstance(key, str) or not key:
        raise RelayError("missing_original_key")
    return {"scope": scope, "source_section": section, "group": str(record.get("group") or section),
            "base_url": normalized_base(record.get("base_url") or ""), "key_sha256": hashlib.sha256(key.encode()).hexdigest()}


def route_id(record, scope):
    return digest(route_identity(record, scope))[:40]


def requires_relay(record, source_config):
    """One owner for fields whose faithful runtime consumer is CPA itself."""
    entry = _source_entry(record, source_config)
    if "_relay_tls" in record or "request-relay-tls" in entry:
        return True
    fields = {"cloak", "rebuild-mid-system-message", "support-prompt-cache-key", "disable-cooling", "request-retry", "fingerprint-profile", "connection-pool", "native-tool-search", "experimental-cch-signing", "disable-codex-cloaking"}
    if any(key in entry and entry[key] is not None and entry[key] != "" for key in fields):
        return True
    if any(isinstance(v, str) and v.startswith("$") for v in (entry.get("headers") or {}).values()):
        return True
    aliases = {}
    for model in entry.get("models") or []:
        if isinstance(model, dict) and model.get("name"):
            aliases.setdefault(str(model.get("alias") or model["name"]), set()).add(str(model["name"]))
    if any(len(names) > 1 for names in aliases.values()):
        return True
    return any(key in source_config and source_config[key] is not None and source_config[key] != "" for key in ("payload", "claude", "codex", "requests", "upstream", "routing", "disable-claude-cloak-mode", "claude-header-defaults", "codex-header-defaults", "request-retry", "disable-cooling", "max-retry-credentials", "max-retry-interval"))


def relay_model_mapping(record, mapping):
    """Keep supported duplicate-alias pools at the real CPA consumer boundary."""
    result = copy.deepcopy(mapping)
    aliases = {}
    for model in record.get("models_raw") or []:
        if isinstance(model, dict) and model.get("name"):
            aliases.setdefault(str(model.get("alias") or model["name"]), set()).add(str(model["name"]))
    selected = set(mapping.values())
    for alias, names in aliases.items():
        if len(names) > 1 and alias in result and names.issubset(selected):
            result[alias] = alias
    return result


def account_original_source(account):
    """Validate relayed source provenance; malformed metadata must not go direct."""
    extra = account.get("extra") or {}
    if RELAY_METADATA not in extra:
        return None
    value = extra[RELAY_METADATA]
    try:
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("scope"), str) or not value["scope"]:
            raise ValueError()
        identity = {key: value[key] for key in ("scope", "source_section", "group", "base_url", "key_sha256")}
        if identity["source_section"] not in SECTIONS or not re.fullmatch("[0-9a-f]{64}", identity["key_sha256"]):
            raise ValueError()
        if normalized_base(identity["base_url"]) != identity["base_url"] or digest(identity)[:40] != value["route_id"]:
            raise ValueError()
        base = normalized_base((account.get("credentials") or {}).get("base_url") or "")
        if not base or not urlsplit(base).path.endswith("/r/" + value["route_id"]) or "%" in urlsplit(base).path:
            raise ValueError()
        if value.get("routing_mode", "exact_key") not in ("exact_key", "weighted_bucket"):
            raise ValueError()
        return copy.deepcopy(value)
    except (ValueError, KeyError, TypeError):
        raise RelayError("invalid_original_source_metadata") from None


def _source_entry(record, source_config):
    section = record.get("source_section") or record.get("section")
    candidates = []
    for entry in source_config.get(section, []):
        if not isinstance(entry, dict) or normalized_base(entry.get("base-url") or "") != normalized_base(record.get("base_url") or ""):
            continue
        if section == "openai-compatibility":
            for sub in entry.get("api-key-entries") or []:
                if isinstance(sub, dict) and sub.get("api-key") == record.get("api_key"):
                    item = copy.deepcopy(entry)
                    item["api-key-entries"] = [copy.deepcopy(sub)]
                    candidates.append(item)
        elif entry.get("api-key") == record.get("api_key"):
            candidates.append(copy.deepcopy(entry))
    unique = {digest(c): c for c in candidates}
    if len(unique) != 1:
        raise RelayError("source_entry_missing_or_ambiguous")
    return next(iter(unique.values()))


def _model_list(entry, record):
    """Retain declared aliases and add direct-name aliases used by sub2api mappings."""
    original = entry.get("models") or []
    if not isinstance(original, list):
        raise RelayError("invalid_route_models")
    models, aliases = [], set()
    for model in original:
        if isinstance(model, str):
            model = {"name": model, "alias": model}
        if not isinstance(model, dict) or not isinstance(model.get("name"), str) or not model["name"].strip():
            raise RelayError("invalid_route_models")
        item = copy.deepcopy(model)
        name = item["name"].strip()
        item["name"] = name
        item["alias"] = str(item.get("alias") or name).strip()
        if not any(existing.get("name") == name and existing.get("alias") == item["alias"] for existing in models):
            models.append(item)
            aliases.add(item["alias"])
        if name not in aliases:
            models.append(dict(item, alias=name))
            aliases.add(name)
    # Dynamic models selected by the importing tool must be explicitly registered.
    for name in record.get("_relay_models", []):
        if not isinstance(name, str) or not name.strip():
            raise RelayError("invalid_selected_model")
        name = name.strip()
        if name not in aliases:
            models.append({"name": name, "alias": name})
            aliases.add(name)
    if not models:
        raise RelayError("route_requires_explicit_selected_models")
    selected = record.get("_relay_models")
    if selected is not None:
        aliases = {str(name).strip() for name in selected if isinstance(name, str) and name.strip()}
    return models, sorted(aliases)


def _consumer_globals(source):
    """Avoid v8/legacy alias conflicts which make CPA rewrite immutable config."""
    result = {k: copy.deepcopy(v) for k, v in source.items() if k in SAFE_GLOBALS}
    aliases = {"proxy-url": ("requests", "proxy-url"), "tls-handshake-timeout-seconds": ("requests", "tls-handshake-timeout-seconds"),
        "passthrough-headers": ("requests", "passthrough-headers"), "payload": ("requests", "payload"),
        "claude": ("upstream", "claude"), "disable-claude-cloak-mode": ("upstream", "claude", "disable-claude-cloak-mode"),
        "claude-header-defaults": ("upstream", "claude", "header-defaults"),
        "request-retry": ("routing", "retry", "request-retry"), "max-retry-credentials": ("routing", "retry", "max-retry-credentials"),
        "max-retry-interval": ("routing", "retry", "max-retry-interval"), "disable-cooling": ("routing", "cooldown", "disable-cooling"),
        "transient-error-cooldown-seconds": ("routing", "cooldown", "transient-error-cooldown-seconds")}
    for old, path in aliases.items():
        node = source
        for part in path:
            if not isinstance(node, dict) or part not in node:
                break
            node = node[part]
        else:
            result.pop(old, None)  # Native v8 explicitly wins, including false/empty.
    return result


def compile_bundle(records, source_config, source_scope, prior_state=None, *, consumer_port=8318, weighted_bucket=False, transport_port=8391):
    """Return deterministic consumer/routes generation plus persistent random secrets.

    Pass the previous result's ``state`` on repeated pure calls. ``export_bundle``
    handles locking and persistence for normal callers. ``_relay_models`` may
    contain dynamic actual model names selected by the existing model mapper.
    """
    if not isinstance(source_config, dict) or not isinstance(consumer_port, int) or not 1 <= consumer_port <= 65535:
        raise RelayError("invalid_consumer_configuration")
    state = copy.deepcopy(prior_state or {})
    if state and state.get("version") != 1:
        raise RelayError("unsupported_relay_state")
    state.setdefault("version", 1)
    state.setdefault("consumer_token", secrets.token_urlsafe(40))
    state.setdefault("control_token", secrets.token_urlsafe(40))
    state.setdefault("route_tokens", {})
    state.setdefault("transport_tokens", {})
    if not all(isinstance(state[k], str) and len(state[k]) >= 32 for k in ("consumer_token", "control_token")) or not isinstance(state["route_tokens"], dict) or not isinstance(state["transport_tokens"], dict):
        raise RelayError("invalid_relay_secret_state")
    consumer = _consumer_globals(source_config)
    routing = copy.deepcopy(source_config.get("routing") or {})
    if not isinstance(routing, dict):
        raise RelayError("invalid_source_routing")
    routing["force-model-prefix"] = True
    if weighted_bucket:
        routing["strategy"] = "weighted-round-robin"
    else:
        routing.setdefault("strategy", "round-robin")
    if "request-retry" not in consumer and "request-retry" not in routing.get("retry", {}):
        routing.setdefault("retry", {})["request-retry"] = 0
    # Explicitly override inherited v8 aliases: v8 wins when both forms exist.
    consumer.update({
        "server": {"host": "127.0.0.1", "port": consumer_port, "discovery": {"enabled": False}, "response-compression": {"enabled": False}},
        "access": {"api-keys": [state["consumer_token"]]},
        "oauth": {"auth-dir": "./empty-auth"},
        "routing": routing,
        "management": {"allow-remote": False, "secret-key": "", "disable-control-panel": True, "disable-auto-update-panel": True},
        "logging": {"debug": False, "to-file": False, "request-log": False},
        "models": {"catalog": "", "codex-catalog": "", "devin-catalog": ""},
        "quota-exceeded": {"switch-project": False, "switch-preview-model": False}})
    routes = {}
    entries_by_route = {}
    transports = {}
    for record in records:
        identity = route_identity(record, source_scope)
        rid = digest(identity)[:40]
        entry = _source_entry(record, source_config)
        models, allowed = _model_list(entry, record)
        prefix = "r" + rid
        entry["prefix"] = prefix
        entry["models"] = models
        tls_present = "_relay_tls" in record or "request-relay-tls" in entry
        tls = record.get("_relay_tls", entry.pop("request-relay-tls", None))
        if tls_present:
            if not isinstance(tls, dict) or set(tls) - {"impersonate", "ca-bundle"} or tls.get("impersonate") not in TLS_PRESETS:
                raise RelayError("unsupported_transport_tls_preset")
            original_url = normalized_base(record.get("base_url") or "")
            if not original_url.startswith("https://"):
                raise RelayError("tls_transport_requires_https_target")
            if not isinstance(transport_port, int) or not 1 <= transport_port <= 65535 or transport_port == consumer_port:
                raise RelayError("invalid_transport_port")
            ca_bundle = tls.get("ca-bundle")
            if ca_bundle is not None and (not isinstance(ca_bundle, str) or not Path(ca_bundle).is_absolute() or not Path(ca_bundle).is_file() or not os.access(ca_bundle, os.R_OK)):
                raise RelayError("transport_ca_bundle_must_be_absolute")
            token = state["transport_tokens"].setdefault(rid, secrets.token_urlsafe(40))
            if not isinstance(token, str) or len(token) < 32:
                raise RelayError("invalid_transport_secret_state")
            original_headers = entry.get("headers") or {}
            if any(str(k).lower() == "x-relay-transport-token" for k in original_headers):
                raise RelayError("reserved_transport_header")
            proxy = entry.get("proxy-url") or source_config.get("proxy-url") or (source_config.get("requests") or {}).get("proxy-url") or ""
            if identity["source_section"] == "openai-compatibility":
                proxy = entry["api-key-entries"][0].get("proxy-url") or proxy
                entry["api-key-entries"][0]["proxy-url"] = "direct"
            else:
                entry["proxy-url"] = "direct"
            if any(str(k).lower() == "host" and str(v).lower() != urlsplit(original_url).netloc.lower() for k, v in original_headers.items()):
                raise RelayError("transport_host_override_conflicts_with_fixed_target")
            transports[rid] = {"id": rid, "target": original_url, "token": token, "impersonate": TLS_PRESETS[tls["impersonate"]], "proxy": proxy,
                "ca_bundle": ca_bundle, "upstream_api_key": record["api_key"], "configured_headers": copy.deepcopy(original_headers)}
            entry["headers"] = dict(original_headers, **{"X-Relay-Transport-Token": token})
            entry["base-url"] = "http://127.0.0.1:" + str(transport_port) + "/t/" + rid
        if identity["source_section"] == "openai-compatibility":
            entry["name"] = prefix
        token = state["route_tokens"].setdefault(rid, secrets.token_urlsafe(40))
        if not isinstance(token, str) or len(token) < 32:
            raise RelayError("invalid_route_secret_state")
        original = {"version": 1, **identity, "key_fingerprint": str(record.get("fp") or identity["key_sha256"][:12]), "route_id": rid}
        route = {"id": rid, "prefix": prefix, "exact_prefix": prefix, "diagnostic_prefix": "d" + rid, "routing_mode": "exact_key", "token": token, "source_section": identity["source_section"],
                 "models": allowed, "blocked": not bool(allowed), "original_source": original, "entry_digest": digest(entry)}
        if rid in routes:
            if routes[rid] != route:
                raise RelayError("conflicting_duplicate_route")
            continue
        routes[rid] = route
        entries_by_route[rid] = entry
        consumer.setdefault(identity["source_section"], []).append(entry)
    if weighted_bucket:
        buckets = {}
        for rid, route in routes.items():
            original = route["original_source"]
            bucket = digest({k: original[k] for k in ("scope", "source_section", "group", "base_url")})[:40]
            buckets.setdefault(bucket, []).append(rid)
        for bucket, members in buckets.items():
            if len(members) < 2:
                continue
            section = routes[members[0]]["source_section"]
            # Each route keeps its original model contract. CPA's registry
            # selects only pool members which actually serve the requested model.
            pool_prefix = "p" + bucket
            pool_entries = []
            for rid in members:
                clone = copy.deepcopy(entries_by_route[rid])
                clone["prefix"] = pool_prefix
                if section == "openai-compatibility":
                    clone["name"] = pool_prefix
                pool_entries.append(clone)
                routes[rid].update({"prefix": pool_prefix, "routing_mode": "weighted_bucket", "pool_id": bucket,
                    "pool_members": sorted(members)})
                routes[rid]["original_source"].update({"routing_mode": "weighted_bucket", "pool_id": bucket})
            if section == "openai-compatibility":
                tls_members = [rid for rid in members if rid in transports]
                if tls_members:
                    for rid in members:
                        if rid not in transports:
                            original_entry = entries_by_route[rid]
                            key_entry = original_entry["api-key-entries"][0]
                            token = state["transport_tokens"].setdefault(rid, secrets.token_urlsafe(40))
                            transports[rid] = {"id": rid, "target": routes[rid]["original_source"]["base_url"], "token": token,
                                "impersonate": None, "proxy": key_entry.get("proxy-url") or source_config.get("proxy-url") or (source_config.get("requests") or {}).get("proxy-url") or "",
                                "ca_bundle": None, "upstream_api_key": key_entry["api-key"], "configured_headers": copy.deepcopy(original_entry.get("headers") or {})}
                    # One provider shares a fixed bucket endpoint. Each CPA key
                    # is an opaque adapter credential, bound to exactly one real
                    # key/target/headers/preset in this bucket's private state.
                    for rid, item in zip(members, pool_entries):
                        item["base-url"] = "http://127.0.0.1:" + str(transport_port) + "/t/" + bucket
                        item["headers"] = {k: v for k, v in item.get("headers", {}).items() if str(k).lower() not in {"authorization", "x-relay-transport-token"}}
                        item["api-key-entries"][0]["api-key"] = transports[rid]["token"]
                        item["api-key-entries"][0]["proxy-url"] = "direct"
                    transports[bucket] = {"id": bucket, "members": sorted(members), "auth_mode": "pool_bearer"}
                template = {k: v for k, v in pool_entries[0].items() if k != "api-key-entries"}
                if any({k: v for k, v in item.items() if k != "api-key-entries"} != template for item in pool_entries[1:]):
                    raise RelayError("weighted_bucket_requires_compatible_provider_settings")
                merged = copy.deepcopy(pool_entries[0])
                merged["api-key-entries"] = [key for item in pool_entries for key in item["api-key-entries"]]
                consumer[section].append(merged)
            else:
                # The importer contract gives same-site keys one priority. A
                # pooled consumer must not suppress lower-original-priority keys.
                priority = max(int(item.get("priority") or 0) for item in pool_entries)
                for item in pool_entries:
                    item["priority"] = priority
                consumer[section].extend(pool_entries)
    # Explicit diagnostics use the same key/config/model contract but never
    # inherit business retry rounds. Their opaque prefix is route-bound only.
    for rid, route in routes.items():
        if route["blocked"]:
            continue
        diagnostic = copy.deepcopy(entries_by_route[rid])
        diagnostic["prefix"] = route["diagnostic_prefix"]
        diagnostic["request-retry"] = 0
        if route["source_section"] == "openai-compatibility":
            diagnostic["name"] = route["diagnostic_prefix"]
        consumer[route["source_section"]].append(diagnostic)
    for section in SECTIONS:
        if section in consumer:
            consumer[section].sort(key=lambda e: e["prefix"])
    routes = dict(sorted(routes.items()))
    transports = dict(sorted(transports.items()))
    generation = digest({"consumer_config": consumer, "routes": routes, "transports": transports})
    return {"version": 1, "generation": generation, "source_scope": source_scope, "consumer_port": consumer_port, "transport_port": transport_port, "weighted_bucket": bool(weighted_bucket),
            "state": state, "consumer_config": consumer, "routes": routes, "transports": transports}


def _private_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.chmod(0o700)


def _atomic_write(path, data):
    _private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def _state_lock(root):
    _private_dir(root)
    lock_path = root / ".lock"
    with open(lock_path, "a+b") as lock:
        if os.name == "nt":
            import msvcrt
            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def export_bundle(records, source_config, source_scope, state_dir, *, consumer_port=8318, weighted_bucket=False, transport_port=8391):
    root = Path(state_dir)
    with _state_lock(root):
        if (root / "CURRENT").exists():
            try:
                old_generation = (root / "CURRENT").read_text(encoding="ascii").strip()
                if not re.fullmatch("[0-9a-f]{64}", old_generation):
                    raise RelayError("invalid_generation_pointer")
                old_dir = root / "generations" / old_generation
                old_manifest = json.loads((old_dir / "manifest.json").read_text(encoding="utf8"))
                if "transports_sha256" in old_manifest:
                    previous_scope = load_bundle(root)["source_scope"]
                else:
                    # Explicitly verified legacy generation can be re-exported
                    # from the authoritative source while retaining its secrets.
                    old_consumer = (old_dir / "consumer.yaml").read_bytes()
                    old_routes = (old_dir / "routes.json").read_bytes()
                    if old_manifest.get("generation") != old_generation or hashlib.sha256(old_consumer).hexdigest() != old_manifest.get("consumer_sha256") or hashlib.sha256(old_routes).hexdigest() != old_manifest.get("routes_sha256") or digest({"consumer_config": yaml.safe_load(old_consumer), "routes": json.loads(old_routes)}) != old_generation:
                        raise RelayError("legacy_relay_generation_invalid")
                    previous_scope = old_manifest["source_scope"]
                if previous_scope != source_scope:
                    raise RelayError("relay_state_scope_mismatch")
            except (OSError, ValueError, KeyError, TypeError):
                raise RelayError("relay_previous_generation_unreadable") from None
        secret_path = root / "secrets.json"
        try:
            prior = json.loads(secret_path.read_text(encoding="utf8")) if secret_path.exists() else None
        except (OSError, ValueError):
            raise RelayError("relay_secret_state_unreadable") from None
        bundle = compile_bundle(records, source_config, source_scope, prior, consumer_port=consumer_port, weighted_bucket=weighted_bucket, transport_port=transport_port)
        # Secrets first: a crash never leaves published routes with lost auth state.
        _atomic_write(secret_path, canonical_json(bundle["state"]))
        generation_dir = root / "generations" / bundle["generation"]
        _private_dir(generation_dir)
        consumer = yaml.safe_dump(bundle["consumer_config"], allow_unicode=True, sort_keys=True).encode("utf8")
        routes = canonical_json(bundle["routes"])
        transports = canonical_json(bundle["transports"])
        manifest = {"version": 1, "generation": bundle["generation"], "source_scope": source_scope, "consumer_port": consumer_port, "transport_port": transport_port, "weighted_bucket": bool(weighted_bucket),
                    "consumer_sha256": hashlib.sha256(consumer).hexdigest(), "routes_sha256": hashlib.sha256(routes).hexdigest(), "transports_sha256": hashlib.sha256(transports).hexdigest()}
        _atomic_write(generation_dir / "consumer.yaml", consumer)
        _atomic_write(generation_dir / "routes.json", routes)
        _atomic_write(generation_dir / "transports.json", transports)
        _atomic_write(generation_dir / "manifest.json", canonical_json(manifest))
        _atomic_write(root / "CURRENT", (bundle["generation"] + "\n").encode())
        return bundle


def _source_is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _source_root(state_dir):
    declared = Path(state_dir).absolute()
    info = declared.lstat()
    if _source_is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise RelayError("generation_source_root_not_regular")
    return declared, declared.resolve(strict=True)


def _source_path(root, relative, directory=False):
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or any(part in (".", "..") for part in relative.parts):
        raise RelayError("generation_source_path_invalid")
    path = root
    for index, part in enumerate(relative.parts):
        path = path / part
        info = path.lstat()
        is_directory = directory or index < len(relative.parts) - 1
        if _source_is_link(info) or not (stat.S_ISDIR(info.st_mode) if is_directory else stat.S_ISREG(info.st_mode)):
            raise RelayError("generation_source_path_not_regular")
        if not path.resolve(strict=True).is_relative_to(root):
            raise RelayError("generation_source_path_outside_root")
    return path


def _source_bytes(root, relative):
    path = _source_path(root, relative)
    before = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RelayError("generation_source_path_changed")
        data = stream.read()
    _source_path(root, relative)  # Recheck ancestors and the final path after I/O.
    after = path.lstat()
    if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
        raise RelayError("generation_source_path_changed")
    return data


_GENERATION_PARSE_CACHE = OrderedDict()
_GENERATION_PARSE_LOCK = threading.Lock()
_GENERATION_PARSE_LIMIT = 4


def _verified_generation_parse(source_identity, generation, consumer_data, routes_data, transports_data, content_hashes):
    """Reuse decoding only after the caller verifies the current raw bytes."""
    key = (*source_identity, generation, *content_hashes)
    with _GENERATION_PARSE_LOCK:
        parsed = _GENERATION_PARSE_CACHE.get(key)
        if parsed is None:
            consumer = yaml.safe_load(consumer_data)
            routes, transports = json.loads(routes_data), json.loads(transports_data)
            if digest({"consumer_config": consumer, "routes": routes, "transports": transports}) != generation:
                raise RelayError("generation_content_mismatch")
            parsed = (consumer, routes, transports)
            _GENERATION_PARSE_CACHE[key] = parsed
            while len(_GENERATION_PARSE_CACHE) > _GENERATION_PARSE_LIMIT:
                _GENERATION_PARSE_CACHE.popitem(last=False)
        _GENERATION_PARSE_CACHE.move_to_end(key)
        # Callers own their bundle objects and cannot contaminate a later read.
        return copy.deepcopy(parsed)


def load_bundle(state_dir):
    try:
        declared, root = _source_root(state_dir)
        generation = _source_bytes(root, "CURRENT").decode("ascii").strip()
        if not re.fullmatch("[0-9a-f]{64}", generation):
            raise RelayError("invalid_generation_pointer")
        directory = _source_path(root, Path("generations") / generation, directory=True)
        prefix = Path("generations") / generation
        manifest = json.loads(_source_bytes(root, prefix / "manifest.json"))
        consumer_data = _source_bytes(root, prefix / "consumer.yaml")
        routes_data = _source_bytes(root, prefix / "routes.json")
        transports_data = _source_bytes(root, prefix / "transports.json")
        state = json.loads(_source_bytes(root, "secrets.json"))
        content_hashes = tuple(hashlib.sha256(data).hexdigest() for data in (consumer_data, routes_data, transports_data))
        if manifest["generation"] != generation or content_hashes != tuple(manifest[key] for key in (
                "consumer_sha256", "routes_sha256", "transports_sha256")):
            raise RelayError("generation_digest_mismatch")
        consumer, routes, transports = _verified_generation_parse(
            (str(declared), str(root), str(directory)), generation, consumer_data, routes_data, transports_data, content_hashes)
        if (consumer.get("access") or {}).get("api-keys") != [state.get("consumer_token")] or any(state.get("route_tokens", {}).get(rid) != route["token"] for rid, route in routes.items()) or any(state.get("transport_tokens", {}).get(rid) != item["token"] for rid, item in transports.items() if "token" in item):
            raise RelayError("generation_secret_state_mismatch")
        return {"version": 1, "generation": generation, "source_scope": manifest["source_scope"], "consumer_port": manifest["consumer_port"], "weighted_bucket": bool(manifest.get("weighted_bucket", False)),
                "state": state, "consumer_config": consumer, "routes": routes, "transports": transports, "transport_port": manifest["transport_port"], "consumer_path": str(directory / "consumer.yaml")}
    except RelayError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise RelayError("relay_generation_unreadable") from None


def adapt_account(account, record, bundle, relay_base_url):
    base = normalized_base(relay_base_url)
    if not base:
        raise RelayError("invalid_relay_base")
    rid = route_id(record, bundle["source_scope"])
    route = bundle["routes"].get(rid)
    if not route:
        raise RelayError("route_not_in_generation")
    result = copy.deepcopy(account)
    if "model_mapping" in (result.get("credentials") or {}):
        result["credentials"]["model_mapping"] = diagnostic_model_mapping(route,
            relay_model_mapping(record, result["credentials"]["model_mapping"]))
    result.setdefault("credentials", {}).update({"base_url": base + "/r/" + rid, "api_key": route["token"]})
    # Source-managed headers are consumed exactly once by CPA. Sending them to
    # the front could replace its route token with an actual upstream secret.
    for field in ("header_override_enabled", "header_overrides", "custom_headers"):
        result["credentials"].pop(field, None)
    result["proxy_id"] = None
    result.setdefault("extra", {})[RELAY_METADATA] = copy.deepcopy(route["original_source"])
    return result
