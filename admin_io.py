"""Bounded, subprocess-isolated transport for Sub management calibration only."""
from contextvars import ContextVar
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import unquote, urlsplit

RESPONSE_LIMIT = 8 * 1024 * 1024
REQUEST_DEADLINE = ContextVar("admin_request_deadline", default=None)


class Failure(Exception):
    def __init__(self, kind, status=0, retry_after=None):
        super().__init__(kind)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


def _valid_target(url, origin):
    try:
        target, base = urlsplit(url), urlsplit(origin)
        prefix = base.path.rstrip("/") + "/api/v1/admin/"
        path = unquote(target.path)
        return (target.scheme in ("http", "https") and target.scheme == base.scheme
                and target.hostname == base.hostname and target.port == base.port
                and not target.username and not target.password and not target.fragment
                and path.startswith(prefix) and "%" not in path
                and all(part not in (".", "..") for part in path.split("/")))
    except (TypeError, ValueError):
        return False


class Calibration:
    def __init__(self, origin="", request_seconds=30, phase_seconds=600, total_seconds=900):
        self.origin = origin
        value = float(request_seconds)
        self.request_seconds = min(30.0, max(.05, value)) if math.isfinite(value) else 30.0
        self.phase_seconds = max(.05, float(phase_seconds))
        self.deadline = time.monotonic() + max(.05, float(total_seconds))
        self.phase_deadline = self.deadline
        self.label, self.index, self.total, self.aid = "", 0, 0, None
        self.consecutive_failures = 0
        self.stopped = False
        self.stop_status = 0
        self.stop_reason = ""
        self.stop_phase = ""
        self.unverified = {}

    def phase(self, label, total=0):
        self.label, self.index, self.total, self.aid = label, 0, total, None
        self.phase_deadline = min(self.deadline, time.monotonic() + self.phase_seconds)

    def item(self, index, total, aid=None):
        self.index, self.total, self.aid = index, total, aid
        self.progress("开始核查")

    def progress(self, message):
        identity = " 账号id=%s" % self.aid if self.aid is not None else ""
        print("    %s [%d/%d]%s：%s" % (
            self.label, self.index, self.total, identity, message), flush=True)

    def stop(self, reason, status=0):
        if not self.stopped:
            self.stopped, self.stop_reason, self.stop_status = True, reason, status
            self.stop_phase = self.label

    def check(self):
        if not self.stopped and time.monotonic() >= self.phase_deadline:
            self.stop("管理校准预算耗尽，剩余操作未执行")
        if self.stopped:
            raise Failure("calibration_stopped", self.stop_status)

    def request_deadline(self):
        self.check()
        deadline = min(self.phase_deadline, self.deadline,
                       time.monotonic() + self.request_seconds)
        enclosing = REQUEST_DEADLINE.get()
        if enclosing is not None:
            deadline = min(deadline, enclosing)
        if deadline <= time.monotonic():
            raise Failure("deadline")
        return deadline

    def record_success(self):
        self.consecutive_failures = 0

    def record_error(self, error):
        token = (id(self), self.label, self.index)
        if getattr(error, "_admin_failure_recorded", None) == token:
            return
        error._admin_failure_recorded = token
        status = getattr(error, "status", None)
        kind = getattr(error, "admin_kind", None)
        if status in (401, 403):
            self.stop("管理接口 HTTP %s，停止剩余校准" % status, status)
        elif kind in ("boundary", "redirect", "calibration_stopped"):
            self.stop("管理请求边界拒绝，剩余校准未执行", status or 0)
        elif status == 429:
            # A server cooldown is not a failed connection. Its owning request
            # pauses within the unchanged phase/job budget or stops explicitly.
            return
        elif status == 0 or status in (500, 502, 503, 504) or isinstance(error, OSError):
            self.consecutive_failures += 1
            if self.consecutive_failures >= 3:
                self.stop("连续三次管理网络失败，停止剩余校准", status or 0)
        else:
            self.consecutive_failures = 0

    def add_unverified(self, count):
        self.unverified[self.label] = self.unverified.get(self.label, 0) + count


def request(method, url, headers, body, timeout, deadline, origin, progress=None):
    """One send, killed and joined on deadline or caller cancellation."""
    if not _valid_target(url, origin):
        raise Failure("boundary")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Failure("deadline")
    payload = json.dumps({"method": method, "url": url, "origin": origin,
        "headers": headers, "body": body, "timeout": min(float(timeout), remaining)},
        ensure_ascii=False).encode("utf-8")
    options = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        child = subprocess.Popen([sys.executable, "-X", "utf8", "-I", "-S", str(Path(__file__).resolve()), "--worker"], **options)
    except OSError:
        raise Failure("worker") from None
    first = True
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Failure("deadline")
            try:
                raw, _ = child.communicate(payload if first else None, timeout=min(5.0, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
                if time.monotonic() >= deadline:
                    raise Failure("deadline")
                if progress:
                    progress("管理网络仍在等待响应")
        if child.returncode != 0:
            raise Failure("worker")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError()
            kind = result.get("kind")
            status = result.get("status")
            if kind:
                raise Failure(kind, status if type(status) is int else 0, result.get("retry_after"))
            if type(status) is not int or not isinstance(result.get("text"), str):
                raise ValueError()
            return status, result["text"]
        except (ValueError, TypeError):
            raise Failure("worker") from None
    except BaseException:
        if child.poll() is None:
            child.kill()
        child.communicate()
        raise


def _worker():
    import urllib.error
    import urllib.request
    try:
        data = json.load(sys.stdin)
        if not _valid_target(data["url"], data["origin"]):
            raise Failure("boundary")
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise Failure("redirect", code)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        body = data.get("body")
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = dict(data["headers"])
        if encoded is not None:
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(data["url"], data=encoded, headers=headers, method=data["method"])
        try:
            response = opener.open(req, timeout=data["timeout"])
        except urllib.error.HTTPError as error:
            with error:
                raise Failure("http", error.code, error.headers.get("Retry-After"))
        with response:
            try:
                declared = int(response.headers.get("Content-Length", "0"))
            except ValueError:
                declared = 0
            if declared > RESPONSE_LIMIT:
                raise Failure("response_too_large")
            raw = response.read(RESPONSE_LIMIT + 1)
            if len(raw) > RESPONSE_LIMIT:
                raise Failure("response_too_large")
            result = {"status": response.status, "text": raw.decode("utf-8", "replace")}
    except Failure as error:
        result = {"kind": error.kind, "status": error.status, "retry_after": error.retry_after}
    except Exception:
        result = {"kind": "network", "status": 0}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        _worker()
