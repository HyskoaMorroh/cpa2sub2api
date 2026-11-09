"""Narrow routing-model changes, preserving prompt, tool and signature content."""
import json
import re
import secrets
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from .config import RelayError


JSON_PATHS = {"/v1/messages", "/v1/messages/count_tokens", "/v1/chat/completions", "/v1/responses", "/v1/responses/compact", "/v1beta/interactions"}
GEMINI = re.compile(r"^/(v1beta|v1)/models/([^:]+):(generateContent|streamGenerateContent|countTokens)$")


def authorized(headers, token):
    values = []
    for key, value in headers.items():
        k = key.lower()
        if k == "authorization":
            if not value.startswith("Bearer "):
                return False
            values.append(value[7:])
        elif k in ("x-api-key", "x-goog-api-key"):
            values.append(value)
    return bool(values) and all(secrets.compare_digest(v, token) for v in values)


def _route_model(route, model):
    if not isinstance(model, str) or model not in route["models"]:
        raise RelayError("model_not_in_route")
    return route["prefix"] + "/" + model


def _path(path, *, claude_messages_beta=False):
    split = urlsplit(path)
    decoded = unquote(split.path)
    if split.scheme or split.netloc or split.fragment or "\\" in decoded or any(p in (".", "..") for p in decoded.split("/")) or decoded != split.path:
        raise RelayError("invalid_protocol_path")
    if claude_messages_beta and split.path == "/v1/messages" and split.query == "beta=true":
        return split
    # Gemini uses only alt=sse; no caller-supplied targets/keys/selectors.
    query = parse_qsl(split.query, keep_blank_values=True)
    if any(k != "alt" or v != "sse" for k, v in query) or len(query) > 1:
        raise RelayError("invalid_protocol_query")
    return split


def prepare_request(route, path, body, *, websocket=False, continuation_model=None):
    section = route["source_section"]
    split = _path(path, claude_messages_beta=section == "claude-api-key")
    match = GEMINI.fullmatch(split.path)
    if match:
        if route["source_section"] not in ("gemini-api-key", "interactions-api-key"):
            raise RelayError("protocol_not_in_route")
        model = _route_model(route, match.group(2))
        return "/" + match.group(1) + "/models/" + model + ":" + match.group(3) + ("?" + split.query if split.query else ""), body
    claude_beta = section == "claude-api-key" and split.path == "/v1/messages" and split.query == "beta=true"
    if split.path not in JSON_PATHS or (split.query and not claude_beta):
        raise RelayError("unsupported_protocol_path")
    if section == "claude-api-key" and not split.path.startswith("/v1/messages"):
        raise RelayError("protocol_not_in_route")
    if section in ("codex-api-key", "xai-api-key", "openai-compatibility") and split.path.startswith("/v1/messages"):
        raise RelayError("protocol_not_in_route")
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
    except (UnicodeDecodeError, ValueError):
        raise RelayError("invalid_protocol_json") from None
    target = value
    if websocket and value.get("type") == "session.update":
        target = value.get("session")
        if not isinstance(target, dict):
            raise RelayError("invalid_websocket_session")
    if "model" in target:
        target["model"] = _route_model(route, target["model"])
    elif not websocket or (value.get("type") in ("response.create", "session.update") and not continuation_model):
        raise RelayError("missing_route_model")
    return split.path + ("?beta=true" if claude_beta else ""), json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8")


def restore_response(route, data):
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        return data
    if not isinstance(value, dict):
        return data
    changed = False
    prefix = route["prefix"] + "/"
    for obj in (value, value.get("message"), value.get("response"), value.get("session")):
        if isinstance(obj, dict) and isinstance(obj.get("model"), str) and obj["model"].startswith(prefix):
            obj["model"] = obj["model"][len(prefix):]
            changed = True
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf8") if changed else data


def restore_sse_event(route, event):
    # Multi-line SSE data is legal. Preserve all non-data envelope lines and EOLs.
    lines = event.splitlines(keepends=True)
    indexes = [i for i, line in enumerate(lines) if line.startswith(b"data:")]
    if not indexes:
        return event
    payload = b"\n".join(lines[i][5:].lstrip(b" ").rstrip(b"\r\n") for i in indexes)
    restored = restore_response(route, payload)
    if restored == payload:
        return event
    first = indexes[0]
    ending = b"\r\n" if lines[first].endswith(b"\r\n") else b"\n"
    lines[first] = b"data: " + restored + ending
    return b"".join(line for i, line in enumerate(lines) if i == first or i not in indexes)
