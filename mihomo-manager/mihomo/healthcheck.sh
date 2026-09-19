#!/bin/sh
# mihomo 出口健康检查 + 自动降级（纯 busybox 版）
#
# 为什么是 shell 而不是 Python：
#   实测 2026-09-19，metacubex/mihomo:latest 里**没有 python / python3**
#   （只有 busybox 的 wget / nc / sed / awk / grep / date）。
#   上一版用 `python3 healthcheck.py` 当 docker healthcheck，在 mihomo 容器里
#   必然 `python3: not found` → 检查恒定失败 → 容器永远 unhealthy。
#   同一镜像里也**没有 curl**（有 wget）。两个都试过，只有 shell + busybox 可用。
#
#   注：仓库里另有一份 healthcheck.py（Python），是给**本项目镜像**
#   （python:3.11-slim 派生）用的；那份不能丢，它还要探本容器自己的进程。
#
# 为什么这个脚本对**两种**宿主要都能跑：
#   本脚本有两个可能的执行者 ——
#     · metacubex/mihomo:latest             → busybox 基座，`nc`/`wget` 直接可用
#     · cpa2sub2api / cpa-upstream-importer → Debian slim + 安装的 busybox
#   Debian 上装了 busybox 也**不会**自动给出 /usr/bin/nc 这个软链
#   （实测 2026-09-19：have busybox，MISS nc），所以不能假设 `nc` 在 PATH 里。
#   下面对 `nc` 与 `wget` 各做一次"命令探测 + 回退到 busybox 子命令"，
#   两套基座共用同一份脚本，不靠"记得给 Debian 也建软链"这种隐性约定。
#
# 为什么不能只靠 docker healthcheck：
#   healthcheck 只能把容器标成 unhealthy 或触发重启，**改不了 mihomo 的选路**。
#   机场节点全挂时进程本身活得好好的（端口在听、API 有响应），重启也救不回来，
#   而 config.yaml 里配了代理的上游都指向本容器，于是请求走进一个没有可用出口的
#   代理里排队直到超时 —— 表面现象是上游 502/524，真因在这里。
#
#   所以本脚本做两件事：
#     1. 判活：API 通不通、AUTO 组有没有节点、实际能不能出网
#     2. 降级：出不去就把 PROXY 组切到 DIRECT（裸连总比全挂好），恢复后切回 AUTO
#
# 退出码：0 = 健康（含"已降级到 DIRECT 且直连可用"），1 = 连直连都不通。
#   降级本身不算失败：报 unhealthy 会让 depends_on 的服务连不上，反而放大故障。
#
# 环境变量：
#   MIHOMO_API        默认 http://127.0.0.1:9090
#   MIHOMO_PROXY_PORT 默认 7890
#   MIHOMO_SECRET     API 鉴权密钥，留空则不带 Authorization
#   MIHOMO_PROBE_URL  默认 https://www.gstatic.com/generate_204
#   MIHOMO_STATE_FILE 默认 /tmp/mihomo-egress-state
#   MIHOMO_GROUP      默认 PROXY（要切换的组名）

set -u

API_HOST="${MIHOMO_API_HOST:-127.0.0.1}"
API_PORT="${MIHOMO_API_PORT:-9090}"
PROXY_HOST="${MIHOMO_PROXY_HOST:-127.0.0.1}"
PROXY_PORT="${MIHOMO_PROXY_PORT:-7890}"
SECRET="${MIHOMO_SECRET:-}"
PROBE_URL="${MIHOMO_PROBE_URL:-https://www.gstatic.com/generate_204}"
STATE="${MIHOMO_STATE_FILE:-/tmp/mihomo-egress-state}"
GROUP="${MIHOMO_GROUP:-PROXY}"
TMP="${TMPDIR:-/tmp}"

log() { echo "[healthcheck] $*"; }

# ---- 能力探测：busybox 基座直接用，Debian 基座回退到 `busybox <applet>` ----
# 只探一次，结果放变量里，避免每次调用都 fork 两次。
NC=""
if command -v nc >/dev/null 2>&1; then
    NC="nc"
elif command -v busybox >/dev/null 2>&1 && busybox nc --help >/dev/null 2>&1; then
    NC="busybox nc"
fi

WGET=""
if command -v wget >/dev/null 2>&1; then
    WGET="wget"
elif command -v busybox >/dev/null 2>&1 && busybox wget --help >/dev/null 2>&1; then
    WGET="busybox wget"
fi

# ---- 用 nc 发一个最简 HTTP 请求（本脚本唯一的"非 wget"HTTP 手段）----
# 用法：http_req <method> <path> [json_body]
#
# 为什么必须能用 nc：切换出口是 PUT，而 busybox 的 wget 只能发 GET/POST
# （没有 -X / --method）。没有 nc 就只剩 curl，而 busybox 基座里没有 curl。
http_req() {
    _m="$1"; _p="$2"; _b="${3:-}"
    [ -n "$NC" ] || return 1
    if [ -n "$_b" ]; then
        _len=$(printf '%s' "$_b" | wc -c | tr -d ' ')
    else
        _len=0
    fi
    {
        printf '%s %s HTTP/1.1\r\n' "$_m" "$_p"
        printf 'Host: %s:%s\r\n' "$API_HOST" "$API_PORT"
        printf 'Connection: close\r\n'
        printf 'Accept: application/json\r\n'
        if [ -n "$SECRET" ]; then
            printf 'Authorization: Bearer %s\r\n' "$SECRET"
        fi
        if [ -n "$_b" ]; then
            printf 'Content-Type: application/json\r\n'
            printf 'Content-Length: %s\r\n' "$_len"
        fi
        printf '\r\n'
        [ -n "$_b" ] && printf '%s' "$_b"
    } | $NC -w 5 "$API_HOST" "$API_PORT" 2>/dev/null
}

# ---- API 是否可用：只要拿到 HTTP 状态行就算通（不看状态码）----
api_alive() {
    _resp=$(http_req GET /version)
    printf '%s' "$_resp" | head -n 1 | grep -q "HTTP/"
}

# ---- 某组当前有多少节点：解析 "all":[...] 里的元素个数 ----
# 不用 jq（镜像里没有）。转成一行后逐个匹配 "name" 字段。
group_node_count() {
    _resp=$(http_req GET "/proxies/$1")
    printf '%s' "$_resp" \
        | tr -d '\r' \
        | sed -n 's/.*"all"[[:space:]]*:[[:space:]]*\[\(.*\)\].*/\1/p' \
        | tr ',' '\n' \
        | grep -c '"'
}

# ---- 经代理实际出一次网。wget -Y on 让 busybox 认 http_proxy ----
probe_via_proxy() {
    [ -n "$WGET" ] || return 1
    http_proxy="http://$PROXY_HOST:$PROXY_PORT/" \
    https_proxy="http://$PROXY_HOST:$PROXY_PORT/" \
        $WGET -q -Y on -T 12 -O /dev/null "$PROBE_URL" >/dev/null 2>&1
}

# ---- 不经代理直连 ----
probe_direct() {
    [ -n "$WGET" ] || return 1
    http_proxy="" https_proxy="" \
        $WGET -q -Y off -T 12 -O /dev/null "$PROBE_URL" >/dev/null 2>&1
}

# ---- 切组。切成功判定为响应里含 2xx 或 4xx 状态行 ----
switch_to() {
    _resp=$(http_req PUT "/proxies/$GROUP" "{\"name\":\"$1\"}")
    printf '%s' "$_resp" | head -n 1 | grep -qE "HTTP/1\.[01] (2|4)"
    # 2xx = 切换成功；4xx 视为"组存在但拒绝"（如不是 select 类型）也算到达
}

read_state() {
    [ -f "$STATE" ] && cat "$STATE" 2>/dev/null || echo "AUTO"
}

write_state() {
    printf '%s' "$1" > "$STATE" 2>/dev/null || true
}

# ============================================================================
# 主流程
# ============================================================================

# ---- 0. 两个必需工具都没有：明确说清楚，别让它看起来像"出口不通" ----
if [ -z "$NC" ] || [ -z "$WGET" ]; then
    log "缺少 HTTP 工具（nc=${NC:-无} wget=${WGET:-无}）：容器基座里既没有 nc/wget，
也没有 busybox 可回退。这不是出口故障，是本镜像缺工具 —— 装 busybox 与 wget 即可。"
    exit 1
fi

# ---- 1. API 是否响应。不响应说明进程真的坏了，让 docker 重启它 ----
if ! api_alive; then
    log "API 无响应（$API_HOST:$API_PORT），判定进程异常"
    exit 1
fi

# ---- 2. AUTO 组里有多少节点 ----
NODES=$(group_node_count AUTO)
case "$NODES" in ''|*[!0-9]*) NODES=0 ;; esac

# ---- 3. 真正试一次出网。节点数 > 0 不等于能用 ----
PREV=$(read_state)

if probe_via_proxy; then
    if [ "$PREV" = "DIRECT" ]; then
        if switch_to AUTO; then
            log "出口已恢复（AUTO 组 $NODES 个节点），切回 AUTO"
            write_state AUTO
        else
            log "! 恢复切回 AUTO 失败，仍停在 DIRECT"
        fi
    fi
    exit 0
fi

# ---- 4. 代理出不去：降级 DIRECT ----
log "代理出口不可用（AUTO 节点 $NODES 个，探测 $PROBE_URL 失败），降级 DIRECT"
if switch_to DIRECT; then
    write_state DIRECT
else
    log "! 切换 DIRECT 失败（$GROUP 组可能不是 select 类型，或 secret 不对）"
fi

if probe_direct; then
    log "直连可用，容器保持健康（流量已走 DIRECT）"
    exit 0
fi

log "直连也不通，判定不健康"
exit 1
