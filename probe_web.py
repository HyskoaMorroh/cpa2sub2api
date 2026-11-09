"""Optional, local-first UI for the existing offline probe-consent policy.

Start explicitly: python probe_web.py serve --bind 127.0.0.1 --port 8775
PROBE_UI_TOKEN must be a strong random secret. No model/TLS diagnostics run here.
"""
import argparse
from collections import defaultdict, deque
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import urlsplit

import tool

ASSETS = Path(__file__).resolve().with_name("probe_web_static")
SESSION_SECONDS = 15 * 60
MAX_BODY = 65536
REJECT_DRAIN_SECONDS = 0.25


class UiError(Exception):
    def __init__(self, message, status=400, code="INVALID_REQUEST"):
        super().__init__(message)
        self.status, self.code = status, code


class ProbeService:
    """Public projections and narrow mutations; never return a settings object."""
    def __init__(self):
        self.lock = threading.RLock()
        self.last_application = None

    def _saved(self):
        try:
            if not os.path.exists(tool.SETTINGS_PATH):
                return {}
            with io.open(tool.SETTINGS_PATH, encoding="utf-8-sig") as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError("not a mapping")
            return value
        except Exception:
            raise UiError("本地设置无法安全读取，未改写任何内容。请检查设置文件后重试。", 503, "SETTINGS_UNAVAILABLE") from None

    def _catalog(self):
        categories = defaultdict(set)
        if not os.path.isfile(tool.CONFIG_YAML):
            return categories
        try:
            with io.open(tool.CONFIG_YAML, encoding="utf-8-sig") as stream:
                document = tool._yaml().safe_load(stream)
            if not isinstance(document, dict):
                raise ValueError("not a mapping")
            records, _ = tool.collect(document)
            for record in records:
                host = tool.probe_policy.normalize_host(record.get("base_url"))
                if host:
                    section = record.get("source_section") or record.get("section")
                    categories[host].add(tool.SECTION_GROUP.get(section, "其他"))
            return categories
        except Exception:
            raise UiError("站点配置无法安全读取，未改写任何内容。请检查配置后刷新。", 503, "CONFIG_UNAVAILABLE") from None

    def _state(self):
        saved = self._saved()  # Reject corruption before load_settings' fallback.
        effective = tool.current_probe_policy(tool.load_settings())
        catalog = self._catalog()
        desired_switches = tool.probe_policy.site_switches(saved)
        effective_switches = tool.probe_policy.site_switches(effective)
        hard_disabled = tool.probe_policy.hard_disabled(effective)
        hard_reason = ("MODEL_PROBE_HARD_DISABLED 已开启，保存的授权当前不会生效。" if hard_disabled else "")
        default_master = os.environ.get("MODEL_PROBE_ENABLED", tool.DEFAULTS.get("model_probe_enabled", False))
        env_hosts = bool(os.environ.get("ONLINE_PROBE_HOSTS", "").strip())
        master = {"desired": tool.probe_policy.truthy(saved.get("model_probe_enabled", default_master)),
                  "effective": not hard_disabled and tool.probe_policy.truthy(effective.get("model_probe_enabled", False)),
                  "environmentOverride": tool.probe_policy.truthy(os.environ.get("MODEL_PROBE_HARD_DISABLED", False)),
                  "hardDisabled": hard_disabled, "hardDisableReason": hard_reason}
        sites = []
        for host in sorted(set(catalog) | set(desired_switches) | set(effective_switches)):
            denied = tool.probe_policy.permanently_denied(host)
            active = tool.probe_policy.probe_allowed(host, effective)
            if denied:
                reason = "永久禁探规则锁定，不能通过网页、批量操作或环境变量解除。"
            elif hard_disabled:
                reason = hard_reason
            elif not master["effective"]:
                reason = "检测总闸关闭。"
            elif not effective_switches.get(host, False):
                reason = ("站点授权关闭。" if host in desired_switches and not desired_switches[host]
                          else "环境站点清单未授权。" if env_hosts else "站点授权关闭。")
            else:
                reason = "当前允许检测；保存开关不会执行检测。"
            sites.append({"host": host, "categories": sorted(catalog.get(host) or {"历史授权"}),
                          "desired": bool(desired_switches.get(host, False)), "effective": active,
                          "locked": denied, "reason": reason, "configured": host in catalog,
                          "environmentOverride": env_hosts})
        return {"master": master, "sites": sites, "environmentHostsOverride": env_hosts,
                "autoRecoveryEnabled": tool.probe_policy.truthy(effective.get("auto_recover_enabled", True)),
                "lastApplication": self.last_application}

    def snapshot(self):
        with self.lock:
            return self._state()

    def _reconcile(self, hosts=None):
        """Only admin reads/writes. Selected closures cannot enable other sites."""
        try:
            self._saved()
            settings = tool.current_probe_policy(tool.load_settings())
            api = tool.Sub2Api(settings)
            rows, error = tool._fetch_all_accounts(api)
            if error:
                return {"ok": False, "count": 0, "failed": 1}
            if hosts is not None:
                rows = [row for row in rows if tool.probe_policy.normalize_host(
                    tool.account_probe_base_url(row)) in hosts]
            targets = tool.managed_plan_targets(rows)
            count, failures = tool.ensure_test_plans(api, targets, settings)
            return {"ok": not bool(failures), "count": count, "failed": len(failures)}
        except Exception:
            # API errors may contain credentials, URL query strings or raw bodies.
            return {"ok": False, "count": 0, "failed": 1}

    def update(self, payload):
        with self.lock:
            if not isinstance(payload, dict):
                raise UiError("请求必须是设置对象。")
            before = self._state()
            by_host = {row["host"]: row for row in before["sites"]}
            master_change = set(payload) == {"master"} and type(payload.get("master")) is bool
            site_change = set(payload) == {"hosts", "enabled"} and type(payload.get("enabled")) is bool
            if not master_change and not site_change:
                raise UiError("仅允许修改总闸，或明确选择的站点开关。")
            selected = set()
            if site_change:
                hosts = payload["hosts"]
                if not isinstance(hosts, list) or not hosts or len(hosts) > 2000:
                    raise UiError("请至少选择一个站点。")
                for host in hosts:
                    if not isinstance(host, str) or host != tool.probe_policy.normalize_host(host) or host not in by_host:
                        raise UiError("站点不在当前配置清单中，请刷新后重试。")
                    row = by_host[host]
                    if payload["enabled"] and (row["locked"] or not row["configured"]):
                        raise UiError("所选站点含永久禁探或已移除配置的站点，未保存。", 409, "SITE_LOCKED")
                    selected.add(host)
            enabled = payload["master"] if master_change else payload["enabled"]
            if enabled and before["master"]["hardDisabled"]:
                raise UiError(before["master"]["hardDisableReason"], 409, "PROBE_HARD_DISABLED")

            def mutate(saved):
                if enabled and tool.probe_policy.hard_disabled(tool.load_settings()):
                    raise UiError("MODEL_PROBE_HARD_DISABLED 已开启，未保存新的授权。", 409, "PROBE_HARD_DISABLED")
                if site_change and enabled:
                    current_hosts = self._catalog()
                    if any(host not in current_hosts or tool.probe_policy.permanently_denied(host) for host in selected):
                        raise UiError("站点配置已变化，请刷新后重试。", 409, "SITE_CHANGED")
                if master_change:
                    saved["model_probe_enabled"] = enabled
                else:
                    switches = tool.probe_policy.site_switches(saved)
                    switches.update({host: enabled for host in selected})
                    saved["online_probe_hosts"] = switches
                return saved

            try:
                tool.update_saved_settings(mutate)
            except UiError:
                raise
            except Exception:
                raise UiError("设置保存失败。请检查持久化目录权限后重试。", 503, "SAVE_FAILED") from None
            if enabled:
                result = {"saved": True, "applyStatus": "saved",
                          "message": "授权已保存；未执行检测或创建计划。需要更新已有管理计划时，可点击校准计划。"}
            else:
                after = self._state()
                closing = {r["host"] for r in after["sites"]} if master_change else selected
                still_effective = {r["host"] for r in after["sites"] if r["host"] in closing and r["effective"]}
                # Environment authority has precedence; never silently override it.
                reconcile_hosts = None if master_change and not after["master"]["effective"] else closing - still_effective
                reconciliation = self._reconcile(hosts=reconcile_hosts)
                final = self._state()
                still_effective.update(r["host"] for r in final["sites"] if r["host"] in closing and r["effective"])
                still_master = master_change and final["master"]["effective"]
                applied = reconciliation["ok"] and not still_effective and not still_master
                result = {"saved": True, "applyStatus": "applied" if applied else "saved_not_applied",
                          "reconciliation": reconciliation,
                          "message": ("关闭已保存，相关受管计划已完成停用校准。" if applied else
                                      "关闭已保存，但当前授权仍有效；请刷新检查环境或并发修改后重新校准。" if still_effective or still_master else
                                      "关闭已保存，管理端未能核实全部旧计划已停用。请检查管理端连接后重新校准。")}
            self.last_application = {k: result[k] for k in ("applyStatus", "message")}
            result["state"] = self._state()
            return result

    def reconcile(self):
        with self.lock:
            self._saved()
            result = self._reconcile()
            result.update({"applyStatus": "applied" if result["ok"] else "not_applied",
                           "message": "管理计划已校准；此结果不代表上游模型可用。" if result["ok"] else
                                      "管理计划校准失败或未完整核实。请检查管理端连接并重试。"})
            self.last_application = {k: result[k] for k in ("applyStatus", "message")}
            result["state"] = self._state()
            return result


class ProbeServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, token, service, origin=None):
        super().__init__(address, ProbeHandler)
        self.token, self.service = token, service
        self.sessions, self.attempts = {}, defaultdict(deque)
        self.auth_lock = threading.Lock()
        port = self.server_address[1]
        self.allowed_origins = {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port,
                                "http://[::1]:%d" % port}
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
                self.server_close()
                raise ValueError("PROBE_UI_ORIGIN 必须是精确的 http(s) 源，不含路径或凭据。")
            self.allowed_origins = {origin}
        self.allowed_hosts = {urlsplit(value).netloc for value in self.allowed_origins}
        self.secure_cookie = all(value.startswith("https://") for value in self.allowed_origins)


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "ProbePanel"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self._body_read = 0

    def log_message(self, format, *args):
        pass  # Request URLs, tokens and raw exceptions never enter access logs.

    def send_error(self, code, message=None, explain=None):
        self._send(code, {"error": {"code": "HTTP_ERROR", "message": "请求未被接受。"}})

    def _drain_rejected_body(self):
        """Avoid resetting normal uploads while bounding rejected-client work.

        Header-level denials happen before _body. Windows can reset the TCP
        connection if it closes with that legitimate request body still unread.
        Drain only one unambiguous, bounded Content-Length, under one deadline;
        never interpret chunked, oversized or conflicting framing here.
        """
        headers = getattr(self, "headers", None)
        if headers is None or headers.get("Transfer-Encoding"):
            return
        lengths = headers.get_all("Content-Length", [])
        if (len(lengths) != 1 or len(lengths[0]) > len(str(MAX_BODY))
                or not lengths[0].isascii() or not lengths[0].isdecimal()):
            return
        length = int(lengths[0])
        if not 0 < length <= MAX_BODY:
            return
        remaining = length - self._body_read
        deadline = time.monotonic() + REJECT_DRAIN_SECONDS
        previous_timeout = self.connection.gettimeout()
        try:
            while remaining > 0:
                budget = deadline - time.monotonic()
                if budget <= 0:
                    break
                self.connection.settimeout(budget)
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    break
                self._body_read += len(chunk)
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _send(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
        self.close_connection = True
        if status >= 400:
            self._drain_rejected_body()
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(body)), "Connection": "close",
                           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                           "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
                           "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
                           "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}.items():
            self.send_header(key, value)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def _session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            sid = cookie["probe_session"].value if "probe_session" in cookie else ""
        except Exception:
            sid = ""
        with self.server.auth_lock:
            now = time.monotonic()
            self.server.sessions = {key: val for key, val in self.server.sessions.items() if val["expires"] > now}
            session = self.server.sessions.get(sid)
        return sid, session

    def _body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise UiError("请使用 JSON 请求。", 415, "CONTENT_TYPE")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= MAX_BODY:
                raise ValueError()
            raw = self.rfile.read(length)
            self._body_read += len(raw)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise UiError("请求正文无效或过大。") from None

    def _check_boundary(self, write=False):
        if urlsplit(self.path).query:
            raise UiError("请求地址不接受查询参数。")
        if self.headers.get("Host") not in self.server.allowed_hosts:
            raise UiError("请求来源不受允许。", 403, "ORIGIN_DENIED")
        if write:
            origin = self.headers.get("Origin")
            if origin not in self.server.allowed_origins or urlsplit(origin).netloc != self.headers.get("Host"):
                raise UiError("请求来源不受允许。", 403, "ORIGIN_DENIED")
            if self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin":
                raise UiError("请在面板原页面操作。", 403, "ORIGIN_DENIED")

    def _dispatch(self):
        method, path = self.command, self.path
        write = method in {"POST", "PATCH", "DELETE"}
        self._check_boundary(write)
        if method == "GET" and path in {"/", "/app.js", "/styles.css"}:
            name = "index.html" if path == "/" else path[1:]
            kind = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                    "styles.css": "text/css; charset=utf-8"}[name]
            return self._send(200, (ASSETS / name).read_bytes(), kind)
        if method == "POST" and path == "/api/session":
            now, peer = time.monotonic(), self.client_address[0]
            with self.server.auth_lock:
                if len(self.server.attempts) > 1000:
                    self.server.attempts.clear()
                attempts = self.server.attempts[peer]
                while attempts and attempts[0] <= now - 60:
                    attempts.popleft()
                if len(attempts) >= 5:
                    raise UiError("登录尝试过于频繁，请一分钟后重试。", 429, "LOGIN_LIMITED")
                attempts.append(now)
            body = self._body()
            if set(body) != {"token"} or not isinstance(body["token"], str) or not hmac.compare_digest(body["token"].encode(), self.server.token.encode()):
                raise UiError("访问令牌不正确。", 401, "AUTH_REQUIRED")
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            old_sid, _ = self._session()
            with self.server.auth_lock:
                self.server.sessions.pop(old_sid, None)
                if len(self.server.sessions) >= 100:
                    self.server.sessions.pop(next(iter(self.server.sessions)))
                self.server.sessions[sid] = {"csrf": csrf, "expires": now + SESSION_SECONDS}
                self.server.attempts.pop(peer, None)
            cookie = "probe_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d%s" % (
                sid, SESSION_SECONDS, "; Secure" if self.server.secure_cookie else "")
            return self._send(200, {"csrf": csrf, "expiresIn": SESSION_SECONDS}, cookie=cookie)
        if not path.startswith("/api/"):
            raise UiError("页面不存在。", 404, "NOT_FOUND")
        sid, session = self._session()
        if not session:
            raise UiError("请使用访问令牌登录；会话可能已过期。", 401, "AUTH_REQUIRED")
        if write and not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()):
            raise UiError("页面授权已失效，请刷新后重试。", 403, "CSRF_DENIED")
        if method == "GET" and path == "/api/session":
            return self._send(200, {"csrf": session["csrf"]})
        if method == "GET" and path == "/api/state":
            return self._send(200, self.server.service.snapshot())
        if method == "PATCH" and path == "/api/settings":
            return self._send(200, self.server.service.update(self._body()))
        if method == "POST" and path == "/api/reconciliation":
            if self._body():
                raise UiError("校准请求不接受额外参数。")
            return self._send(200, self.server.service.reconcile())
        if method == "DELETE" and path == "/api/session":
            with self.server.auth_lock:
                self.server.sessions.pop(sid, None)
            return self._send(200, {"ok": True}, cookie="probe_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        raise UiError("接口不存在。", 404, "NOT_FOUND")

    def _handle(self):
        try:
            try:
                self._dispatch()
            except UiError as error:
                self._send(error.status, {"error": {"code": error.code, "message": str(error)}})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            try:
                self._send(503, {"error": {"code": "UNAVAILABLE", "message": "面板暂时无法完成操作，请稍后重试。"}})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    do_GET = do_POST = do_PATCH = do_DELETE = _handle


def create_server(bind="127.0.0.1", port=8775, token=None, service=None, origin=None):
    token = token if token is not None else os.environ.get("PROBE_UI_TOKEN", "")
    if not 32 <= len(token) <= 512 or len(set(token)) < 12 or any(ch.isspace() for ch in token):
        raise ValueError("PROBE_UI_TOKEN 需使用至少32字符的高强度随机令牌。")
    if not callable(getattr(tool, "update_saved_settings", None)):
        raise ValueError("缺少共享原子设置更新接口，请使用完整版本的 tool.py。")
    return ProbeServer((bind, port), token, service or ProbeService(), origin or os.environ.get("PROBE_UI_ORIGIN"))


def main(arguments=None):
    parser = argparse.ArgumentParser(description="本项目可选的站级检测授权面板（默认不运行）")
    parser.add_argument("action", choices=["serve"])
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8775)
    args = parser.parse_args(arguments)
    try:
        server = create_server(args.bind, args.port)
    except (ValueError, OSError) as error:
        # Startup validation errors contain no environment values.
        parser.exit(2, "%s\n" % error if isinstance(error, ValueError) else "面板监听失败，请检查地址与端口。\n")
    print("检测授权面板已启动（端口 %d）；等待本地认证访问。" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
