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

# mihomo 自举脚本必须以 root 可执行的方式进镜像：
#   · mihomo-init 容器用它当 entrypoint 物化配置；
#   · 它在容器里以 root 跑（要写共享卷），所以单独修权限。
RUN chmod +x /app/mihomo-manager/*.sh /app/mihomo-manager/mihomo/healthcheck.py

# 创建输出目录
RUN mkdir -p /app/out && chown app:app /app/out

# 设置 PATH
ENV PATH=/home/app/.local/bin:$PATH

# 切换到非 root 用户
USER app

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# 默认命令（交互式菜单）
CMD ["python", "tool.py"]


# ====================================
# 构建说明
# ====================================
#
# 构建命令：
#   docker build -t hyskaamorroh/cpa2sub2api:latest .
#
# 多架构构建：
#   docker buildx build --platform linux/amd64,linux/arm64 \
#     -t hyskaamorroh/cpa2sub2api:latest --push .
#
# 本地测试：
#   docker run -it --rm \
#     -v $(pwd)/config.yaml:/app/config.yaml:ro \
#     -v $(pwd)/out:/app/out \
#     -e SUB2API_BASE_URL=https://api.example.com \
#     -e SUB2API_ADMIN_KEY=admin-xxx \
#     hyskaamorroh/cpa2sub2api:latest
#
# ====================================
