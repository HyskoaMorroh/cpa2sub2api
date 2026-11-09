"""Shared, persisted per-host automatic probe budget; never grants authority."""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile
import time

import probe_policy
from relay.config import RelayError, _atomic_write, _source_bytes, _source_path, _source_root, canonical_json

PERIOD_SECONDS = 604800
TICKET_SECONDS = 3600
LEDGER_FILE = "automatic-probe-budget.json"
GUARD_FILE = ".automatic-probe-budget-guard"
HEADER = "X-Relay-Automatic-Probe"


class AutomaticProbeSkipped(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _validate(callback):
    try:
        if not callable(callback) or callback() is False:
            raise AutomaticProbeSkipped("automatic_probe_authority_revoked")
    except AutomaticProbeSkipped:
        raise
    except Exception:
        raise AutomaticProbeSkipped("automatic_probe_authority_revoked") from None


def _durable_write(path, value):
    if os.name == "nt":
        import ctypes
        descriptor, temporary = tempfile.mkstemp(prefix=".automatic-probe-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
            move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
            move.restype = ctypes.c_int
            # ReplaceExisting | WriteThrough: successful return waits for move.
            if not move(str(temporary), str(path), 0x1 | 0x8):
                raise OSError("automatic budget durable move failed")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    else:
        _atomic_write(path, value)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _root(state_dir):
    root = Path(state_dir).absolute()
    if root.is_symlink():
        raise AutomaticProbeSkipped("automatic_probe_budget_source_invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    declared, resolved = _source_root(root)
    identity = hashlib.sha256(canonical_json([os.path.normcase(str(declared)), os.path.normcase(str(resolved))])).hexdigest()
    return resolved, identity


@contextlib.contextmanager
def _lock(root):
    path = root / ".lock"
    if path.exists() or path.is_symlink():
        _source_path(root, ".lock")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise AutomaticProbeSkipped("automatic_probe_budget_source_invalid")
        if os.name == "nt":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"0")
                stream.flush()
        else:
            import fcntl
        deadline = time.monotonic() + 5.0
        while True:
            try:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AutomaticProbeSkipped("automatic_probe_budget_busy") from None
                time.sleep(0.01)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _read(root, identity):
    guard = _source_bytes(root, GUARD_FILE).decode("ascii").strip()
    data = json.loads(_source_bytes(root, LEDGER_FILE))
    if (re.fullmatch(r"[0-9a-f]{64}", guard) is None or not isinstance(data, dict)
            or data.get("version") != 1 or data.get("period_seconds") != PERIOD_SECONDS
            or data.get("guard_id") != guard or data.get("root_binding") != identity
            or not isinstance(data.get("entries"), dict)):
        raise ValueError("invalid automatic ledger")
    for host, entry in data["entries"].items():
        if (re.fullmatch(r"[0-9a-f]{64}", host) is None or not isinstance(entry, dict)
                or not _number(entry.get("attempt_at")) or not isinstance(entry.get("bindings"), dict)):
            raise ValueError("invalid automatic entry")
        ticket = entry.get("ticket")
        if ticket is not None and (not isinstance(ticket, str) or re.fullmatch(r"[A-Za-z0-9_-]{40,128}", ticket) is None
                or not _number(entry.get("expires_at"))):
            raise ValueError("invalid automatic ticket")
        for binding, value in entry["bindings"].items():
            if (re.fullmatch(r"[0-9a-f]{64}", binding) is None or not isinstance(value, dict)
                    or re.fullmatch(r"(?:[0-9a-f]{40}|direct:[0-9a-f]{64})", str(value.get("source_id", ""))) is None
                    or re.fullmatch(r"[0-9a-f]{64}", str(value.get("model_sha256", ""))) is None
                    or value.get("generation") is not None and re.fullmatch(r"[0-9a-f]{64}", str(value["generation"])) is None
                    or type(value.get("remaining")) is not int or not 0 <= value["remaining"] <= 2):
                raise ValueError("invalid automatic binding")
    return data


def _legacy_entries(history_dir):
    entries = {}
    if not history_dir:
        return entries
    path = Path(history_dir) / "model-probe-cache.json"
    if not path.exists() and not path.is_symlink():
        return entries
    _, root = _source_root(path.parent)
    data = json.loads(_source_bytes(root, path.name))
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("entries"), dict):
        raise ValueError("unverified legacy attempt history")
    for key, value in data["entries"].items():
        parts = key.split("|") if isinstance(key, str) else []
        host = probe_policy.normalize_host(parts[1]) if len(parts) >= 4 else ""
        if not host or not isinstance(value, dict) or not _number(value.get("ts")):
            raise ValueError("unverified legacy attempt history")
        digest = hashlib.sha256(host.encode()).hexdigest()
        before = entries.get(digest, {}).get("attempt_at", 0)
        entries[digest] = {"attempt_at": max(before, value["ts"]), "ticket": None, "bindings": {}}
    return entries


def initialize_budget(state_dir, validate, history_dir=None):
    """Bootstrap once; a missing/corrupt member of an existing budget is fatal."""
    try:
        _validate(validate)
        root, identity = _root(state_dir)
        with _lock(root):
            _validate(validate)
            exists = [(root / name).exists() or (root / name).is_symlink() for name in (GUARD_FILE, LEDGER_FILE)]
            if any(exists):
                if not all(exists):
                    raise ValueError("incomplete automatic budget")
                _read(root, identity)
                return
            entries = _legacy_entries(history_dir)
            guard = secrets.token_hex(32)
            _durable_write(root / GUARD_FILE, (guard + "\n").encode("ascii"))
            _durable_write(root / LEDGER_FILE, canonical_json({"version": 1, "period_seconds": PERIOD_SECONDS,
                "guard_id": guard, "root_binding": identity, "entries": entries}))
    except AutomaticProbeSkipped:
        raise
    except (OSError, ValueError, TypeError, KeyError, RelayError):
        raise AutomaticProbeSkipped("automatic_probe_budget_unavailable") from None


def _host(host, validate):
    host = probe_policy.normalize_host(host)
    if not host or probe_policy.hard_disabled() or probe_policy.permanently_denied(host):
        raise AutomaticProbeSkipped("automatic_probe_authority_revoked")
    _validate(validate)
    return hashlib.sha256(host.encode()).hexdigest()


def _binding(source_id, model, generation):
    if (re.fullmatch(r"(?:[0-9a-f]{40}|direct:[0-9a-f]{64})", str(source_id)) is None
            or not isinstance(model, str) or not model
            or generation is not None and re.fullmatch(r"[0-9a-f]{64}", str(generation)) is None):
        raise AutomaticProbeSkipped("automatic_probe_binding_changed")
    value = {"source_id": source_id, "model_sha256": hashlib.sha256(model.encode("utf8")).hexdigest(), "generation": generation}
    return hashlib.sha256(canonical_json(value)).hexdigest(), value


def claim_batch(state_dir, host, bindings, validate, now=None):
    """Claim one host job before I/O; failed and interrupted jobs still count."""
    try:
        host_key = _host(host, validate)
        instant = time.time() if now is None else now
        if not _number(instant) or not isinstance(bindings, list) or not 1 <= len(bindings) <= 64:
            raise ValueError("invalid automatic claim")
        allowed = {}
        for item in bindings:
            key, value = _binding(item["source_id"], item["model"], item.get("generation"))
            limit = item.get("limit", 1)
            if type(limit) is not int or not 1 <= limit <= 2:
                raise ValueError("invalid automatic binding budget")
            value["remaining"] = max(limit, allowed.get(key, {}).get("remaining", 0))
            allowed[key] = value
        root, identity = _root(state_dir)
        with _lock(root):
            _host(host, validate)
            data = _read(root, identity)
            previous = data["entries"].get(host_key)
            if previous is not None and instant - previous["attempt_at"] < PERIOD_SECONDS:
                raise AutomaticProbeSkipped("automatic_probe_not_due")
            ticket = secrets.token_urlsafe(32)
            data["entries"][host_key] = {"attempt_at": instant, "expires_at": instant + TICKET_SECONDS,
                "ticket": ticket, "bindings": allowed}
            _durable_write(root / LEDGER_FILE, canonical_json(data))
            return ticket
    except AutomaticProbeSkipped:
        raise
    except (OSError, ValueError, TypeError, KeyError, RelayError):
        raise AutomaticProbeSkipped("automatic_probe_budget_unavailable") from None


def consume_ticket(state_dir, host, ticket, source_id, model, generation, validate, now=None):
    """Consume one finite binding; the ticket never substitutes for fresh consent."""
    try:
        host_key = _host(host, validate)
        key, expected = _binding(source_id, model, generation)
        instant = time.time() if now is None else now
        if not _number(instant) or not isinstance(ticket, str):
            raise ValueError("invalid automatic consumption")
        root, identity = _root(state_dir)
        with _lock(root):
            _host(host, validate)
            data = _read(root, identity)
            entry = data["entries"].get(host_key)
            if not entry or not isinstance(entry.get("ticket"), str) or not secrets.compare_digest(entry["ticket"], ticket):
                raise AutomaticProbeSkipped("automatic_probe_ticket_invalid")
            if instant < entry["attempt_at"] or instant > entry["expires_at"]:
                raise AutomaticProbeSkipped("automatic_probe_ticket_expired")
            binding = entry["bindings"].get(key)
            if binding is None or any(binding.get(field) != value for field, value in expected.items()):
                raise AutomaticProbeSkipped("automatic_probe_binding_changed")
            if binding["remaining"] <= 0:
                raise AutomaticProbeSkipped("automatic_probe_budget_exhausted")
            binding["remaining"] -= 1
            _durable_write(root / LEDGER_FILE, canonical_json(data))
    except AutomaticProbeSkipped:
        raise
    except (OSError, ValueError, TypeError, KeyError, RelayError):
        raise AutomaticProbeSkipped("automatic_probe_budget_unavailable") from None
