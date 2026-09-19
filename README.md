# CPA2SUB2API

把 CPA / CPAMP 生成的 `config.yaml` 批量灌进 sub2api 的自动化工具。

面向的部署形态是 **VPS 只拉远程镜像**：本机改代码 → 推 GitHub → Actions
构建推 Docker Hub → VPS 拉镜像运行。VPS 上不需要源码。

- 图文并茂的完整教程：[`tutorial.html`](tutorial.html)
  —— 17 张自绘示意图（架构 / 流程 / 数据可视化）+ 30 处一键复制 + 逐项配置说明。
  所有插图均为**脱敏的自绘 SVG**，不含任何真实域名、密钥或账号数据。
- 代理实现文档：[`mihomo-README.md`](mihomo-README.md)、[`mihomo-manager/README.md`](mihomo-manager/README.md)

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **参数全覆盖** | credentials、priority、concurrency、auto_pause_on_expired、proxy_id、请求头、冷却规则、Websockets、模型白名单等一次性迁移 |
| **客户端形态伪装** | 站方只认特定客户端时，自动补齐 `claude-cli` / `codex_cli_rs` / `GeminiCLI` 形态的请求头（只补缺，不覆盖你显式配的），解决「直连能用、经中转 503」 |
| **按域名分桶** | 同一上游的多个 KEY 落进同一优先级桶，互为备份；只有整桶不可用才降级到下一个域名 |
| **健康度重排** | 按 sub2api 实测的「可调度率×60% + 活跃率×40%」重排优先级，各分组内桶号唯一 |
| **模型就高原则** | codex 单族 / openai 多族分开处理；代际门槛从上游源码反推，不写死版本号 |
| **连通性探测** | 按代际实发请求验证；最新代打不通时保留最新代占位并补入实测可用的次最新代 |
| **停用站自动复活** | 探活证明可用后自动重新启用被停用的上游（证据驱动，只动本工具导入的账号） |
| **并发导入** | 建号 / 探活 / 定价三条链路走线程池，worker 可配且自动钳上限 |
| **定时探活** | 复用 sub2api 自带的 scheduled-test-plans，自动恢复 error 状态 |
| **反测活** | 自然语料 + 每个账号错开的探活时刻 |
| **代理兜底** | 直连失败自动经 mihomo 重试；容器部署下由环境变量正确下发 |
| **自带代理内核** | 镜像内含 mihomo 二进制与地理数据，同一个镜像兼任 init 与代理本体 —— 部署不需要第二个镜像，宿主机也不放任何配置文件 |
| **代理镜像可互换** | `upstream-importer` 的镜像里也打了**同一份** mihomo（二进制与两份脚本的 md5 均相同）。整栈里那个共享的 `mihomo` 服务用哪个镜像起都行，用 `.env` 的 `MIHOMO_IMAGE` 切换 |
| **出口自动降级** | 节点全挂时 `healthcheck.sh` 把 PROXY 组切到 DIRECT（裸连总比全挂好），恢复后自动切回 AUTO |
| **可排障** | 全流程出声：丢弃的凭据条目、未迁移的能力、补入的请求头、被降级的站点都写进对照表与备注 |

### 已知限制（诚实标注）

| 项 | 现状 |
|---|---|
| `weight` 数值 | sub2api **没有**账号级 weight 字段，只落到备注；负载分配由 `load_factor` 承担。零权重默认「降到队尾」而非停用（可逆） |
| `cloak` 请求伪装 | 写入 `extra.cloak`，但 **sub2api 网关层没有消费逻辑**，写了不生效 |
| TLS 指纹「是否需要」 | **未实现自动检测**，只做字段搬运（`enable_tls_fingerprint`）；且 sub2api 的 TLS 指纹只对 Anthropic OAuth 账号生效，本工具导入的都是 api_key 账号 |
| `match-regexr` 冷却规则 | 无法迁移（sub2api 只支持子串匹配，不支持正则） |
| `rebuild-mid-system-message` | 无 sub2api 等价物，只记进备注 |

---

## 快速开始

### 1. 环境要求

- Python 3.8+（镜像用 3.11）
- PyYAML 6.0+（唯一第三方依赖）
- sub2api 实例 + 管理员密钥

### 2. Docker 部署

```bash
# .env（部署根目录）
DOCKERHUB_USERNAME=你的DockerHub用户名   # 决定拉哪个镜像
DOCKERHUB_IMAGE=cpa2sub2api              # 可选，留空用仓库名
SUB2API_BASE_URL=https://sub2api.example.com
SUB2API_ADMIN_KEY=admin-替换成你的密钥
FALLBACK_PROXY=http://mihomo:7890

# 启动（带代理）—— 容器起来就自动同步一次，跑完即退
docker compose --profile mihomo up -d

# 看到「同步完成」就是跑完了
docker compose logs cpa2sub2api
```

镜像名与 CI 的推送目标由**同一组仓库变量**决定，不存在写死的账号名。
GitHub 侧的配置见 [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml) 顶部注释。

**容器起来就自动同步，跑完即退** —— 镜像默认命令是 `entrypoint.sh`，
它执行非交互的一键导入。所以 `up -d` 之后**不需要**额外手动执行 py 命令。
同步是幂等的：账号名含内容指纹，重复执行不会重复建号，已存在的走差异同步。

想要定时重复同步，二选一（详见教程 6.2 节）：

```bash
# ① 容器内循环：.env 里设间隔，并把 compose 的 restart 改成 unless-stopped
RUN_INTERVAL_SECONDS=3600

# ② 宿主 cron
0 3 * * * cd /opt/deploy && docker-compose run --rm cpa2sub2api >> /var/log/cpa2sub2api.log 2>&1
```

> ⚠ 每次导入都会按 sub2api 的实测健康度重算优先级并**覆盖线上值**。
> 如果你会在 sub2api 后台手工调整优先级，别叠定时任务 —— 算法会把你的调整算回去。

手动执行（临时用，不重新起整个栈）：

```bash
docker compose run --rm cpa2sub2api                    # 只跑一次同步
docker compose run --rm cpa2sub2api python run.py      # 交互菜单
docker compose run --rm cpa2sub2api python 一键导入.py --dry-run   # 空跑预览
```

### 3. 本机直接运行

```bash
pip install -r requirements.txt
python run.py            # 交互菜单
python 一键导入.py        # 一键全自动
```

---

## 配置

完整逐项说明见 [`tutorial.html`](tutorial.html)。要点：

- **优先级**：环境变量 > `设置.json` > 内置默认值。容器部署只用环境变量。
- **代理地址**：容器里**必须**用服务名 `http://mihomo:7890`。
  写 `127.0.0.1:7890` 指向容器自己，不是代理。
- **代理用哪个镜像起**：本镜像与 `upstream-importer` 的镜像里打的是**同一份**
  mihomo（实测二进制与 `healthcheck.sh`、`bootstrap-mihomo.sh` 的 md5 都相同），
  所以整栈里那个共享的 `mihomo` 服务用哪个都行。用 `.env` 的 `MIHOMO_IMAGE`
  切换，不设则用本镜像。
- **宿主端口**：`MIHOMO_HTTP_PORT` / `MIHOMO_API_PORT` 可覆盖（本机已有代理
  客户端时会占着 7890）。容器内端口不能动 —— 要与 `proxy-url` 和健康检查一致。
- **User-Agent**：sub2api 在 Cloudflare 后面时，必须用浏览器 UA，
  否则会被 CF 的浏览器完整性检查拦成 `403 error code: 1010`
  （请求根本到不了源站，sub2api 日志里看不到任何记录）。
- **并发上限**：三个 worker 都硬钳到 8。调更大只会压垮连接池，不会更快。
- **输入文件**：本工具读的是 CPA 生成的 `config.yaml`（容器里 `/app/config.yaml`，只读）。
  它的逐字段含义见教程 **4.5**——包括哪些字段能迁、哪些只能落备注。
- **两个新增开关**（`PROBE_INACTIVE` / `REVIVE_PROVEN_INACTIVE`，默认都开）：
  config.yaml 里被关闭的上游也会挂探活，证明可用后自动重新启用，不需要人工盯着。

完整环境变量表见教程附录，或 [`.env.example`](.env.example)。

---

## 工作原理

### 优先级是"桶号"，不是权重

CPA 和 sub2api 都按优先级**严格分桶**：只调度最优的那一桶，桶内再按负载选号。
数值方向相反——CPA 里大者优先，sub2api 里小者优先。

### 按域名分桶的三条约束

1. **同一域名下所有 KEY 同桶**：它们是彼此的备份。拆进不同桶，sub2api 只调度
   最小的那个，其余全部闲置，冗余退化成单点。
2. **同一分组内不同域名桶号唯一**：撞桶意味着两个域名混在一桶里轮循，
   失去"整桶挂掉才降级"的分层容灾。
3. **步长 10**，留出人工插桶空间。
4. **编号范围是分组内，不是全局**（2026-09-19 改）：sub2api 选号按 `group_id`
   取候选（`SelectAccountWithGroup` → `account_groups` 中间表），实测每条账号
   只属于一个分组 —— 所以 `CPA-Claude` 的桶号 `10` 与 `CPA-OpenAI` 的 `320`
   永远不会被放在一起比较。每组各自从 `10` 编号，组号上限不再随**别的组**的
   域名数增长。跨分组的桶号相同是正常的，不是缺陷。

### 三层容灾

```
某 KEY 没钱/被封
  → 仅该账号被标 error（其余 KEY 不受影响）
  → 桶内剩余 KEY 继续轮循承载        ← 第一层
  → 整桶全挂 → 降级到下一个域名的桶    ← 第二层
  → 上游恢复 → 探活修回 → 回收调度开关  ← 第三层
```

### 健康度重排

```
健康分 = 可调度比例 × 0.6 + 活跃比例 × 0.4
  → 按健康分降序分配桶号（分高 → 桶号小 → 优先调度）
  → 映射 load_factor：≥0.9→8，≥0.7→6，≥0.5→4，否则 2
```

可调度权重更高（0.6）：sub2api 出错时会同时关掉 `status` 和 `schedulable`，
而恢复路径只还 status 不还 schedulable —— 所以 `schedulable=false` 往往意味着
"这个 KEY 栽过跟头且没人管过它"。

**比例的分母是"该域名已导入的 KEY 数"，不是"KEY 总数"。** 这一条影响扩容行为：

同一域名 5 个 KEY，3 个已导入（2 个可调度、2 个活跃）、2 个是本次新增的：

| 分母 | 健康分 | `load_factor` |
|---|---|---|
| 已导入数（当前实现） | **0.667** | 4 |
| KEY 总数 | 0.400 | 2 |

用"总数"会让**每次扩容都稀释老域名的健康分** —— 上例中新加 2 个 KEY
就把老域名的流量份额腰斩，而它的实际表现一点没变。所以分母只算已有实测数据的那些。

整个域名都是新增（一条都没导入过）时给中位分 0.5：既不因"查无记录"被打到队尾，
也不凭空排到已验证的健康站前面。

### 模型就高原则

| 类型 | 规则 |
|---|---|
| gemini | 只选带 `pro` 的最高编号，保留该版本全部变体 |
| claude | 只选当代，保留全部档位（opus / sonnet / haiku） |
| codex | **单族**：只保留当代 gpt 系列**全部**模型名 |
| openai | **多族**：每一族各取最高级，允许多族并存 |
| 其他 | 原样保留（grok 等） |

**低档模型一律不勾选**：名字里带 `mini` / `flash` / `fast` 的排除，四条路径统一执行。
过滤后一个不剩时保留原列表并出声 —— 白名单为空等于该账号退出调度，比留几个低档模型更糟。

注意 claude 的 `haiku` **不算低档**：它与 opus / sonnet 是同代不同档位，
按"同等级系列全部勾选"应当保留。低档判据只认那三个词。

codex 与 openai 在 CPA 里是两个不同的段，但平台类型都是 `openai`。
本工具按**来源段**分流，不靠"列表里有没有 gpt-6"猜 —— 后者会把声明了
gpt-6 的 openai 上游误当 codex，砍掉它其余全部模型。

代际门槛由上游源码里出现过的真实模型名反推，每次启动刷新。上游从 6 代走到
7 代时门槛自动前移。反推失败时退一步用"模型列表里观测到的最高代"，不会砍错。

### 连通性探测（最新代打不通时的兜底）

上游声明了 `gpt-6` 不等于真的能调。模型目录是站方自己填的，常有"列了但调不通"
的情况。开启探测后按**代际**实发请求验证，据结果分三种处理：

| 探测结果 | 处理 |
|---|---|
| 最新代通 | 只留最新代（正常路径） |
| 最新代不通、次最新代通 | **两代都留** —— 最新代按目录保留占位，同时补入实测可用的次最新代 |
| 两代都不通 | 按目录最高级填充，出声告警 |

"次最新"的粒度是 `(主版本, 次版本)`：`gpt-5.6` 与 `gpt-5` 不是同一代，
`gpt-6` 打不通时补的是 `gpt-5.6` 全族，不会把更旧的 `gpt-5` 拉进来。

**成本**：按 `(段, 域名)` 去重，每个单元最多 2 个请求（最新代 + 次最新代）。
实测 231 个账号收敛成 49 个探测单元，8 并发下约 30 秒；结果缓存 6 小时，重跑 0 秒。

```bash
MODEL_PROBE_ENABLED=true       # 默认开启，设 false 完全跳过（零开销）
MODEL_PROBE_WORKERS=8          # 并发
MODEL_PROBE_CACHE_TTL=21600    # 缓存秒数
```

端点拼接与 CPA 源码一致（`baseURL` 原样 + 各平台端点后缀），
不自行剥离尾部 `/v1` —— CPA 的 base-url 是声明式配置，末尾的 `/v1` 是其一部分。

### 反测活

两层：中英双语自然语料随机抽取；**每个账号派生稳定的分钟偏移**，
避免所有账号在同一分钟一起探活（"全体同时发问"这个同时性比措辞更容易被识别）。

语料分两个池，因为两条链路的诉求相反：

- **探活计划**（sub2api 定时任务）用 30 条较长句子 —— 要模型真的把话答完才算成功
- **模型探测**（`model_probe.py`，导入时判模型通不通）用 10 条短句中英各半 ——
  请求体越小越不容易被限流。此前这里三处硬写 `"hi"`（常量、最容易被识别），
  已改成随机抽取，仍保持 `max_tokens=1`

---

## 产出文件

| 文件 | 内容 |
|---|---|
| `out/import-plan.json` | 完整导入计划 |
| `out/对照表.md` | 逐条对照（原/新优先级、放行模型、排除规则、状态） |
| `out/cpa-config-snapshot.yaml` | 导入时的 config 快照 |
| `out/import-record.json` | 执行结果记录 |

导入前建议抽查 `out/对照表.md` 的「放行模型」列——那就是真正写进 sub2api
的白名单，与线上生效的一致。

---

## 故障排查

| 现象 | 先去查 |
|---|---|
| IP 管理页"链接失败" | `curl .../proxies/AUTO \| jq '.all\|length'` 是否为 0；`allow-lan` 是否 true；是否同一 Docker 网络 |
| 只调高优先级上游，低优先级不试 | 402「预算池耗尽」类的凭据级错误；见教程 8.2 |
| `503 No available accounts` + 分组权限提示 | 该上游只接受特定客户端。本工具会自动补齐客户端形态头（教程 10.1）；仍不通则在 CPA 的 `headers` 里显式写私有头 |
| `400 invalid codex request` | 自定义模型列表含上游不存在的模型名，或协议模式不匹配 |
| `524 origin_response_timeout` | CF 回源超时 120 秒不可延长，需压缩 CPA 的重试预算；见教程 8.5 |
| `empty or malformed response (HTTP 200)` + 0 SSE 事件 | 流式引导未缓冲；见教程 8.6 |
| `403 error code: 1010` | CF 浏览器完整性检查，设置浏览器 `USER_AGENT` |
| **所有**管理接口返回 `423 Locked` | **不是密钥问题**。sub2api 的管理员合规确认门禁：先去面板确认合规承诺；见教程 11.1 |
| CPAMP 面板提示"管理员密钥无效" | 是**面板登录密钥**，不是 CPA 管理密钥。改文件无效，只能 `reset-admin-key`；见教程 8.9 / 8.10 |
| `{"error":"IP banned due to too many failed attempts"}` | 认证失败超 5 次触发 30 分钟 IP 封禁。先停 CPAMP 再重启 CPA；见教程 8.11 |
| 改了 `secret-key` 之后彻底登不进 | 该字段要的是 **bcrypt 哈希**（`$2a$` 开头），不是明文；见教程 8.12 |
| 本机全绿但 VPS 报错 | 本机与 VPS 的六类环境差异；逐条核对教程第十一章的清单 |
| 改了代码但 VPS 行为没变 | Docker 默认「本地没有才拉」，`up -d` 不会取新镜像。本仓库已设 `pull_policy: always`；手工 `docker compose pull cpa2sub2api` 兜底 |
| `Permission denied: '/app/out/...'` | `out` 目录属主与容器 uid 不匹配。compose 已用 `user:` 覆盖为 root；或把宿主目录 `chown 1000:1000` |
| `[Errno 21] Is a directory: '/app/config.yaml'` | 宿主上该路径是**目录**（Docker 在文件不存在时自动建的空目录）。放上真正的文件，或改用 `CPA_BASE_URL` 在线拉取 |
| mihomo 容器一直 `unhealthy` | 官方镜像里**没有 python3 也没有 curl**。本仓库用纯 busybox 的 `healthcheck.sh`（教程 11 表第 7 项）。现在 mihomo 由**本项目镜像**充当，里面装了 busybox+wget，脚本对 `nc`/`wget` 会做"命令探测 + 回退到 `busybox <applet>`" |
| mihomo 日志说「缺少 HTTP 工具」 | 镜像基座里既没有 nc/wget 也没有 busybox。这不是出口故障 —— 按 Dockerfile 装上 `busybox` 与 `wget` 即可 |
| 出口正常但上游全 502/524 | 节点其实挂了。手动跑 `docker compose exec mihomo sh /root/.config/mihomo/healthcheck.sh` 看它有没有把出口降级到 DIRECT（教程 5.6） |
| `bootstrap-mihomo.sh` 抛 `TypeError: must be real number` | 订阅 URL 里的 `%2F`/`%3D` 被当成格式化占位符。本仓库已改成一次填完 + `json.dumps`；若你本地有旧副本，同步一次 |
| `Pool overlaps with other one on this address space` | 网段冲突。在 `.env` 里设 `CPA2SUB2API_SUBNET=172.29.0.0/16` |

### 密钥排障速查（教程 8.9–8.13）

管理 CPA/CPAMP 时会遇到几个**名字相近但用途完全不同**的密钥，改错对象是常见的时间黑洞：

| 你要做的事 | 用哪个密钥 | 存在哪 |
|---|---|---|
| 登录 `cpas.域名/management.html` 面板 | **面板登录密钥** | CPAMP 首次启动时写入 `/data/usage.sqlite`，之后改文件无效 |
| 让 CPAMP 读到 CPA 的账号数据 | **CPA 管理密钥** | CPAMP 侧 `secrets/cpa_management_key`，必须与 CPA 侧 `config.yaml` 的 `secret-key` **哈希对应同一把明文** |
| Claude Code / Codex 客户端连 CPA | **下游 api-key** | `config.yaml` 的 `api-keys` 列表（`sk-` 开头） |
| 用 curl 调 `/v0/management/*` | CPA 管理密钥 | 同上，或环境变量 `MANAGEMENT_PASSWORD`（明文，优先于哈希） |

**换 CPA 管理密钥时优先用环境变量方式**，不需要 bcrypt、删掉即可回退：

```yaml
# docker-compose.yml 的 cli-proxy-api 服务
environment:
  - MANAGEMENT_PASSWORD=${CPA_MANAGEMENT_PASSWORD}
```

详细排查步骤见 [`tutorial.html`](tutorial.html) 第八章。

---

## 测试

```bash
python tests/test_fixes.py
```

纯函数回归测试，不联网、不写 `out/`。覆盖：优先级分组内唯一定序、
同域名同桶、健康度重排与冷启动保护、模型就高分流、域名级冷却规则、
探活时刻错开、低档模型过滤、连通性探测三分支、配置解析。

---

## 目录结构

```
.
├── tool.py                    主程序（导入、优先级、探活、定价）
├── 一键导入.py                 全自动流程入口
├── run.py                     ASCII 名启动器（绕开 cmd 代码页问题）
├── model_selection.py         模型就高原则的**唯一权威实现**
├── model_probe.py             模型连通性探测（按代际、去重、并发、缓存）
├── new_remap_priority.py      基于 sub2api 实测状态的智能优先级
├── extract.py                 从上游源码反推常量与代际门槛
├── pricing.py                 定价解析与推送
├── mihomo-manager/            mihomo 订阅与自举
├── tests/test_fixes.py        回归测试
├── docker-compose.yml         独立部署（自带 mihomo）
├── docker-compose.cpa2sub2api.yml  叠加到已有部署栈的片段
├── tutorial.html              完整教程
└── .github/workflows/         CI：代码检查 + 镜像构建
```

---

## 安全

不要提交以下内容：

- `.env`、`设置.json`、`config.yaml` —— 含管理密钥与连接信息
- `mihomo-manager/mihomo-subscriptions.conf` —— 含订阅 token
- `*.docx` / `*.pdf` / `*.mhtml` —— 需求与说明文档，
  常含**明文密钥的界面截图**，是二进制文件、无法靠文本搜索审出来

`.gitignore` 已覆盖以上全部。

---

## 许可

MIT，见 [LICENSE](LICENSE)。
