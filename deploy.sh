#!/bin/bash
# CPA2SUB2API 部署检查与启动
#
# 这个脚本只做三件事：检查前置条件、准备 .env、启动 compose。
# 它**不**负责准备 mihomo 配置 —— mihomo 的配置由 mihomo-init 容器
# 从镜像内的模板物化，宿主不需要预置任何文件。

set -e

echo "=== CPA2SUB2API 部署 ==="
echo ""

# ---- 前置检查 ----
if ! command -v docker >/dev/null 2>&1; then
    echo "✗ 未安装 docker"
    exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
    echo "✗ 未安装 docker compose（v2）"
    echo "  注意：旧版的 docker-compose（带连字符）本脚本不支持"
    exit 1
fi

# ---- .env ----
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "⚠ 已从 .env.example 生成 .env，请填写后再跑一次："
        echo "    SUB2API_BASE_URL="
        echo "    SUB2API_ADMIN_KEY="
        echo "    FALLBACK_PROXY=http://mihomo:7890"
        exit 0
    fi
    echo "✗ 缺少 .env 与 .env.example"
    exit 1
fi

if ! grep -q '^SUB2API_BASE_URL=.\+' .env; then
    echo "✗ .env 里 SUB2API_BASE_URL 未填写"
    exit 1
fi
if ! grep -q '^SUB2API_ADMIN_KEY=.\+' .env; then
    echo "✗ .env 里 SUB2API_ADMIN_KEY 未填写"
    exit 1
fi

# ---- config.yaml ----
# 两种来源：本地文件，或通过 CPA_BASE_URL 在线拉。
if [ ! -f config.yaml ]; then
    if grep -q '^CPA_BASE_URL=.\+' .env; then
        echo "· 本地无 config.yaml，将经 CPA_BASE_URL 在线拉取"
    else
        echo "✗ 既没有 config.yaml，也没有配 CPA_BASE_URL"
        exit 1
    fi
fi

# ---- 代理 ----
USE_PROFILE=""
if grep -q '^MIHOMO_SUBSCRIPTIONS=.\+\|^MIHOMO_SUB_' .env; then
    USE_PROFILE="--profile mihomo"
    echo "· 检测到订阅配置，将一并启动 mihomo"
else
    echo "· 未配置订阅，只启动本工具"
fi

# ---- 启动 ----
echo ""
echo "启动中..."
docker compose $USE_PROFILE up -d

echo ""
echo "✓ 完成"
echo ""
echo "后续："
echo "  执行导入      docker compose run --rm cpa2sub2api python 一键导入.py"
echo "  交互菜单      docker compose run --rm cpa2sub2api python run.py"
echo "  查看日志      docker compose logs -f cpa2sub2api"
echo "  查看产物      ls -lh out/"
if [ -n "$USE_PROFILE" ]; then
    echo "  代理验证      curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \\"
    echo "                  -w '%{http_code}\\n' https://www.gstatic.com/generate_204"
    echo ""
    echo "  ⚠ 若代理不通，先查 AUTO 组节点数（为 0 就是静默失效）："
    echo "     curl -fsS -H \"Authorization: Bearer \$MIHOMO_SECRET\" \\"
    echo "       http://127.0.0.1:9090/proxies/AUTO | jq '.all|length'"
fi
