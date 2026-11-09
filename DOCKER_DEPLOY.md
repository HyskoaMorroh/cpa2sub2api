# CPA2SUB2API Docker 部署指南

面向的部署形态是 **VPS 只拉远程镜像**：本机改代码 → 推 GitHub →
Actions 构建推 Docker Hub → VPS 拉镜像运行。**VPS 上不需要源码**，
也不需要预置任何 mihomo 配置文件（配置由 init 容器从镜像内模板物化）。

更完整的图文说明见 [`tutorial.html`](tutorial.html)。

本工具提供两个同镜像的可选服务：`probe-ui`（站级授权面板，宿主仅回环8775）和 `request-relay`（固定官方源码构建的CPA consumer，包含本项目诊断重定向安全补丁，不映射宿主端口）。公开模板默认关闭两者；需要 CPA 独有行为的输入必须启用 relay，不能静默丢字段后直连。完整字段、TLS/scope、来源认领及迁移合同以[主 README](README.md#可选-request-relay让原-cpa-运行时消费参数)和[教程](tutorial.html#s6-relay)为准。

镜像内 `/app/third_party/CLIProxyAPI.PROVENANCE.json` 记录consumer固定上游commit、构建镜像、二进制/输入SHA、CGO、Go版本和架构。功能标识 `cpa2sub2api-diagnostic-no-redirect-v1` 对诊断关闭重定向，不改变普通业务原配置；重试为0本身不代表没有重定向。周计划诊断model标记由工具自动生成，认证、route/model及双闸验证后才消费；不要人工拼接带斜线的Gemini模型标记。新镜像应同时核验来源记录、实际consumer与relay代际，不能只拉原版CPA镜像就假定包含本项目诊断补丁。

```bash
# 首次先只读预览本地配置，不访问管理端/上游、不发布relay状态
docker compose run --rm cpa2sub2api python tool.py import --preview
# 输入需要relay时，先在私有.env同时设置：
# REQUEST_RELAY_ENABLED=true 和 COMPOSE_PROFILES=request-relay
# 无状态的首次服务暂时unready正常；不要用healthy依赖阻断首次导出
docker compose up -d request-relay
# 原子导出 → 实际consumer代际就绪 → 账号创建/同步
docker compose run --rm cpa2sub2api python tool.py import --yes
# 仅在已配置强随机PROBE_UI_TOKEN和精确PROBE_UI_ORIGIN后启动面板
docker compose --profile probe-ui up -d probe-ui
```

`--host upstream.example.invalid --preview` / `--yes` 只添加/同步该站，不清理其他站；无 `--host` 是全量输入。Web 开启只保存授权，关闭先保存再尽力校准旧计划，检查 `saved` / `applied`；环境 `MODEL_PROBE_ENABLED=false` 始终是硬闸。多个来源使用不同稳定 scope 和不同 state_dir/卷。以上是部署合同，不是某个真实环境已经验收的结论。

Web关闭后的逐账号校准可能等待管理端限流窗口；网页120秒超时后先刷新核实，不能把超时提示当作未保存，也不能把保存成功当作旧计划已停止。relay模式的显式诊断与恢复只读已有`CURRENT`，地址、目录、scope、权重模式或代际变化时旧证据失效。每次consumer启动使用独立空OAuth目录，原配置和私有令牌仍保留在state卷；正常重启无需删除该卷。

### 官方版本信息与受控升级

`import --preview` 和显式离线检查不请求 GitHub；预览也不访问管理端或模型上游。公开模板 `FETCH_UPSTREAM_SRC=true` 让正常线上执行按TTL自动查询官方最新稳定 tag/commit与候选源码，默认每7天；缓存新鲜时不重复HTTP。设 false 则只用本地证据。也可显式检查和刷新：

```bash
docker compose run --rm cpa2sub2api python tool.py upstream inspect
docker compose run --rm cpa2sub2api python tool.py upstream refresh --offline
docker compose run --rm cpa2sub2api python tool.py upstream refresh
docker compose run --rm cpa2sub2api python tool.py upstream refresh --latest
```

`inspect` 零网络、零缓存写入；`refresh --offline` 只用已有完整且SHA匹配的固定源码；`refresh` 显式刷新固定版本；`refresh --latest` 取得官方最新稳定版及候选源码，不能与 `--offline` 同用。可加 `--kind cpa` 或 `--kind sub2api`，默认检查全部。

源码缓存位于 out 卷的 `upstream-cache/<kind>/<commit>/<generation>/manifest.json`，最新版本报告为 `upstream-cache/release-evidence.json`，固定源码刷新报告为同目录的 `source-cache-evidence.json`。`UPSTREAM_CACHE_TTL_SECONDS=604800` 对应设置键 `upstream_cache_ttl_seconds`（默认7天）；过期固定缓存可离线复用并显示 stale，不能据此称最新。未激活的新版本候选不阻止仍匹配固定合同的导入；实际使用的sourcehash漂移、未知受管字段或旧计划固定身份不匹配才会阻止实际导入及清理。首次空out卷仍可按镜像内合同运行，不要求下载Go源码。发现可用新版本不升级正在运行的 consumer 或 sub2api；先完成新合同语义适配、固定consumer摘要及基线SHA和容器/真实管理页/调用链验证，再发布和拉取已验证镜像。删除报告不能使新源码变兼容。详细字段合同和回滚步骤见[README](README.md#上游版本信息与字段合同)。

---

## 目录

1. [部署架构](#1-部署架构)
2. [快速开始](#2-快速开始)
3. [配置详解](#3-配置详解)
4. [代理（mihomo）](#4-代理mihomo)
5. [执行与运维](#5-执行与运维)
6. [故障排查](#6-故障排查)
7. [镜像构建](#7-镜像构建)

---

## 1. 部署架构

```
                    ┌──────────────────────────────┐
   客户端 ──► CF ──► │ nginx :443                   │
                    └───────────┬──────────────────┘
                                │
                    ┌───────────▼──────────────────┐
                    │ sub2api                      │
                    └───────────┬──────────────────┘
                                │
                    ┌───────────▼──────────────────┐
                    │ CPA (cli-proxy-api) :8317    │
                    └───────────┬──────────────────┘
                                │ 需要代理的条目走这里
                    ┌───────────▼──────────────────┐
                    │ mihomo :7890                 │
                    └───────────┬──────────────────┘
                                │
                            各上游站点

   本工具（cpa2sub2api）是一次性运维容器：
   读 CPA 的 config.yaml ──► 写 sub2api 的账号/分组/代理/定价
```

**默认工具与可选代理服务**（另有上文两个可选 profile）：

| 服务 | 性质 | 说明 |
|---|---|---|
| `cpa2sub2api` | 按需运行的运维工具 | `restart: "no"`，跑完就退 |
| `mihomo-init` | init 容器，跑完即退 | 从镜像内模板物化 mihomo 配置到命名卷 |
| `mihomo` | 长驻 | 代理服务，读 init 写好的配置 |

---

## 2. 快速开始

### 2.1 拉取镜像

镜像名由**你自己的环境变量**决定，仓库里不写死账号名：

```bash
# .env
DOCKERHUB_USERNAME=你的DockerHub用户名
DOCKERHUB_IMAGE=cpa2sub2api        # 可选，留空用仓库名

docker pull $DOCKERHUB_USERNAME/${DOCKERHUB_IMAGE:-cpa2sub2api}:latest
```

### 2.2 准备文件

在部署根目录（`/opt/deploy`）：

| 文件 | 必需 | 来源 |
|---|---|---|
| `.env` | ✅ | 自己写，见 3.1 |
| `config.yaml` | ✅ | CPA / CPAMP 生成的产物，拷过来即可（只读） |
| `docker-compose.yml` | ✅ | 随镜像仓库提供 |

### 2.3 最小 `.env`

```bash
SUB2API_BASE_URL=https://sub2api.example.com
SUB2API_ADMIN_KEY=admin-替换成你的管理员密钥
FALLBACK_PROXY=http://mihomo:7890
```

### 2.4 启动

```bash
cd /opt/deploy

# 带代理（推荐）
docker compose --profile mihomo up -d

# 不带代理，直连
docker compose up -d cpa2sub2api
```

首次启动时 `mihomo-init` 会先生成配置，多等十几秒属正常。

### 2.5 执行导入

```bash
# 空跑预览（不写任何数据）
docker compose run --rm cpa2sub2api python 一键导入.py

# 正式导入
docker compose run --rm cpa2sub2api python 一键导入.py
```

---

## 3. 配置详解

### 3.1 连接配置（必填）

```bash
SUB2API_BASE_URL=https://sub2api.example.com
SUB2API_ADMIN_KEY=admin-xxxxxxxx
```

### 3.2 CPA 连接（可选）

本地没有 `config.yaml` 时，让它从 CPA 的管理接口在线拉：

```bash
CPA_BASE_URL=http://cli-proxy-api:8317
CPA_MANAGEMENT_KEY=your-management-key
```

### 3.3 代理

```bash
# 容器间必须用服务名。写 127.0.0.1 指向的是本容器自己。
FALLBACK_PROXY=http://mihomo:7890

# 调试时临时关掉代理兜底
# DISABLE_PROXY_FALLBACK=true
```

### 3.4 并发

```bash
IMPORT_WORKERS=4        # 建号并发，上限 8
TEST_PLAN_WORKERS=8     # 探活计划并发，上限 8
PRICING_WORKERS=8       # 定价推送并发，上限 8
DEFAULT_CONCURRENCY=3   # 每个账号的并发请求数
BATCH_SIZE=50           # 每批写入条数
```

三个 worker 都硬性钳到 8，调更大不会更快，只会压垮连接池。

### 3.5 策略开关

```bash
HEALTH_RERANK_ENABLED=true      # 按实测健康度重排优先级
SYNC_EXISTING_ACCOUNTS=true     # 已存在账号也同步最新参数
RESPECT_WEIGHT_ZERO=true        # weight=0 降级而非停用
POOL_MODE_ENABLED=true          # 同账号重试瞬时抖动
AUTO_RECOVER_ENABLED=true       # 定时探活并自动恢复
AUTO_RECOVER_CRON=*/30 * * * *  # 探活频率
```

### 3.6 HTTP

```bash
HTTP_TIMEOUT=30
HTTP_RETRIES=3
# sub2api 在 Cloudflare 后面时必须用浏览器 UA，否则被拦成 403 error 1010
USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36
```

### 3.7 优先级

```
环境变量  >  设置.json  >  内置默认值
```

**容器部署只用环境变量**，不要混用 `设置.json` —— 两份配置同时存在时排查会非常混乱。

完整变量表见 [`tutorial.html`](tutorial.html) 附录或 [`.env.example`](.env.example)。

---

## 4. 代理（mihomo）

### 4.1 为什么不需要手工准备配置文件

`metacubex/mihomo` 官方镜像基于 **scratch**：没有 shell、没有 `curl`/`wget`、没有 python，
它自己**没法**把模板展开成配置。

所以由本项目的镜像充当 init 容器（有 python3 和 sh）：

```
mihomo-init（从镜像内模板物化到 /mihomo-config，跑完即退）
      │  depends_on: service_completed_successfully
      ▼
mihomo（读同一个命名卷里的 config.yaml，长驻）
```

这样部署保持"VPS 只拉镜像、宿主不预置文件"。

### 4.2 配置订阅

在 `.env` 里：

```bash
MIHOMO_SECRET=自己生成的随机串
MIHOMO_SUBSCRIPTIONS=wog=https://机场订阅地址
MIHOMO_AUTOMATIC_TESTS=false
MIHOMO_SHARED_VOLUME=mihomo-data  # 共享叠加模板：父compose配置卷key
```

或每个订阅一个变量：

```bash
MIHOMO_SUB_WOG=https://机场订阅地址
MIHOMO_SUB_WOGB=https://另一个订阅地址
```

两种可混用，`MIHOMO_SUB_<名称>` 优先。

### 4.2.1 关闭节点自动检测的共享栈策略

`MIHOMO_AUTOMATIC_TESTS=false` 关闭 provider 节点健康检测和 url-test，自动检测组转为手工 `select`，保留节点、过滤和规则。模型账号在线检测由总闸和站级授权另外控制，两者不互相授权。

共享父栈保留 upstream-importer 原 init 和 kernel 镜像，在原 init 完成后运行本项目镜像的 `mihomo-policy-init`。该服务使用 `MIHOMO_POLICY_ONLY=1`，无网络、同一配置卷，只原子处理已有配置；失败时 kernel 继续等待。它不覆盖原中立连通性健康脚本、地理数据和手工 fallback。

```bash
# 私有.env中设MIHOMO_AUTOMATIC_TESTS=false，先核实共享卷
docker compose stop mihomo
docker compose up -d --force-recreate mihomo-init mihomo-policy-init mihomo
docker compose ps -a mihomo-init mihomo-policy-init mihomo
```

恢复自动节点检测时，明确设置 true，并恢复组的自动类型和 provider health-check，或使用已备份配置/受控重建，再按上述次序启动；仅改变量不能把 select 自动还原。独立模板和共享叠加模板均带此策略，共享模板保留父栈原镜像。

### 4.3 验证（四步，缺一不可）

```bash
# ① 进程与 API
docker compose ps mihomo
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" http://127.0.0.1:9090/version

# ② AUTO 组有没有节点（最容易漏，为 0 就是"代理静默失效"）
curl -fsS -H "Authorization: Bearer $MIHOMO_SECRET" \
  http://127.0.0.1:9090/proxies/AUTO | jq '{now: .now, count: (.all|length)}'

# ③ 宿主机能否出网
curl -fsS -m 15 -x http://127.0.0.1:7890 -o /dev/null \
  -w 'egress OK: %{http_code}\n' https://www.gstatic.com/generate_204

# ④ 从本工具容器内部能否出网（容器间走服务名，不经过宿主端口映射）
docker compose exec cpa2sub2api \
  curl -fsS -m 15 -x http://mihomo:7890 -o /dev/null \
  -w '%{http_code}\n' https://www.gstatic.com/generate_204
```

第 ② 步为 0 是"链接失败"最常见的真因：mihomo 进程健康、端口在听、API 有响应，
**但任何请求都出不去，日志里看不出异常**。

### 4.4 更新订阅

```bash
bash mihomo-manager/update-mihomo-subscriptions.sh
```

脚本会重写 `proxy-providers`、两个组的 `use`、订阅域名的 DIRECT 规则，
跑 `mihomo -t` 校验语法（失败自动回滚），清 provider 缓存，重启容器，
然后逐订阅点名、查 AUTO 节点数、实测出网 —— 任一项失败 `exit 1`。

详见 [`mihomo-manager/README.md`](mihomo-manager/README.md)。

---

## 5. 执行与运维

### 5.1 两种入口

| 入口 | 命令 | 适用 |
|---|---|---|
| 一键全自动 | `python 一键导入.py` | 日常使用 |
| 交互菜单 | `python run.py` | 分步操作、回滚、推送定价 |

容器里执行：

```bash
docker compose run --rm cpa2sub2api python run.py
```

### 5.2 导入的六个阶段 + 第二阶段重排

```
[1/6] 建齐分组、建立降级链
[2/6] 建齐代理
[3/6] 去重、健康度重排、批量建号
[4/6] 给需要停用的账号补停用
[5/6] 挂定时探活计划
[6/6] 回收被关掉的调度开关、复活已证明可用的账号
 ↓
[第二阶段] 用新建账号的真实状态再重排一次优先级
```

第二阶段是为解决冷启动：首次导入时 sub2api 里还没有任何账号，健康度无从算起，
只能沿用 CPA 的优先级；新建完成后立刻重排，一次到位，不需要手工跑第二遍。

### 5.3 产物

| 文件 | 内容 |
|---|---|
| `out/import-plan.json` | 完整导入计划 |
| `out/对照表.md` | 逐条对照（原/新优先级、放行模型、排除规则、状态） |
| `out/cpa-config-snapshot.yaml` | 导入时的 config 快照 |
| `out/import-record.json` | 执行结果记录 |

**导入前先抽查 `out/对照表.md` 的「放行模型」列** —— 那就是真正写进 sub2api
的计划白名单；仍须在实际管理页与消费者读回核实。

### 5.4 日志

```bash
docker compose logs -f cpa2sub2api
docker compose logs -f mihomo
docker compose exec mihomo python3 /root/.config/mihomo/healthcheck.py
```

---

## 6. 故障排查

| 现象 | 先查什么 |
|---|---|
| IP 管理页"链接失败" | `AUTO` 组节点数是否为 0；`allow-lan` 是否 true；是否同一 Docker 网络 |
| 只调高优先级上游，低优先级不试 | 凭据级错误（如 402 预算池耗尽）被网关判为非重试；见 tutorial 8.2 |
| `503 No available accounts` + 分组权限 | 该上游只接受特定客户端，需在 CPA 侧配伪装请求头 |
| `400 invalid codex request` | 模型列表含上游不存在的名字，或协议模式不匹配 |
| `524 origin_response_timeout` | CF 回源超时 120 秒不可延长，需压缩 CPA 的重试预算 |
| `empty or malformed response (HTTP 200)` + 0 SSE | 流式引导未缓冲 |
| `403 error code: 1010` | CF 浏览器完整性检查，设置浏览器 `USER_AGENT` |
| 改了代码但 VPS 拉到的还是旧镜像 | CI 是否真的跑了；分支名是否与 workflow 的触发条件一致 |

详细步骤见 [`tutorial.html`](tutorial.html) 第八章。

---

## 7. 镜像构建

### 7.1 自动构建（推荐）

推送即触发：

```bash
git push origin master
```

`.github/workflows/docker-publish.yml` 会构建 `linux/amd64` + `linux/arm64`
双架构镜像并推送到 Docker Hub。

**前提：仓库里配置好以下内容**（Settings → Secrets and variables → Actions）：

| 类型 | 名称 | 必需 | 说明 |
|---|---|---|---|
| Variable | `DOCKERHUB_USERNAME` | ✅ | Docker Hub 用户名。**它同时是"要不要发 Docker Hub"的开关** |
| Variable | `DOCKERHUB_IMAGE` | — | 镜像名。留空 = 用仓库名 |
| Secret | `DOCKERHUB_TOKEN` | ✅ | Docker Hub Access Token，**不要填登录密码** |

只配了用户名没配 token 不会让构建失败 —— Docker Hub 那一步会安静跳过，
GHCR 照常发布。

工作流只在 `master` / `main` 分支和 `v*` tag 上触发，推别的分支不会跑。

### 7.2 本地构建（调试用）

```bash
docker build -t $DOCKERHUB_USERNAME/${DOCKERHUB_IMAGE:-cpa2sub2api}:latest .

# 多架构
docker buildx build --platform linux/amd64,linux/arm64 \
  -t $DOCKERHUB_USERNAME/${DOCKERHUB_IMAGE:-cpa2sub2api}:latest --push .
```

### 7.3 运行单个容器

```bash
docker run -it --rm \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/out:/app/out \
  -e SUB2API_BASE_URL=https://sub2api.example.com \
  -e SUB2API_ADMIN_KEY=admin-xxx \
  -e FALLBACK_PROXY=http://mihomo:7890 \
  --network your-stack_default \
  $DOCKERHUB_USERNAME/${DOCKERHUB_IMAGE:-cpa2sub2api}:latest python 一键导入.py
```
