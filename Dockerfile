# ====================================
# Dockerfile for CPA2SUB2API
# ====================================
#
# 多阶段构建，最小化镜像体积
# 支持多架构：linux/amd64, linux/arm64
#
# ====================================

# ====================================
# 阶段 1: 基础镜像
# ====================================
FROM python:3.11-slim as base

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git && \
    rm -rf /var/lib/apt/lists/*


# ====================================
# 阶段 2: 依赖安装
# ====================================
FROM base as builder

# 设置工作目录
WORKDIR /build

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --user --no-warn-script-location -r requirements.txt


# ====================================
# 阶段 3: 运行时镜像
# ====================================
FROM base as runtime

# 创建非 root 用户
RUN useradd -m -u 1000 -s /bin/bash app

# 设置工作目录
WORKDIR /app

# 从 builder 复制已安装的依赖
COPY --from=builder /root/.local /home/app/.local

# 复制应用代码
COPY --chown=app:app . .

# mihomo 自举脚本与容器入口都必须以可执行的方式进镜像：
#   · mihomo-init 容器用 bootstrap-mihomo.sh 当 entrypoint；
#   · 本容器的 CMD 是 entrypoint.sh。
#   · healthcheck.sh 是 **mihomo 容器**的健康检查脚本（纯 busybox shell，
#     因为 metacubex/mihomo 镜像里没有 python3/curl）。它由
#     bootstrap-mihomo.sh materialize 到命名卷，再被 compose 的
#     healthcheck: sh /root/.config/mihomo/healthcheck.sh 调用。
# 显式 chmod 一遍，不依赖宿主文件系统的执行位（Windows 上 checkout 出来的
# 文件往往没有 x 位，靠 git 记录不可靠）。
RUN chmod +x /app/entrypoint.sh \
             /app/mihomo-manager/*.sh \
             /app/mihomo-manager/mihomo/healthcheck.py \
             /app/mihomo-manager/mihomo/healthcheck.sh

# 创建输出目录
RUN mkdir -p /app/out && chown app:app /app/out

# 设置 PATH
ENV PATH=/home/app/.local/bin:$PATH

# 切换到非 root 用户
USER app

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# 默认命令：**非交互**地把 config.yaml 同步进 sub2api。
#
# 为什么不是 `python tool.py`：那是交互式菜单。容器起来后它只会打印菜单
# 然后等输入 —— 没有 TTY 时立刻 EOF 退出，什么活都不干。于是
# `docker compose up -d` 看起来"成功"，实际一次都没同步，
# 必须人工 `docker compose run ...` 才有效果。这违背了
# 「up -d 之后就应该自动跑完」的预期。
#
# 一键导入.py 已经正确处理非交互环境（读输入遇 EOF 时按「回车」处理 = 执行导入），
# 所以它可以安全地当容器默认命令。
#
# 想要定时重复执行，设 RUN_INTERVAL_SECONDS（见 entrypoint.sh）：
#     0 或未设 = 跑一次就退出（默认，适合 up -d 同步一次）
#     3600     = 每小时再跑一次
CMD ["/app/entrypoint.sh"]


# ====================================
# 构建说明
# ====================================
#
# 构建命令：
#   docker build -t your-dockerhub-user/cpa2sub2api:latest .
#
# 多架构构建：
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t your-dockerhub-user/cpa2sub2api:latest --push .
#
# 本地测试：
#   docker run -it --rm \
#     -v $(pwd)/config.yaml:/app/config.yaml:ro \
#     -v $(pwd)/out:/app/out \
#     -e SUB2API_BASE_URL=https://api.example.com \
#     -e SUB2API_ADMIN_KEY=admin-xxx \
#     your-dockerhub-user/cpa2sub2api:latest
#
# ====================================
