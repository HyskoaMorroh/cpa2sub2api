# CPA2SUB2API Docker 部署指南

面向的部署形态是 **VPS 只拉远程镜像**：本机改代码 → 推 GitHub →
Actions 构建推 Docker Hub → VPS 拉镜像运行。**VPS 上不需要源码**，
也不需要预置任何 mihomo 配置文件（配置由 init 容器从镜像内模板物化）。

更完整的图文说明见 [`tutorial.html`](tutorial.html)。

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

**三个服务**：

| 服务 | 性质 | 说明 |
|---|---|---|
| `cpa2sub2api` | 按需运行的运维工具 | `restart: "no"`，跑完就退 |
| `mihomo-init` | init 容器，跑完即退 | 从镜像内模板物化 mihomo 配置到命名卷 |
| `mihomo` | 长驻 | 代理服务，读 init 写好的配置 |

---

## 2. 快速开始

### 2.1 拉取镜像

```bash
docker pull hyskaamorroh/cpa2sub2api:latest
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
```

或每个订阅一个变量：

```bash
MIHOMO_SUB_WOG=https://机场订阅地址
MIHOMO_SUB_WOGB=https://另一个订阅地址
```

两种可混用，`MIHOMO_SUB_<名称>` 优先。

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
的白名单，与线上生效的完全一致。

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

**前提：仓库里要配置好 secret `DOCKER_HUB_TOKEN`**
（Settings → Secrets and variables → Actions）。
缺少它登录会失败，而工作流只监听 `master`/`main` 与 `v*` tag，
推别的分支不会触发。

### 7.2 本地构建（调试用）

```bash
docker build -t hyskaamorroh/cpa2sub2api:latest .

# 多架构
docker buildx build --platform linux/amd64,linux/arm64 \
  -t hyskaamorroh/cpa2sub2api:latest --push .
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
  hyskaamorroh/cpa2sub2api:latest python 一键导入.py
```
