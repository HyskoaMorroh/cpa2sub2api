#!/bin/sh
# mihomo 配置自举 —— 由**本项目镜像**（cpa2sub2api 或 upstream-importer）作为 init 容器执行
#
# 为什么需要这个脚本：
#   部署方式是"VPS 只拉镜像，宿主不预置任何文件"。而 mihomo 官方镜像基于
#   scratch —— 没有 shell、没有 curl/wget、没有 python，它自己没法把模板
#   展开成配置。所以由本镜像（有 python3 与 shell）充当 init 容器：
#   启动时把 config.template.yaml 物化到共享卷，mihomo 容器再挂同一个卷读它。
#
#   compose 里的顺序由 depends_on + service_completed_successfully 保证：
#     mihomo-init（本脚本，跑完即退）-> mihomo（长驻）
#
# 幂等：卷里已有 config.yaml 就不覆盖，只补 healthcheck 与地理数据。
#   想强制重建：设 MIHOMO_FORCE_REBUILD=1，或直接删掉卷里的 config.yaml。
#   不默认覆盖是因为探测脚本会把结果写进 AUTO 组的 filter，
#   容器重启就冲掉那些结果等于让探测白跑。
#
# 环境变量：
#   MIHOMO_TARGET_DIR      物化目标目录（默认 /mihomo-config，即共享卷挂载点）
#   MIHOMO_SRC_DIR         镜像内 mihomo-manager 所在目录（默认 /app/mihomo-manager）
#   MIHOMO_GEO_SRC_DIR     镜像内地理数据所在目录（默认 /usr/local/share/mihomo）
#   MIHOMO_SECRET          RESTful API 鉴权密钥；留空则不设鉴权并告警
#   MIHOMO_SUBSCRIPTIONS   订阅清单，多行或分号分隔的 `名称=URL`
#   MIHOMO_SUB_<名称>      单个订阅的 URL（与上面二选一，大写名称）
#   MIHOMO_FORCE_REBUILD   非空则强制用模板覆盖现有 config.yaml

set -eu

SRC_DIR="${MIHOMO_SRC_DIR:-/app/mihomo-manager}"
DST_DIR="${MIHOMO_TARGET_DIR:-/mihomo-config}"
TEMPLATE="$SRC_DIR/mihomo/config.template.yaml"
CONFIG="$DST_DIR/config.yaml"

log() { echo "[mihomo-init] $*"; }
die() { echo "[mihomo-init] ERROR: $*" >&2; exit 1; }

[ -f "$TEMPLATE" ] || die "模板不存在: $TEMPLATE（镜像构建可能漏拷了 mihomo-manager）"

mkdir -p "$DST_DIR/providers"

# ---- healthcheck 每次都同步：它是脚本不是配置，没有"用户改过"的语义 ----
# 两份都要同步，因为**两个容器的工具集不同**：
#   · healthcheck.sh —— 给 mihomo 容器用。实测 metacubex/mihomo:latest 里
#     没有 python3 / curl，只有 busybox 的 wget/nc/sed/awk/grep，
#     所以健康检查只能写成纯 shell。
#   · healthcheck.py —— 给本镜像（python:3.11-slim 派生，有 python3 无 curl）
#     或其它 Python 环境用。
# compose 里 mihomo 的 healthcheck 调的是 .sh 那一份。
for _hc in healthcheck.sh healthcheck.py; do
    if [ -f "$SRC_DIR/mihomo/$_hc" ]; then
        cp "$SRC_DIR/mihomo/$_hc" "$DST_DIR/$_hc"
        chmod +x "$DST_DIR/$_hc"
        log "已同步 $_hc"
    fi
done

# ---- 地理数据也每次同步 ----
#
# 为什么必须拷：配置里所有 GEOIP/GEOSITE 规则都要这两份数据，缺了 mihomo
# 启动会报 "geoip.dat not found"，并把每条 GEOIP 规则静默当成不匹配 ——
# 表现是"规则看起来在，实际全走 MATCH 兜底"，很难查。
#
# 为什么卷里没有：本镜像（或 importer 镜像）把它放在
# /usr/local/share/mihomo/，而 mihomo 的 -d 指向挂载卷
# /root/.config/mihomo —— 卷挂上去会把镜像里那份遮掉，所以每次启动都补。
#
# metadb 是新版 mihomo 的默认 geoip 格式（geoip.dat 是老格式），两份都拷，
# 由配置文件里 geoip 相关字段决定用哪个。
GEO_SRC="${MIHOMO_GEO_SRC_DIR:-/usr/local/share/mihomo}"
_geo_copied=0
for _geo in geoip.dat geosite.dat geoip.metadb; do
    if [ -f "$GEO_SRC/$_geo" ]; then
        cp "$GEO_SRC/$_geo" "$DST_DIR/$_geo"
        _geo_copied=$((_geo_copied + 1))
    fi
done
if [ "$_geo_copied" -gt 0 ]; then
    log "已同步地理数据 $_geo_copied 份"
else
    log "! 未找到地理数据（$GEO_SRC），GEOIP/GEOSITE 规则会全部不匹配"
fi

# ---- 已有配置且未要求重建：保留 ----
if [ -f "$CONFIG" ] && [ -z "${MIHOMO_FORCE_REBUILD:-}" ]; then
    log "配置已存在，保留不覆盖（要重建设 MIHOMO_FORCE_REBUILD=1）"
    log "  $CONFIG"
    exit 0
fi

if [ -f "$CONFIG" ]; then
    BAK="$CONFIG.backup-$(date +%Y%m%d-%H%M%S)"
    cp "$CONFIG" "$BAK"
    log "MIHOMO_FORCE_REBUILD 已设，原配置备份到 $(basename "$BAK")"
fi

[ -n "${MIHOMO_SECRET:-}" ] || \
    log "! 未设 MIHOMO_SECRET，9090 API 将不设鉴权 —— 同一 Docker 网络内的任何容器都能切换出口、读出全部节点地址"

# ---- 物化 ----
# 订阅来源有两种写法，python 侧统一处理：
#   MIHOMO_SUBSCRIPTIONS="wog=https://a;wogb=https://b"  （分号或换行分隔）
#   MIHOMO_SUB_WOG=... / MIHOMO_SUB_WOGB=...             （每个订阅一个变量）
python3 - "$TEMPLATE" "$CONFIG" <<'PY'
import io, json, os, re, sys

tpl, out = sys.argv[1], sys.argv[2]
with io.open(tpl, encoding="utf-8") as f:
    text = f.read()


def parse_subscriptions():
    """收集订阅，返回 [(名称, URL), ...]。

    两种来源合并，MIHOMO_SUB_<名称> 优先（更具体）。名称统一转小写作为
    provider 键名 —— YAML 键大小写敏感，而环境变量名惯例是大写，不统一
    会让 MIHOMO_SUB_WOG 生成键 WOG 而模板里引用的是 wog。
    """
    subs = {}
    blob = os.environ.get("MIHOMO_SUBSCRIPTIONS", "")
    for chunk in re.split(r"[;\n]", blob):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        name, sep, url = chunk.partition("=")
        if sep and url.strip():
            subs[name.strip().lower()] = url.strip()
    for key, val in os.environ.items():
        if key.startswith("MIHOMO_SUB_") and val.strip():
            subs[key[len("MIHOMO_SUB_"):].lower()] = val.strip()
    return sorted(subs.items())


subs = parse_subscriptions()

# ---- 展开模板里的已知占位符 ----
KNOWN = {"MIHOMO_SECRET": os.environ.get("MIHOMO_SECRET", "")}
for name, url in subs:
    KNOWN["MIHOMO_SUB_%s" % name.upper()] = url


def expand(m):
    key = m.group(1)
    # 未知占位符原样保留：可能是用户自己加的模板变量，静默清空反而难查
    return KNOWN.get(key, m.group(0))


text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", expand, text)

# ---- 按实际订阅重写 proxy-providers / use 列表 / DIRECT 规则 ----
# 模板里预置的是 wog/wogb 两个示例。用户给了别的订阅名时，这三处都要跟着变，
# 否则生成的配置引用一堆不存在的 provider，mihomo 启动即报错。
if subs:
    # name 与 url **一次填完**，不再分两段（2026-09-16 从 importer 侧同步过来）。
    #
    # 原来的写法是两段式：这里先填 url、留一个字面 "%s" 给 name，调用方再
    # `hc_block(url) % name` 填第二次。于是 url 自己带的百分号会在第二次
    # 格式化时被当成格式符 ——
    #
    #   url = "https://x.example/sub?t=a%2Fb"
    #     → 第二次 % 把 "%2F" 读成「宽度 2 的 f 转换」
    #     → TypeError: must be real number, not str
    #
    # 而 `%2F` / `%3D` / `%3A` 在订阅链接里是常态（路径与 token 都要
    # percent-encode）。崩溃点在 init 容器里，表现是它非零退出、
    # `service_completed_successfully` 依赖不满足 —— **整个代理永不启动**。
    # 日志里只有一行 TypeError，与「订阅写错了」长得一样。
    # 实测 2026-09-19：本仓库这一份当时确实还是旧写法，已改成一次填完。
    #
    # 顺带把 url 从裸插值改成 json.dumps：YAML 的双引号串与 JSON 字符串
    # 转义规则一致，这样 url 里真的出现引号或反斜杠时也不会破坏结构。
    def hc_block(name, url):
        return (
            '    type: http\n'
            '    url: %s\n'
            '    path: ./providers/%s.yaml\n'
            '    interval: %s\n'
            '    health-check:\n'
            '      enable: true\n'
            '      url: "https://www.gstatic.com/generate_204"\n'
            '      interval: %s\n'
            '      lazy: false\n'
            % (json.dumps(url, ensure_ascii=False), name,
               os.environ.get("MIHOMO_PROVIDER_INTERVAL", "21600"),
               os.environ.get("MIHOMO_HC_INTERVAL", "300"))
        )

    lines = ["proxy-providers:"]
    for name, url in subs:
        lines.append("  %s:" % name)
        lines.append(hc_block(name, url))
    providers_block = "\n".join(lines).rstrip("\n") + "\n"

    # 替换整个 proxy-providers 段（到下一个顶级键为止）
    text = re.sub(
        r"^proxy-providers:.*?(?=^[^\s#])",
        providers_block,
        text,
        flags=re.MULTILINE | re.DOTALL,
    )

    # use: 列表 —— 两个组都要列全部订阅
    def fix_use(m):
        indent = m.group(1)
        return ("%suse:\n" % indent
                + "".join("%s  - %s\n" % (indent, n) for n, _ in subs))

    text = re.sub(
        r"^(\s+)use:\s*\n(?:\1\s+-\s+\S+\s*\n)+",
        fix_use,
        text,
        flags=re.MULTILINE,
    )

    # 订阅域名必须直连，否则"要先有代理才能拉到代理"死锁
    domains = []
    for _, url in subs:
        d = re.sub(r"^[a-z]+://", "", url)
        d = re.sub(r"[/?].*$", "", d)
        d = re.sub(r":\d+$", "", d)
        if d and d not in domains:
            domains.append(d)

    # 逐行重写 rules 段：模板里 rules: 与第一条 DIRECT 之间隔着注释，
    # 用 `^rules:\s*\n(?:\s+-\s+DOMAIN...)*` 这种"紧跟"的正则匹配不到那些
    # 规则，于是模板自带的示例域名（vpn.example.com 等）会和新生成的并存 ——
    # 实测踩到。改成扫描整个 rules 段，丢弃所有既有 DOMAIN,*,DIRECT 行
    # 再插入新的，注释与 MATCH 等其它规则原样保留。
    out_lines, in_rules, inserted = [], False, False
    for line in text.split("\n"):
        if re.match(r"^rules:\s*$", line):
            in_rules = True
            out_lines.append(line)
            continue
        if in_rules:
            # 顶级键（行首非空白且非注释）意味着 rules 段结束
            if re.match(r"^[^\s#]", line):
                in_rules = False
            else:
                # 丢弃旧的订阅 DIRECT 规则；其余（注释、MATCH）保留
                if re.match(r"^\s*-\s*DOMAIN,\S+,DIRECT\s*$", line):
                    if not inserted:
                        for d in domains:
                            out_lines.append("  - DOMAIN,%s,DIRECT" % d)
                        inserted = True
                    continue
        out_lines.append(line)

    # rules 段里原本一条 DOMAIN 规则都没有时，补在 MATCH 之前
    if not inserted and domains:
        final, done = [], False
        for line in out_lines:
            if not done and re.match(r"^\s*-\s*MATCH,", line):
                for d in domains:
                    final.append("  - DOMAIN,%s,DIRECT" % d)
                done = True
            final.append(line)
        out_lines = final

    text = "\n".join(out_lines)

with io.open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

leftover = set()
for line in text.split("\n"):
    if line.lstrip().startswith("#"):
        continue
    for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", line):
        leftover.add(m.group(1))

print("[mihomo-init] 已物化 %s" % out)
print("[mihomo-init] 订阅 %d 个: %s"
      % (len(subs), ", ".join(n for n, _ in subs) if subs else "(无，用模板默认)"))
if leftover:
    print("[mihomo-init] ! 未展开的占位符: %s —— 对应订阅未配置，"
          "mihomo 会因 URL 非法而启动失败" % ", ".join(sorted(leftover)))
    sys.exit(1)
PY

log "自举完成"
