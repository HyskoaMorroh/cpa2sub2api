"""Explicit, offline consent policy shared by all upstream diagnostics.

Host digests keep deployment-specific deny rules out of public documentation.
The deny rules cannot be overridden by environment variables or saved settings.
"""
import hashlib
import os
import re
from urllib.parse import urlsplit

WEEKLY_CRON = "0 3 * * 0"
DENIED_HOST_DIGESTS = frozenset({
    "b4b8b89a088063bbc488497c22861761bfdb3242f657bd2d24c969e9917baf36",
    "a0463d0763c5b10ed8021631b1d5e6a3139b6418031ade9029c8fb85cfdddb79",
    "793606e351a820d128fbdeaafd250c544f17b3309ace7beef04f4e58d2e4ad3e",
})


def truthy(value):
    return value is True or (isinstance(value, (str, int))
                            and str(value).strip().lower() in {"true", "1", "yes", "on"})


def normalize_host(value):
    """Canonical DNS name; reject ambiguous URL spellings rather than guessing."""
    text = str(value or "").strip()
    if not text or re.search(r"[\s\\%@]", text):
        return ""
    try:
        parsed = urlsplit(text if "://" in text else "https://" + text)
        if parsed.scheme.lower() not in {"https", "http"} or parsed.username or parsed.password:
            return ""
        parsed.port  # Validate malformed/out-of-range ports before authorizing.
        host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        if not host or len(host) > 253:
            return ""
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                   for label in host.split(".")):
            return ""
        return host
    except (ValueError, UnicodeError):
        return ""


def permanently_denied(value):
    host = normalize_host(value)
    if not host:
        return True
    labels = host.split(".")
    return any(hashlib.sha256(".".join(labels[i:]).encode("ascii")).hexdigest()
               in DENIED_HOST_DIGESTS for i in range(len(labels)))


def site_switches(settings):
    configured = (settings or {}).get("online_probe_hosts") or {}
    if isinstance(configured, str):
        configured = {item: True for item in configured.split(",") if item.strip()}
    elif isinstance(configured, (list, tuple)):
        configured = {item: True for item in configured}
    if not isinstance(configured, dict):
        return {}
    return {normalize_host(host): truthy(enabled) for host, enabled in configured.items()
            if normalize_host(host)}


def hard_disabled(settings=None):
    """An operator veto cannot be weakened by saved or in-process grants."""
    return (truthy(os.environ.get("MODEL_PROBE_HARD_DISABLED", False))
            or truthy((settings or {}).get("model_probe_hard_disabled", False)))


def probe_allowed(value, settings):
    host = normalize_host(value)
    return bool(not hard_disabled(settings) and host and not permanently_denied(host)
                and truthy((settings or {}).get("model_probe_enabled", False))
                and site_switches(settings).get(host, False))


def weekly_cron(value=None, offset=0):
    """One execution per week; migrate historical interval cron expressions."""
    parts = str(value or WEEKLY_CRON).split()
    if (len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit()
            or parts[2:4] != ["*", "*"] or not parts[4].isdigit()
            or not 0 <= int(parts[0]) < 60 or not 0 <= int(parts[1]) < 24
            or not 0 <= int(parts[4]) <= 6):
        parts = WEEKLY_CRON.split()
    parts[0] = str((int(parts[0]) + int(offset)) % 60)
    return " ".join(parts)
