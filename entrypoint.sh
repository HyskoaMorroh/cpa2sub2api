#!/bin/sh
# cpa2sub2api 容器入口。
#
# 两种运行模式，由 RUN_INTERVAL_SECONDS 决定：
#
#   未设 / 0   跑一次导入就退出。
#              这是 `docker compose up -d` 的默认行为 —— 容器起来、同步一次、
#              退出。配合 `restart: "no"` 就是"启动即同步"的语义。
#
#   > 0        常驻，每 RUN_INTERVAL_SECONDS 秒同步一次。
#              要让它在 up -d 后一直活着，compose 里必须配
#              `restart: unless-stopped`，否则跑完第一轮退出后不会重来。
#
# 也支持用 RUN_ONCE=1 显式指定"只跑一次"（此时忽略 RUN_INTERVAL_SECONDS）。
#
# 另外，宿主 cron 也可以直接调 `docker compose run --rm cpa2sub2api`
# 来做定期同步 —— 那条路径不经过本脚本的循环逻辑，等价于"跑一次"。

set -u

log() { echo "[cpa2sub2api] $*"; }

# 按脚本自身位置推导应用目录，而不是硬编码 /app。
# 容器里它在 /app，推导结果就是 /app；在宿主上直接执行时也能正确工作，
# 因而这个入口脚本本身是可测的（不必先构建镜像）。
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---- 前置检查：告诉用户缺什么，别让人对着一段 traceback 猜 ----
if [ -z "${SUB2API_BASE_URL:-}" ]; then
    log "错误：未设置 SUB2API_BASE_URL"
    log "  容器部署请在 .env 里设成服务名，例如："
    log "      SUB2API_BASE_URL=http://sub2api:8080"
    log "  注意：容器里的 127.0.0.1 是它自己，不是 sub2api，也不是宿主机。"
    exit 1
fi
if [ -z "${SUB2API_ADMIN_KEY:-}" ]; then
    log "错误：未设置 SUB2API_ADMIN_KEY（sub2api 的管理员密钥）"
    exit 1
fi

# config.yaml 可选：没有就走 CPA_BASE_URL 在线拉。
if [ ! -r "$APP_DIR/config.yaml" ]; then
    if [ -n "${CPA_BASE_URL:-}" ]; then
        log "本地无 config.yaml，将经 CPA_BASE_URL 在线拉取：${CPA_BASE_URL}"
    else
        log "错误：既没有 $APP_DIR/config.yaml，也没有设置 CPA_BASE_URL"
        log "  容器部署请确认 compose 里有这一行挂载："
        log "      - ./config.yaml:/app/config.yaml:ro,Z"
        log "  宿主上该文件不存在时，Docker 会**建一个同名空目录**顶上去，"
        log "  表现为「文件存在但读不了」——用 ls -ld 确认它是文件而不是目录。"
        exit 1
    fi
fi

INTERVAL="${RUN_INTERVAL_SECONDS:-0}"

run_once() {
    log "开始同步（$(date '+%Y-%m-%d %H:%M:%S')）"
    # 用 一键导入.py 而不是 tool.py：后者是交互菜单，没有 TTY 时直接 EOF 退出。
    #
    # ASSUME_YES=1 是必需的：导入前有一道人工确认（"将向 xxx 写入 N 条账号，
    # 输入 y 开始"），非交互环境下它读不到输入会按「否」处理 —— 于是整个流程
    # 跑完、打印"已取消，什么都没写"、**退出码还是 0**，看起来成功实则没写数据。
    # 容器/cron 里没有人能敲 y，所以这里显式开启自动确认。
    # 注意它只在导入那一处生效，不会顺带授权「全部清空」那个破坏性操作。
    ( cd "$APP_DIR" && ASSUME_YES=1 python "$APP_DIR/一键导入.py" )
    rc=$?
    if [ "$rc" -eq 0 ]; then
        log "同步完成"
    else
        log "同步失败，退出码 $rc"
    fi
    return $rc
}

if [ -n "${RUN_ONCE:-}" ] || [ "$INTERVAL" -le 0 ] 2>/dev/null; then
    run_once
    exit $?
fi

# ---- 常驻循环模式 ----
case "$INTERVAL" in
    ''|*[!0-9]*)
        log "RUN_INTERVAL_SECONDS 不是合法整数（'$INTERVAL'），按跑一次处理"
        run_once
        exit $?
        ;;
esac

log "已启用定时同步：每 ${INTERVAL} 秒一次"
log "提示：这个模式下 compose 必须配 restart: unless-stopped，否则不会重来"

while :; do
    run_once || log "本轮失败，等下一轮继续"
    sleep "$INTERVAL"
done
