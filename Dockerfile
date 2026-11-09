# ====================================
# Dockerfile for CPA2SUB2API
# ====================================
#
# 多阶段构建，最小化镜像体积
# 支持多架构：linux/amd64, linux/arm64
#
# 本镜像自带 mihomo（代理内核）。为什么把代理二进制打进来而不是另起一个
# 官方镜像跑：部署方式是「VPS 只拉镜像 + 只上传 .env / config.yaml /
# docker-compose.yml / nginx.conf 四个文件」，宿主上不预置任何 mihomo 相关
# 文件。但 mihomo 官方镜像基于 scratch（没有 shell、没有 python），它自己
# 没法把 config.template.yaml 展开成 config.yaml（要填订阅 URL 与 secret）。
# 所以由本镜像充当 init 与运行两个角色：
#   · `entrypoint: sh /app/mihomo-manager/bootstrap-mihomo.sh` → 物化配置进卷
#   · `entrypoint: mihomo -d /root/.config/mihomo`            → 长驻代理
# 这样「上传四个配套部署文件就够」这个约束才成立。
#
# mihomo 是 Go 编的纯静态二进制（实测 file 显示 static，不 link musl 运行库），
# 所以从 metacubex/mihomo（Alpine）COPY 到本镜像的 Debian/glibc 基座上能直接跑
# —— 2026-09-19 实测 `mihomo -v` 输出 v1.19.31，`-t` 语法检查 rc=0，
# 并且以 uid 1000 非 root 运行也正常（会往 -d 目录写 cache.db）。
#
# ====================================

# ====================================
# 阶段 0: 代理内核来源
# ====================================
#
# **必须声明在第一个 FROM 之前**：只有"全局 ARG"（位于任何 FROM 之上）
# 才能被后面的 FROM 引用；写在 FROM 之后的 ARG 属于那个 stage 的局部作用域，
# 对下一个 FROM 不可见，构建会报 "base name should not be blank"。
ARG MIHOMO_IMAGE=metacubex/mihomo@sha256:9211361a3ed85305383288d38c05e85862d6d5a590c10007c37fc059cca82e9c
ARG PYTHON_IMAGE=python@sha256:65bd3d9f1d7fa268dd1909973ba7eb4bdf41f6dcf2cfbe76ba9528b170f3fd79
ARG CPA_CONSUMER_BUILD_IMAGE=golang:1.26.10-bookworm@sha256:5de7c99eb78ca5e515a4c340235d0955ec5964a5071c492c15503d7c267da473

FROM ${MIHOMO_IMAGE} AS mihomo

# The relay preserves the pinned native executor and its selected wire policy.
# This project-local patch only disables redirects for its diagnostic auth
# namespaces. Source/patch/build/binary evidence is included in the runtime.
FROM ${CPA_CONSUMER_BUILD_IMAGE} AS cpa-consumer-source
ARG CPA_CONSUMER_BUILD_IMAGE
ENV CPA_CONSUMER_BUILD_IMAGE=${CPA_CONSUMER_BUILD_IMAGE} GOTOOLCHAIN=local
WORKDIR /build/consumer-src
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git python3 && \
    rm -rf /var/lib/apt/lists/*
RUN git init . && git config core.autocrlf false && \
    git remote add origin https://github.com/router-for-me/CLIProxyAPI.git && \
    git fetch --depth 1 origin 91e0dee827abf476f9f86246f68c500a112f7155 && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = 91e0dee827abf476f9f86246f68c500a112f7155
COPY Dockerfile /build/patches/Dockerfile
COPY .dockerignore /build/patches/.dockerignore
COPY requirements.txt /build/patches/requirements.txt
COPY third_party/patches/consumer_patch_manifest.json /build/patches/consumer_patch_manifest.json
COPY third_party/patches/apply_consumer_patch.py /build/patches/apply_consumer_patch.py
COPY third_party/patches/diagnostic_redirect.go /build/patches/diagnostic_redirect.go
COPY third_party/patches/diagnostic_redirect_test.go /build/patches/diagnostic_redirect_test.go
RUN --mount=type=cache,target=/go/pkg/mod go mod download

FROM cpa-consumer-source AS cpa-consumer
RUN python3 /build/patches/apply_consumer_patch.py apply
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go test ./internal/runtime/executor/helps -run '^TestCPA2Sub2APIDiagnostic' -count=1
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    python3 /build/patches/apply_consumer_patch.py build && \
    python3 /build/patches/apply_consumer_patch.py provenance

# ====================================
# 阶段 1: 基础镜像
# ====================================
FROM ${PYTHON_IMAGE} AS base

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖
#
# busybox 与 wget 只有**一个**用途：本镜像要同时充当 mihomo 容器，而
# healthcheck.sh 得在那个容器里跑。Debian slim 里既没有 nc 也没有 wget
# （实测 2026-09-19：MISS nc、MISS wget），而 busybox 的 wget 只能发
# GET/POST —— 切换出口是 PUT，必须有 nc 手写 HTTP 请求。
#
# 装了 busybox 也**不会**自动出现 /usr/bin/nc（Docker 里没有 update-alternatives
# 那一步），所以 healthcheck.sh 里对 nc/wget 各做一次"命令探测 + 回退到
# `busybox <applet>`"。这样同一份脚本在 busybox 基座（metacubex/mihomo）和
# 本镜像基座（Debian + busybox）上都能跑，不用维护两个版本。
#
# 代价：两个包合计约 1.3 MB（busybox 808K + wget 472K）。
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        wget \
        busybox \
        git && \
    rm -rf /var/lib/apt/lists/*


# ====================================
# 阶段 2: 依赖安装
# ====================================
FROM base AS builder

# 设置工作目录
WORKDIR /build

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --user --no-warn-script-location -r requirements.txt


# ====================================
# 阶段 3: 运行时镜像
# ====================================
FROM base AS runtime

# 创建非 root 用户
RUN useradd -m -u 1000 -s /bin/bash app
# useradd may use HOME_MODE=0700. Root with cap_drop: ALL cannot bypass
# that directory, so even PYTHONUSERBASE would become unreadable.
RUN chmod 0755 /home/app

# 设置工作目录
WORKDIR /app

# 从 builder 复制已安装的依赖
COPY --from=builder /root/.local /home/app/.local

# PYTHONUSERBASE：**让非 app 用户也能 import 到这些依赖**。
#
# 为什么必须有这一行（2026-09-19 实测的真 BUG）：
#   依赖是 `pip install --user` 装进 /home/app/.local 的，而 --user 目录
#   默认按 HOME 解析。compose 里为了写 out/ 卷会把容器 user 覆盖成 root
#   （见 docker-compose.yml 的 user: 注释），root 的 HOME 是 /root，
#   于是 root 跑起来时 sys.path 里根本没有 /home/app/.local/... →
#   `import yaml` 直接 ModuleNotFoundError → 容器启动即崩。
#   实测对照：
#     docker run --entrypoint python cpa2sub2api   -c "import yaml"  → OK
#     docker run --user 0:0 ... -c "import yaml"                    → ModuleNotFoundError
#
#   PYTHONUSERBASE 是解释器层面的设置，与 HOME 无关，所以对 root 和 app
#   都生效（实测两个用户都 OK）。它比写死 PYTHONPATH=/home/app/.local/lib/
#   python3.11/site-packages 更好：那个路径里嵌了 Python 小版本号，
#   基础镜像升到 3.12 时会静默失效，而 PYTHONUSERBASE 由解释器自己展开
#   （实测 `site.getusersitepackages()` 会解析出 .../python3.11/site-packages）。
ENV PYTHONUSERBASE=/home/app/.local

# 代理内核 + 地理数据（mihomo 跑 rules 模式要 geosite/geoip，缺了会在
# 启动时报 "geoip.dat not found" 并对每条 GEOIP 规则回退成不匹配）。
#
# 为什么连 geo 数据也拷：官方镜像把它们预置在 /root/.config/mihomo/，
# 那是它的默认 -d 目录。本镜像的 -d 由 compose 指向挂载卷
# （/root/.config/mihomo），所以启动时卷里没有这几个文件 ——
# bootstrap-mihomo.sh 会把它们从镜像的备份位置复制进卷（见该脚本）。
# 放在 /usr/local/share/mihomo 而不是默认目录，避免"卷挂上去把镜像里那份遮掉"
# 这种依赖挂载顺序的隐式行为。
COPY --from=mihomo /mihomo /usr/local/bin/mihomo
COPY --from=cpa-consumer /CLIProxyAPI/CLIProxyAPI /usr/local/bin/cpa-consumer
COPY --from=mihomo /root/.config/mihomo/geoip.dat \
     /root/.config/mihomo/geosite.dat \
     /root/.config/mihomo/geoip.metadb \
     /usr/local/share/mihomo/
RUN chmod +x /usr/local/bin/mihomo /usr/local/bin/cpa-consumer

# 复制应用代码
# Only runtime sources enter the image. Private deployment files, screenshots,
# local reports and tutorials cannot be included by an accidental new file.
COPY --chown=app:app tool.py admin_io.py run.py extract.py pricing.py model_selection.py upstream_contract.py tls_diagnostic_transaction.py automatic_probe_budget.py \
     model_probe.py probe_policy.py new_remap_priority.py 一键导入.py entrypoint.sh ./
COPY --chown=app:app mihomo-manager/bootstrap-mihomo.sh \
     mihomo-manager/update-mihomo-subscriptions.sh ./mihomo-manager/
COPY --chown=app:app mihomo-manager/mihomo/config.template.yaml \
     mihomo-manager/mihomo/healthcheck.py \
     mihomo-manager/mihomo/healthcheck.sh ./mihomo-manager/mihomo/
COPY --chown=app:app probe_web.py request_relay.py ./
COPY --chown=app:app probe_web_static/index.html probe_web_static/app.js \
     probe_web_static/styles.css ./probe_web_static/
COPY --chown=app:app relay/__init__.py relay/config.py relay/protocol.py \
     relay/server.py relay/transport.py ./relay/
COPY --chown=app:app third_party/CLIProxyAPI.LICENSE third_party/CLIProxyAPI.NOTICE.txt ./third_party/
COPY --from=cpa-consumer /build/consumer-provenance.json ./third_party/CLIProxyAPI.PROVENANCE.json

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
RUN mkdir -p /app/out /app/state && chown app:app /app/out /app/state

# 设置 PATH
ENV PATH=/home/app/.local/bin:$PATH
ENV SETTINGS_PATH=/app/state/设置.json
ENV REQUEST_RELAY_STATE_DIR=/app/state/relay

# 切换到非 root 用户
USER app

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import tool, yaml, model_selection, model_probe, probe_policy, upstream_contract"

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
