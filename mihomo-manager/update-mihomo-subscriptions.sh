#!/bin/bash
# Mihomo 订阅管理脚本
#
# 职责：按 mihomo-subscriptions.conf 重写 config.yaml 的 proxy-providers、
#       proxy-groups 的 use 列表、以及订阅域名的 DIRECT 规则。
#       其余段落（dns / rules 的 MATCH / AUTO 的 filter 等）一律不动。
#
# 与 probe-upstreams.py 的分工：本脚本管**订阅**，它管**节点筛选**。
#
# 目录约定：
#   mihomo-manager/
#     ├─ mihomo-subscriptions.conf      订阅清单（含 token，手工编辑，不进版本库）
#     ├─ update-mihomo-subscriptions.sh 本脚本
#     └─ mihomo/                        mihomo 的配置目录，被容器挂载
#         ├─ config.mihomo.yaml         模板（进版本库/镜像，私密值一律 ${VAR}）
#         ├─ config.yaml                实际配置（由模板生成，不进版本库）
#         ├─ healthcheck.py             容器健康检查 + 出口降级
#         └─ providers/                 订阅缓存，mihomo 自己写
#
# 模板名叫 config.mihomo.yaml 而不是 config.template.yaml：上游
# .dockerignore 有一条 `config.*.yaml`，会把后者从镜像构建上下文里排除。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONF_FILE="$SCRIPT_DIR/mihomo-subscriptions.conf"
MIHOMO_DIR="$SCRIPT_DIR/mihomo"
CONFIG_FILE="$MIHOMO_DIR/config.yaml"
# 模板文件名（2026-09-13 修）：脚本原来找 config.mihomo.yaml，而磁盘上是
# config.template.yaml —— 于是 config.yaml 不存在时不是"从模板生成"，而是
# 直接报"配置与模板都不存在"退出。首次部署必然踩到。
TEMPLATE_FILE="$MIHOMO_DIR/config.template.yaml"
# 兼容旧文件名：老部署里可能还叫 config.mihomo.yaml
[ -f "$TEMPLATE_FILE" ] || [ ! -f "$MIHOMO_DIR/config.mihomo.yaml" ] || \
    TEMPLATE_FILE="$MIHOMO_DIR/config.mihomo.yaml"
# 部署根：docker-compose.yml 与 .env 所在的目录。
# 不写死层级（原先固定 ../..）—— 这个脚本可能被放在不同深度，或在测试
# 环境里少一层，固定相对路径会静默算到错误目录，.env 读不到就退化成
# "不设 secret"，而那恰好不报错。改成从脚本目录向上逐级找 compose 文件。
find_deploy_root() {
    local d="$SCRIPT_DIR"
    local i=0
    while [ "$i" -lt 6 ]; do
        d="$(cd "$d/.." && pwd)"
        if [ -f "$d/docker-compose.yml" ] || [ -f "$d/docker-compose.yaml" ] \
           || [ -f "$d/compose.yml" ] || [ -f "$d/compose.yaml" ]; then
            printf '%s' "$d"; return 0
        fi
        [ "$d" = "/" ] && break
        i=$((i + 1))
    done
    # 找不到就退回固定层级，行为与旧版一致
    printf '%s' "$(cd "$SCRIPT_DIR/../.." && pwd)"
}
DEPLOY_DIR="$(find_deploy_root)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

[ -f "$CONF_FILE" ] || { log_error "订阅配置不存在: $CONF_FILE"; exit 1; }

# ---- 读 .env，供模板变量展开 ----
# 模板里的 ${MIHOMO_SECRET} / ${MIHOMO_SUB_*} 需要真值。优先用进程环境
# （compose 从 .env 注入），没有再从部署根的 .env 读。
ENV_FILE="$DEPLOY_DIR/.env"
env_get() {   # $1=键名
    local v
    v="${!1:-}"
    if [ -z "$v" ] && [ -f "$ENV_FILE" ]; then
        v=$(grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true)
        # 去掉包裹的引号
        v=$(printf '%s' "$v" | sed -E 's/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/')
    fi
    printf '%s' "$v"
}

# ---- config.yaml 不存在（或强制重建）时从模板生成 ----
if [ ! -f "$CONFIG_FILE" ]; then
    [ -f "$TEMPLATE_FILE" ] || {
        log_error "配置与模板都不存在: $CONFIG_FILE / $TEMPLATE_FILE"; exit 1; }

    SECRET_VAL=$(env_get MIHOMO_SECRET)
    if [ -z "$SECRET_VAL" ]; then
        log_warn "未读到 MIHOMO_SECRET，API 将不设鉴权（旧行为，同网段容器可直连 9090）"
    fi

    log_info "config.yaml 不存在，从模板生成"
    # 展开 ${VAR}。用 python 而非 envsubst：后者不一定装在部署机上。
    # 只展开模板里显式写出的键，不做通用环境变量替换，避免误伤 YAML 里
    # 形如 ${...} 的正则或注释。
    MIHOMO_SECRET="$SECRET_VAL" \
    MIHOMO_SUB_WOG="$(env_get MIHOMO_SUB_WOG)" \
    MIHOMO_SUB_WOGB="$(env_get MIHOMO_SUB_WOGB)" \
    python3 - "$TEMPLATE_FILE" "$CONFIG_FILE" <<'PY'
import io, os, re, sys

tpl, out = sys.argv[1], sys.argv[2]
with io.open(tpl, encoding="utf-8") as f:
    text = f.read()

KNOWN = ("MIHOMO_SECRET", "MIHOMO_SUB_WOG", "MIHOMO_SUB_WOGB")

def sub(m):
    key = m.group(1)
    if key not in KNOWN:
        # 非本工具管理的占位符，原样保留（可能是用户自己写的模板变量）
        return m.group(0)
    return os.environ.get(key, "")

text = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", sub, text)

with io.open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

# 只看**非注释行**里的残留占位符。模板注释里会出现 ${VAR} 这样的说明性
# 写法，把它算成"未展开"是误报。
leftover = set()
for line in text.split("\n"):
    if line.lstrip().startswith("#"):
        continue
    for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", line):
        if m.group(1) not in KNOWN:
            leftover.add(m.group(1))
if leftover:
    print("[WARN] 模板里还有未展开的占位符: %s" % ", ".join(sorted(leftover)))
PY
else
    # 已存在则沿用现有值（含 secret），只重写订阅相关段落。
    log_info "config.yaml 已存在，沿用现有 secret 与调优参数"
fi

log_info "开始更新订阅配置..."
log_info "  配置文件: $CONFIG_FILE"
log_info "  部署目录: $DEPLOY_DIR"

# ---- 备份 ----
BACKUP_FILE="${CONFIG_FILE}.backup-$(date +%Y%m%d-%H%M%S)"
cp "$CONFIG_FILE" "$BACKUP_FILE"
log_info "已备份到: $(basename "$BACKUP_FILE")"

# ---- 读订阅清单 ----
# 名称=URL，等号分隔。URL 里可能带 ?token=xxx（含等号），所以只按**第一个**
# 等号切分：IFS='=' read -r a b 会把后续等号留在 b 里，正合需要。
declare -a NAMES=() URLS=() DOMAINS=()
while IFS='=' read -r name url; do
    [[ "$name" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${name// }" ]] && continue
    name="$(echo "$name" | xargs)"
    url="$(echo "$url" | xargs)"
    [ -z "$url" ] && { log_warn "跳过无 URL 的行: $name"; continue; }
    NAMES+=("$name"); URLS+=("$url")
    # 提取域名用于 DIRECT 规则
    DOMAINS+=("$(echo "$url" | sed -E 's#^[a-z]+://##; s#[/?].*$##; s#:[0-9]+$##')")
done < "$CONF_FILE"

[ ${#NAMES[@]} -gt 0 ] || { log_error "订阅清单为空"; exit 1; }
log_info "读到 ${#NAMES[@]} 个订阅: ${NAMES[*]}"

# ---- 从现有配置里读回 health-check 参数，避免每次跑脚本把手工调过的值打回默认 ----
# 原脚本硬写 interval: 86400 / health-check interval: 600 且丢掉 lazy，
# 于是"跑一次脚本就覆盖调优结果"。这里以现有值为准，读不到才用默认。
read_existing() {   # $1=键名 $2=默认值
    local v
    v=$(grep -m1 -E "^[[:space:]]+$1:" "$CONFIG_FILE" 2>/dev/null \
        | sed -E "s/^[[:space:]]+$1:[[:space:]]*//; s/[[:space:]]*(#.*)?$//") || true
    echo "${v:-$2}"
}
PROVIDER_INTERVAL=$(read_existing "interval" "21600")
HC_LAZY=$(read_existing "lazy" "false")

# health-check 块内的 url / interval 必须在块内定位，不能用 grep -m1 取
# "文件里第一个"：provider 自己的 url（订阅地址）出现得更早，会被误取成
# 探测地址 —— 那样健康检查等于去 GET 订阅链接，判活彻底失效（实测踩到）。
read_in_hc() {   # $1=键名 $2=默认值
    local v
    v=$(awk -v key="$1" '
        /^[[:space:]]+health-check:[[:space:]]*$/ { inhc = 1; next }
        inhc {
            # 块内：缩进比 health-check 更深才算；遇到同级或更浅的键就出块
            if ($0 ~ /^[[:space:]]*$/) next
            if ($0 !~ /^[[:space:]]{6,}/) { inhc = 0; next }
            if ($0 ~ "^[[:space:]]+" key ":") {
                sub("^[[:space:]]+" key ":[[:space:]]*", "")
                sub(/[[:space:]]*(#.*)?$/, "")
                print; exit
            }
        }
    ' "$CONFIG_FILE" 2>/dev/null) || true
    echo "${v:-$2}"
}
HC_URL=$(read_in_hc "url" '"https://www.gstatic.com/generate_204"')
HC_INTERVAL=$(read_in_hc "interval" "300")

# 探测地址若被写成订阅地址（历史缺陷留下的坏配置），纠正回默认值。
case "$HC_URL" in
    *generate_204*|*204*) : ;;
    *)
        log_warn "health-check.url 看起来不是探活地址（$HC_URL），已纠正为 generate_204"
        HC_URL='"https://www.gstatic.com/generate_204"'
        ;;
esac
log_info "沿用现有参数: provider interval=$PROVIDER_INTERVAL, health-check interval=$HC_INTERVAL, lazy=$HC_LAZY"

# ---- 生成 proxy-providers 块 ----
PROVIDERS_YAML="$(mktemp)"
trap 'rm -f "$PROVIDERS_YAML" "${PROVIDERS_YAML}.groups" "${PROVIDERS_YAML}.out"' EXIT

{
    echo "proxy-providers:"
    for i in "${!NAMES[@]}"; do
        cat <<EOF
  ${NAMES[$i]}:
    type: http
    url: "${URLS[$i]}"
    path: ./providers/${NAMES[$i]}.yaml
    interval: ${PROVIDER_INTERVAL}
    health-check:
      enable: true
      url: ${HC_URL}
      interval: ${HC_INTERVAL}
      lazy: ${HC_LAZY}

EOF
    done
} > "$PROVIDERS_YAML"

# ---- 替换 proxy-providers 块 ----
# 用 awk 按"顶级键"边界替换。原实现有个 EOF 边界缺陷：跳过旧块时
# `while (getline > 0)` 若一路读到文件末尾都没遇到顶级键（proxy-providers
# 是最后一个块的情况），循环因 getline 返回 0 退出，**后面的内容全被丢掉**。
# 当前配置里它后面跟着 proxy-groups 所以没炸，属于运气。这里改成单趟状态机：
# 只在"处于旧块内"时丢弃行，遇到顶级键立刻回到正常输出，天然不依赖后面还有内容。
awk -v pf="$PROVIDERS_YAML" '
BEGIN { inblock = 0 }
/^proxy-providers:[[:space:]]*$/ {
    while ((getline line < pf) > 0) print line
    close(pf)
    inblock = 1
    next
}
{
    if (inblock) {
        # 顶级键 = 行首非空白且不是注释；碰到就结束丢弃并正常输出本行
        if ($0 ~ /^[^[:space:]#]/) { inblock = 0; print }
        # 否则仍在旧块内，丢弃
        next
    }
    print
}
' "$CONFIG_FILE" > "${PROVIDERS_YAML}.out"

# 校验替换结果非空且仍含关键段，避免把配置写坏
for key in "proxy-providers:" "proxy-groups:" "rules:"; do
    grep -q "^$key" "${PROVIDERS_YAML}.out" || {
        log_error "替换后缺失 $key，已放弃写入（原配置未改动）"; exit 1; }
done
mv "${PROVIDERS_YAML}.out" "$CONFIG_FILE"

# ---- 同步 proxy-groups 的 use 列表 ----
# PROXY 与 AUTO 两组的 use 都应列出全部订阅。用 python 做块级替换：
# use 列表的边界判断在 awk/sed 里很容易写错，而 python3 在部署机上已有
# （upstream-importer 本身就是 python 项目）。
python3 - "$CONFIG_FILE" "${NAMES[@]}" <<'PY'
import io, re, sys

path, names = sys.argv[1], sys.argv[2:]
with io.open(path, encoding="utf-8") as f:
    lines = f.read().split("\n")

out, i = [], 0
while i < len(lines):
    line = lines[i]
    out.append(line)
    m = re.match(r"^(\s+)use:\s*$", line)
    if not m:
        i += 1
        continue
    indent = m.group(1)
    # 跳过原有的 - xxx 条目
    i += 1
    while i < len(lines) and re.match(r"^%s\s+-\s" % re.escape(indent), lines[i]):
        i += 1
    for n in names:
        out.append("%s  - %s" % (indent, n))

with io.open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(out))
print("[INFO] proxy-groups.use 已同步为: %s" % ", ".join(names))
PY

# ---- 同步订阅域名的 DIRECT 规则 ----
# "要先有代理才能拉到代理"的死锁必须靠这些规则避免。
python3 - "$CONFIG_FILE" "${DOMAINS[@]}" <<'PY'
import io, re, sys

path, domains = sys.argv[1], sys.argv[2:]
with io.open(path, encoding="utf-8") as f:
    lines = f.read().split("\n")

out, in_rules = [], False
for line in lines:
    if re.match(r"^rules:\s*$", line):
        in_rules = True
        out.append(line)
        for d in domains:
            out.append("  - DOMAIN,%s,DIRECT" % d)
        continue
    if in_rules:
        # 丢掉旧的订阅 DIRECT 规则，保留 MATCH 及其它自定义规则
        if re.match(r"^\s+-\s*DOMAIN,\S+,DIRECT\s*$", line):
            continue
        if re.match(r"^[^\s#]", line):
            in_rules = False
    out.append(line)

with io.open(path, "w", encoding="utf-8", newline="\n") as f:
    f.write("\n".join(out))
print("[INFO] 订阅域名 DIRECT 规则已同步: %s" % ", ".join(domains))
PY

# ---- 语法校验：写坏了立刻回滚，别等容器起不来 ----
log_info "校验配置语法..."
if command -v docker >/dev/null 2>&1; then
    if docker run --rm -v "$MIHOMO_DIR:/root/.config/mihomo:Z" \
            metacubex/mihomo:latest -t >/dev/null 2>&1; then
        log_info "✓ 配置语法正确"
    else
        log_error "配置语法校验失败，回滚到备份"
        cp "$BACKUP_FILE" "$CONFIG_FILE"
        docker run --rm -v "$MIHOMO_DIR:/root/.config/mihomo:Z" \
            metacubex/mihomo:latest -t 2>&1 | tail -20
        exit 1
    fi
else
    log_warn "docker 不可用，跳过语法校验"
fi

# ---- 清理旧订阅缓存，强制重新拉取 ----
for n in "${NAMES[@]}"; do
    rm -f "$MIHOMO_DIR/providers/${n}.yaml"
done
log_info "已清理订阅缓存"

# ---- 重启容器 ----
log_info "重启 mihomo 容器..."
cd "$DEPLOY_DIR"
if docker compose version >/dev/null 2>&1; then
    docker compose restart mihomo
else
    docker-compose restart mihomo
fi

log_info "等待 mihomo 拉取订阅（15 秒）..."
sleep 15

# ---- 验证 ----
# API 已启用 secret，请求必须带 Authorization。secret 从部署目录的 .env 读，
# 与 compose 注入容器的是同一个值。
SECRET=""
if [ -f "$DEPLOY_DIR/.env" ]; then
    SECRET=$(grep -m1 '^MIHOMO_SECRET=' "$DEPLOY_DIR/.env" 2>/dev/null \
        | cut -d= -f2- | tr -d '"'"'"'' ) || true
fi
if [ -n "$SECRET" ]; then
    AUTH=(-H "Authorization: Bearer $SECRET")
else
    AUTH=()
    log_warn "未从 .env 读到 MIHOMO_SECRET，验证请求不带鉴权（若已设 secret 会 401）"
fi

echo "-----------------------------------"
SUCCESS=0; FAILED=0
for n in "${NAMES[@]}"; do
    RESP=$(curl -fsS -m 10 "${AUTH[@]+"${AUTH[@]}"}" \
        "http://127.0.0.1:9090/providers/proxies/$n" 2>/dev/null || echo '')
    if [ -n "$RESP" ]; then
        CNT=$(printf '%s' "$RESP" | grep -o '"name"' | wc -l | tr -d ' ')
        log_info "✓ $n: 约 $CNT 个节点"
        SUCCESS=$((SUCCESS + 1))
    else
        log_error "✗ $n: 拉取失败"
        FAILED=$((FAILED + 1))
    fi
done
echo "-----------------------------------"

# AUTO 组可用节点数。为 0 说明 filter 把节点全筛掉了 —— mihomo 对此不报错，
# 必须在这里显式告警，否则表现为"代理静默失效"。
AUTO_RESP=$(curl -fsS -m 10 "${AUTH[@]+"${AUTH[@]}"}" \
    "http://127.0.0.1:9090/proxies/AUTO" 2>/dev/null || echo '')
if [ -n "$AUTO_RESP" ]; then
    AUTO_CNT=$(printf '%s' "$AUTO_RESP" \
        | sed -n 's/.*"all":\[\([^]]*\)\].*/\1/p' | tr ',' '\n' | grep -c '"' || echo 0)
    AUTO_NOW=$(printf '%s' "$AUTO_RESP" | sed -n 's/.*"now":"\([^"]*\)".*/\1/p')
    if [ "$AUTO_CNT" -eq 0 ]; then
        log_error "✗ AUTO 组 0 个可用节点！检查 config.yaml 的 filter 是否筛掉了全部节点"
        FAILED=$((FAILED + 1))
    else
        log_info "AUTO 组 $AUTO_CNT 个节点，当前使用: ${AUTO_NOW:-未知}"
    fi
else
    log_warn "读不到 AUTO 组状态（secret 不对或 API 未就绪）"
fi

# 实测一次出网，这是唯一能证明"代理真的能用"的检查
if curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \
        "https://www.gstatic.com/generate_204" 2>/dev/null; then
    log_info "✓ 经 7890 出网成功"
else
    log_error "✗ 经 7890 出网失败 —— 节点已加载但不可用，或全部被目标站拦截"
    FAILED=$((FAILED + 1))
fi

echo ""
if [ $FAILED -eq 0 ]; then
    log_info "✓ 订阅更新完成，$SUCCESS 个订阅全部就绪"
else
    log_warn "⚠ 完成但有 $FAILED 项异常，查看日志: docker logs mihomo --tail 50"
    exit 1
fi
