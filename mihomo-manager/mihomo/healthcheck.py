#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mihomo 出口健康检查 + 自动降级。

为什么是 Python 而不是 shell：
    两个候选镜像里都**没有 curl**（实测 2026-09-13）：
      · metacubex/mihomo   有 wget/sed/awk/nc，无 curl
      · python:3.12-slim   有 python3，无 curl 也无 wget
    原先的 shell 版全篇依赖 curl，放进任何一个容器都会立刻失败。
    urllib 是标准库，两边都在，且能精确控制超时与代理。

为什么不能只靠 docker healthcheck：
    healthcheck 只能把容器标成 unhealthy 或触发重启，**改不了 mihomo 的
    选路**。机场节点全挂时进程本身活得很好（端口在听、API 有响应），重启
    也救不回来，而 CPA 的 proxy-url 指着 7890，于是全部上游请求走进一个
    没有可用出口的代理里排队直到超时 —— 表面现象是上游 502/524，真因在这里。

    所以这个脚本做两件事：
      1. 判活：API 通不通、AUTO 组有没有可用节点、实际能不能出网
      2. 降级：出不去就把 PROXY 组切到 DIRECT，让流量继续走（裸连总比全挂好）
         恢复后再切回 AUTO。切换走 9090 的 PUT /proxies/:name。

退出码：0 = 健康（含"已降级到 DIRECT 且直连可用"），1 = 连直连都不通。
    降级本身不算失败：报 unhealthy 会让 depends_on 的服务连不上，反而放大故障。

环境变量：
    MIHOMO_API        默认 http://127.0.0.1:9090
    MIHOMO_PROXY      默认 http://127.0.0.1:7890
    MIHOMO_SECRET     API 鉴权密钥，留空则不带 Authorization
    MIHOMO_PROBE_URL  默认 https://www.gstatic.com/generate_204
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("MIHOMO_API", "http://127.0.0.1:9090").rstrip("/")
PROXY = os.environ.get("MIHOMO_PROXY", "http://127.0.0.1:7890")
SECRET = os.environ.get("MIHOMO_SECRET", "")
PROBE_URL = os.environ.get("MIHOMO_PROBE_URL",
                           "https://www.gstatic.com/generate_204")
# 状态落在容器可写的位置。用它记住上一轮是否降级过，才知道何时该切回 AUTO。
STATE = os.environ.get("MIHOMO_STATE_FILE", "/tmp/mihomo-egress-state")


def log(msg):
    print("[healthcheck] %s" % msg, flush=True)


def api_call(path, method="GET", payload=None, timeout=5):
    """调 mihomo 的 RESTful API。返回解析后的 JSON，失败返回 None。"""
    url = API + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if SECRET:
        headers["Authorization"] = "Bearer " + SECRET
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # 显式用 ProxyHandler({}) 建 opener：容器里常设 HTTP_PROXY，
        # 而访问本机 API 绝不能走代理，否则形成自环。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read()
            if not body:
                return {}
            try:
                return json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                return {}
    except Exception:
        return None


def probe_via_proxy(timeout=12):
    """经 7890 实际出一次网。与 CPA 使用的路径完全一致。"""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    try:
        with opener.open(PROBE_URL, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as ex:
        # 探测端点返回 204；任何明确的 HTTP 响应都说明链路是通的
        return 200 <= ex.code < 500
    except Exception:
        return False


def probe_direct(timeout=12):
    """不经代理直连。决定容器降级后还有没有价值。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(PROBE_URL, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as ex:
        return 200 <= ex.code < 500
    except Exception:
        return False


def read_state():
    try:
        with open(STATE, "r") as f:
            return f.read().strip() or "AUTO"
    except Exception:
        return "AUTO"


def write_state(v):
    try:
        with open(STATE, "w") as f:
            f.write(v)
    except Exception:
        # 状态文件写不进去只影响"恢复后切回"的判断，不该让健康检查失败
        pass


def switch(target):
    """把 PROXY 组切到 target。成功返回 True。"""
    return api_call("/proxies/PROXY", method="PUT",
                    payload={"name": target}) is not None


def main():
    # ---- 1. API 是否响应。不响应说明进程真的坏了，让 docker 重启它 ----
    if api_call("/version") is None:
        log("API 无响应（%s），判定进程异常" % API)
        return 1

    # ---- 2. AUTO 组里有多少可用节点 ----
    auto = api_call("/proxies/AUTO") or {}
    nodes = auto.get("all") or []
    node_count = len(nodes)

    # ---- 3. 真正试一次出网。节点数 > 0 不等于能用 ----
    egress_ok = probe_via_proxy()
    prev = read_state()

    if egress_ok and node_count > 0:
        # 出网正常。如果上一轮降级过，现在切回 AUTO。
        if prev == "DIRECT":
            if switch("AUTO"):
                log("出口已恢复（AUTO 组 %d 个节点），切回 AUTO" % node_count)
                write_state("AUTO")
            else:
                log("! 恢复切回 AUTO 失败，仍停在 DIRECT")
        return 0

    # ---- 4. 代理出不去：降级 DIRECT ----
    log("代理出口不可用（AUTO 节点 %d 个，探测 %s 失败），降级 DIRECT"
        % (node_count, PROBE_URL))
    if switch("DIRECT"):
        write_state("DIRECT")
    else:
        log("! 切换 DIRECT 失败（PROXY 组可能不是 select 类型，或 secret 不对）")

    if probe_direct():
        log("直连可用，容器保持健康（流量已走 DIRECT）")
        return 0

    log("直连也不通，判定不健康")
    return 1


if __name__ == "__main__":
    sys.exit(main())
