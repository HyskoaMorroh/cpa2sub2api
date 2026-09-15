# CPA2SUB2API

把 CPA / CPAMP 生成的 `config.yaml` 批量灌进 sub2api 的自动化工具。

面向的部署形态是 **VPS 只拉远程镜像**：本机改代码 → 推 GitHub → Actions
构建推 Docker Hub → VPS 拉镜像运行。VPS 上不需要源码。

- 图文并茂的完整教程：[`tutorial.html`](tutorial.html)（含逐项配置说明与一键复制）
- 代理实现文档：[`mihomo-README.md`](mihomo-README.md)、[`mihomo-manager/README.md`](mihomo-manager/README.md)

---

## 核心能力

| 能力 | 说明 |
|---|---|
| **参数全覆盖** | credentials、priority、concurrency、auto_pause_on_expired、proxy_id、请求头、冷却规则、Websockets、模型白名单等一次性迁移 |
| **按域名分桶** | 同一上游的多个 KEY 落进同一优先级桶，互为备份；只有整桶不可用才降级到下一个域名 |
| **健康度重排** | 按 sub2api 实测的「可调度率×60% + 活跃率×40%」重排优先级，桶号全局唯一 |
| **模型就高原则** | codex 单族 / openai 多族分开处理；代际门槛从上游源码反推，不写死版本号 |
| **并发导入** | 建号 / 探活 / 定价三条链路走线程池，worker 可配且自动钳上限 |
| **定时探活** | 复用 sub2api 自带的 scheduled-test-plans，自动恢复 error 状态 |
| **反测活** | 自然语料 + 每个账号错开的探活时刻 |
| **代理兜底** | 直连失败自动经 mihomo 重试；容器部署下由环境变量正确下发 |

---

## 快速开始

### 1. 环境要求

- Python 3.8+（镜像用 3.11）
- PyYAML 6.0+（唯一第三方依赖）
- sub2api 实例 + 管理员密钥

### 2. Docker 部署

```bash
# .env（部署根目录）
SUB2API_BASE_URL=https://sub2api.example.com
SUB2API_ADMIN_KEY=admin-替换成你的密钥
FALLBACK_PROXY=http://mihomo:7890

# 启动（带代理）
docker compose --profile mihomo up -d

# 执行导入
docker compose run --rm cpa2sub2api python 一键导入.py
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
- **User-Agent**：sub2api 在 Cloudflare 后面时，必须用浏览器 UA，
  否则会被 CF 的浏览器完整性检查拦成 `403 error code: 1010`
  （请求根本到不了源站，sub2api 日志里看不到任何记录）。
- **并发上限**：三个 worker 都硬钳到 8。调更大只会压垮连接池，不会更快。

完整环境变量表见教程附录，或 [`.env.example`](.env.example)。

---

## 工作原理

### 优先级是"桶号"，不是权重

CPA 和 sub2api 都按优先级**严格分桶**：只调度最优的那一桶，桶内再按负载选号。
数值方向相反——CPA 里大者优先，sub2api 里小者优先。

### 按域名分桶的三条约束

1. **同一域名下所有 KEY 同桶**：它们是彼此的备份。拆进不同桶，sub2api 只调度
   最小的那个，其余全部闲置，冗余退化成单点。
2. **不同域名桶号全局唯一**：撞桶意味着两个域名混在一桶里轮循，
   失去"整桶挂掉才降级"的分层容灾。
3. **步长 10**，留出人工插桶空间。

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

### 模型就高原则

| 类型 | 规则 |
|---|---|
| gemini | 只选带 `pro` 的最高编号，保留该版本全部变体 |
| claude | 只选当代，保留全部档位（opus / sonnet / haiku） |
| codex | **单族**：只保留当代 gpt 系列**全部**模型名 |
| openai | **多族**：每一族各取最高级，允许多族并存 |
| 其他 | 原样保留（grok 等） |

codex 与 openai 在 CPA 里是两个不同的段，但平台类型都是 `openai`。
本工具按**来源段**分流，不靠"列表里有没有 gpt-6"猜 —— 后者会把声明了
gpt-6 的 openai 上游误当 codex，砍掉它其余全部模型。

代际门槛由上游源码里出现过的真实模型名反推，每次启动刷新。上游从 6 代走到
7 代时门槛自动前移。反推失败时退一步用"模型列表里观测到的最高代"，不会砍错。

### 反测活

两层：中英双语自然语料随机抽取；**每个账号派生稳定的分钟偏移**，
避免所有账号在同一分钟一起探活（"全体同时发问"这个同时性比措辞更容易被识别）。

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
| `503 No available accounts` + 分组权限提示 | 该上游只接受特定客户端，需在 CPA 侧配伪装请求头，与账号无关 |
| `400 invalid codex request` | 自定义模型列表含上游不存在的模型名，或协议模式不匹配 |
| `524 origin_response_timeout` | CF 回源超时 120 秒不可延长，需压缩 CPA 的重试预算；见教程 8.5 |
| `empty or malformed response (HTTP 200)` + 0 SSE 事件 | 流式引导未缓冲；见教程 8.6 |
| `403 error code: 1010` | CF 浏览器完整性检查，设置浏览器 `USER_AGENT` |

详细排查步骤见 [`tutorial.html`](tutorial.html) 第八章。

---

## 测试

```bash
python tests/test_fixes.py
```

纯函数回归测试，不联网、不写 `out/`。覆盖：优先级全局唯一定序、
同域名同桶、健康度重排与冷启动保护、模型就高分流、域名级冷却规则、
探活时刻错开、配置解析。

---

## 目录结构

```
.
├── tool.py                    主程序（导入、优先级、探活、定价）
├── 一键导入.py                 全自动流程入口
├── run.py                     ASCII 名启动器（绕开 cmd 代码页问题）
├── model_selection.py         模型就高原则的**唯一权威实现**
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
